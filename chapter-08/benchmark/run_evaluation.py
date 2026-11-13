"""
run_evaluation.py - Run the chunking benchmark against the deployed KBs.

Accompanies Chapter 8 of "AgentCore in Production: The Operator's Playbook"
(2nd edition, October 2026).

For each Knowledge Base in the Terraform output, submit a native Bedrock
RAG evaluation job (applicationType=RagEvaluation):

  * retrieve-and-generate job, always: Builtin.Correctness, Completeness,
    Helpfulness, Faithfulness. Generator: Sonnet 4.6 via inference profile.
  * retrieve-only job, with --retrieval-metrics: Builtin.ContextRelevance and
    Builtin.ContextCoverage. Gotcha 6: these cannot share a job with the
    generation metrics. The first edition skipped this job; Section 8.4 lists
    that as one of the benchmark's four weaknesses.

Judge: Nova Pro by default. Sonnet 4.5/4.6 and Opus 4.5-4.8 are now on the
judge allowlist (gotcha 4 is resolved); Nova Pro stays as a cross-family
judge for Claude-generated answers. Override with JUDGE_MODEL_ID.

Repeats: --repeats N submits N independent generation jobs per KB and
reports mean and min-max range. The 2nd edition asks for 3-5 (CT-08-10).

Usage:
    python run_evaluation.py --terraform-output terraform/output.json \
        --questions questions.jsonl \
        --eval-role-arn "$(cd terraform && terraform output -raw eval_role_arn)" \
        [--retrieval-metrics] [--repeats 3]

STATUS: run end to end against a live account on 2026-11-11 (console test CT-08-09),
one Knowledge Base, three questions, Nova 2 Lite as generator and Nova Pro as judge:
both the generation job and the retrieve-only job completed with no unparsed scores.
Two constraints came out of that run:
  * Claude 5.x models cannot be the generator. The service rejects the job with
    "Custom prompt templates must be provided for both Orchestration and Generation",
    and the evaluation API has no field for an orchestration prompt. Use a model with
    default Knowledge Base prompts (Nova 2 Lite worked). Sonnet 4.6, the 1st edition's
    generator, needs account access to the Anthropic use-case form.
  * The eval role's InvokeModel must cover the generator you pass; see gotcha 5 in
    the README.
The first-edition script submitted a model-evaluation job with pre-generated
responses and unprefixed metric names, which did not match the RAG path the
published numbers came from.

Requires: pip install "boto3>=1.43"
"""

import argparse
import json
import os
import statistics
import sys
import time

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
GENERATOR_PROFILE_ID = os.environ.get("GENERATOR_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
JUDGE_MODEL_ID = os.environ.get("JUDGE_MODEL_ID", "amazon.nova-pro-v1:0")

GENERATION_METRICS = ["Builtin.Correctness", "Builtin.Completeness",
                      "Builtin.Helpfulness", "Builtin.Faithfulness"]
RETRIEVAL_METRICS = ["Builtin.ContextRelevance", "Builtin.ContextCoverage"]

bedrock = boto3.client("bedrock", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)


def generator_arn() -> str:
    account = boto3.client("sts").get_caller_identity()["Account"]
    return f"arn:aws:bedrock:{AWS_REGION}:{account}:inference-profile/{GENERATOR_PROFILE_ID}"


def upload_dataset(questions_file: str, bucket: str, key: str) -> str:
    """questions.jsonl -> RAG evaluation dataset (one conversation turn per line)."""
    lines = []
    with open(questions_file, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            lines.append(json.dumps({"conversationTurns": [{
                "prompt": {"content": [{"text": row["question"]}]},
                "referenceResponses": [{"content": [{"text": row["ground_truth"]}]}],
            }]}))
    s3.put_object(Bucket=bucket, Key=key, Body="\n".join(lines).encode("utf-8"))
    return f"s3://{bucket}/{key}"


def submit_job(name, role_arn, kb_id, dataset_uri, output_uri, retrieval_only):
    retrieval_cfg = {"vectorSearchConfiguration": {"numberOfResults": 5}}
    if retrieval_only:
        rag = {"retrieveConfig": {
            "knowledgeBaseId": kb_id,
            "knowledgeBaseRetrievalConfiguration": retrieval_cfg}}
        metrics = RETRIEVAL_METRICS
    else:
        rag = {"retrieveAndGenerateConfig": {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": generator_arn(),
                "retrievalConfiguration": retrieval_cfg}}}
        metrics = GENERATION_METRICS
    response = bedrock.create_evaluation_job(
        jobName=name,
        roleArn=role_arn,
        applicationType="RagEvaluation",
        inferenceConfig={"ragConfigs": [{"knowledgeBaseConfig": rag}]},
        outputDataConfig={"s3Uri": output_uri},
        evaluationConfig={"automated": {
            "datasetMetricConfigs": [{
                "taskType": "QuestionAndAnswer",
                "dataset": {"name": "RagDataset", "datasetLocation": {"s3Uri": dataset_uri}},
                "metricNames": metrics,
            }],
            "evaluatorModelConfig": {"bedrockEvaluatorModels": [{"modelIdentifier": JUDGE_MODEL_ID}]},
        }},
    )
    return response["jobArn"], metrics


def poll(job_arn, interval=60, max_wait=7200):
    waited = 0
    while waited < max_wait:
        status = bedrock.get_evaluation_job(jobIdentifier=job_arn)["status"]
        print(f"  [{waited:>5}s] {job_arn.split('/')[-1]} -> {status}")
        if status in ("Completed", "Failed", "Stopped", "COMPLETED", "FAILED", "STOPPED"):
            return status.upper()
        time.sleep(interval)
        waited += interval
    return "TIMEOUT"


def _scores(record):
    """Yield (metricName, value) from either documented output shape.

    The September 2026 Managed KB lab found RAG jobs write
    conversationTurns[].results[] rather than automatedEvaluationResult.scores[].
    Results with result=null ("Unable to parse score") are skipped and counted.
    """
    for turn in record.get("conversationTurns", []):
        for res in turn.get("results", []):
            yield res.get("metricName"), res.get("result")
    for res in record.get("automatedEvaluationResult", {}).get("scores", []):
        yield res.get("metricName"), res.get("result")


def parse_results(output_uri, metrics):
    bucket, _, prefix = output_uri.replace("s3://", "").partition("/")
    sums = {m: 0.0 for m in metrics}
    counts = {m: 0 for m in metrics}
    unparsed = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".jsonl"):
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode("utf-8")
            for line in filter(str.strip, body.split("\n")):
                for name, value in _scores(json.loads(line)):
                    if name not in sums:
                        continue
                    if isinstance(value, (int, float)):
                        sums[name] += float(value)
                        counts[name] += 1
                    else:
                        unparsed += 1
    means = {m: (sums[m] / counts[m] if counts[m] else None) for m in metrics}
    return means, unparsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--terraform-output", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--eval-role-arn", required=True)
    ap.add_argument("--retrieval-metrics", action="store_true")
    ap.add_argument("--no-generation", action="store_true",
                    help="Only submit the retrieve-only job (implies --retrieval-metrics). "
                         "Cheaper way to repeat the metrics that separate strategies.")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--output-csv", default="benchmark-results.csv")
    args = ap.parse_args()

    tf = json.load(open(args.terraform_output, encoding="utf-8"))
    kb_ids = tf["knowledge_base_ids"]["value"]
    bucket = tf["corpus_bucket"]["value"]
    dataset_uri = upload_dataset(args.questions, bucket, "eval-input/questions-rag.jsonl")

    jobs = []  # (strategy, kind, run, arn, metrics, output_uri)
    stamp = int(time.time())
    for strategy, kb_id in kb_ids.items():
        with_ret = args.retrieval_metrics or args.no_generation
        kinds = ([] if args.no_generation else [("gen", False)]) + ([("ret", True)] if with_ret else [])
        for kind, retrieval_only in kinds:
            runs = args.repeats if kind == "gen" else 1
            for run in range(1, runs + 1):
                out = f"s3://{bucket}/eval-output/{strategy}/{kind}-{run}-{stamp}/"
                name = f"kb-{strategy}-{kind}-{run}-{stamp}"[:63]
                arn, metrics = submit_job(name, args.eval_role_arn, kb_id,
                                          dataset_uri, out, retrieval_only)
                print(f"submitted {name}")
                jobs.append((strategy, kind, run, arn, metrics, out))

    rows = []
    for strategy, kind, run, arn, metrics, out in jobs:
        status = poll(arn)
        means, unparsed = parse_results(out, metrics) if status == "COMPLETED" else ({}, 0)
        rows.append({"strategy": strategy, "kind": kind, "run": run,
                     "status": status, "unparsed": unparsed, **means})

    with open(args.output_csv, "w", encoding="utf-8") as f:
        keys = ["strategy", "kind", "run", "status", "unparsed"] + GENERATION_METRICS + RETRIEVAL_METRICS
        f.write(",".join(keys) + "\n")
        for r in rows:
            f.write(",".join("" if r.get(k) is None else str(r.get(k)) for k in keys) + "\n")

    print(f"\n{'strategy':<14}{'metric':<26}{'mean':>8}{'min':>8}{'max':>8}{'n':>4}")
    for strategy in kb_ids:
        for metric in GENERATION_METRICS + RETRIEVAL_METRICS:
            vals = [r[metric] for r in rows
                    if r["strategy"] == strategy and r.get(metric) is not None]
            if vals:
                print(f"{strategy:<14}{metric:<26}{statistics.mean(vals):>8.3f}"
                      f"{min(vals):>8.3f}{max(vals):>8.3f}{len(vals):>4}")
    print(f"\nWrote {args.output_csv}. Check the 'unparsed' column: judge "
          "responses the service could not score are excluded, not averaged as zero.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
