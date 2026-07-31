"""
run_evaluation.py — Run the chunking benchmark against the 5 deployed KBs.

Accompanies Chapter 8 of "AgentCore in Production: The Operator's Playbook".

What this script does:
    For each of the 5 deployed Knowledge Bases (none, fixed-size, hierarchical,
    semantic, custom), this script submits a Bedrock Evaluations job using the
    25-question ground-truth corpus. The judge is Nova Pro (cross-family,
    on the allowlist — see gotcha #4 in Chapter 8 Section 8.5).

    Each job evaluates four generation metrics:
        - Correctness   (does the answer match ground truth)
        - Completeness  (does it cover all aspects of the question)
        - Helpfulness   (well-written and useful — see gotcha re using this for chunking)
        - Faithfulness  (grounded in retrieved context)

    Results are polled until completion and aggregated into a comparison table.

Usage:
    # Configure once
    export AWS_REGION=us-east-1
    export CORPUS_BUCKET=<from terraform output>

    # Run (KB IDs read from terraform output JSON)
    python run_evaluation.py --terraform-output ../terraform/output.json --questions questions.jsonl \
        --eval-role-arn arn:aws:iam::111122223333:role/kb-chunking-benchmark-eval-role

The expected wall-clock time for a full benchmark is approximately 45-90
minutes, depending on Bedrock job queue depth. Total cost is approximately
$18 USD assuming 25 questions × 5 KBs.

Requires:
    pip install boto3
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional

import boto3
from botocore.exceptions import ClientError


# =============================================================================
# Configuration
# =============================================================================

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# The generator model produces answers from retrieved chunks.
# Sonnet 4.6 is a strong default — high quality, reasonable cost.
GENERATOR_MODEL_ID = os.environ.get(
    "GENERATOR_MODEL_ID",
    "us.anthropic.claude-sonnet-4-6"
)

# The judge model evaluates generator outputs against ground truth.
# Nova Pro because:
#   1. It is on the Bedrock Evaluations allowlist (Sonnet 4.x is not — gotcha #4)
#   2. Cross-family judging reduces intra-family self-evaluation bias
JUDGE_MODEL_ID = os.environ.get("JUDGE_MODEL_ID", "amazon.nova-pro-v1:0")

# Generation metrics. Retrieval-only metrics (ContextRelevance, ContextCoverage)
# CANNOT be combined with these in a single eval job — see gotcha #6.
METRICS = ["Correctness", "Completeness", "Helpfulness", "Faithfulness"]


bedrock = boto3.client("bedrock", region_name=AWS_REGION)
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class StrategyResult:
    """Aggregated results for one chunking strategy."""
    strategy: str
    kb_id: str
    job_arn: Optional[str] = None
    status: str = "PENDING"
    correctness: Optional[float] = None
    completeness: Optional[float] = None
    helpfulness: Optional[float] = None
    faithfulness: Optional[float] = None
    error: Optional[str] = None


# =============================================================================
# Upload prompt dataset to S3 (Bedrock Evaluations input format)
# =============================================================================

def build_prompt_dataset(questions_file: str, kb_id: str, output_bucket: str, output_key: str) -> str:
    """Convert questions.jsonl into the format Bedrock Evaluations expects.

    Each line is a JSON object with the prompt, ground truth, and the KB ARN
    to retrieve from. The runner generates a response per prompt using
    retrieve_and_generate, then the eval job scores those responses.

    Returns the S3 URI of the uploaded dataset.
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)
    lines = []
    with open(questions_file) as f:
        for line in f:
            row = json.loads(line)
            # Generate the candidate answer using the KB
            answer = retrieve_and_generate_one(row["question"], kb_id)
            lines.append(json.dumps({
                "prompt": {"content": [{"text": row["question"]}]},
                "referenceResponses": [{"content": [{"text": row["ground_truth"]}]}],
                "category": "kb-chunking-benchmark",
                "responses": [{"response": answer, "modelIdentifier": GENERATOR_MODEL_ID}],
            }))

    s3.put_object(
        Bucket=output_bucket,
        Key=output_key,
        Body="\n".join(lines).encode("utf-8"),
    )
    return f"s3://{output_bucket}/{output_key}"


def retrieve_and_generate_one(question: str, kb_id: str) -> str:
    """Generate a response for one question against one KB."""
    try:
        response = bedrock_agent_runtime.retrieve_and_generate(
            input={"text": question},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": kb_id,
                    "modelArn": f"arn:aws:bedrock:{AWS_REGION}::foundation-model/{GENERATOR_MODEL_ID}",
                },
            },
        )
        return response["output"]["text"]
    except ClientError as exc:
        return f"[ERROR generating response: {exc}]"


# =============================================================================
# Submit evaluation job
# =============================================================================

def submit_eval_job(
    strategy: str,
    kb_id: str,
    dataset_s3_uri: str,
    output_s3_uri: str,
    role_arn: str,
) -> str:
    """Submit a Bedrock Evaluations job for one strategy. Returns the job ARN."""
    response = bedrock.create_evaluation_job(
        jobName=f"kb-benchmark-{strategy}-{int(time.time())}",
        roleArn=role_arn,
        evaluationConfig={
            "automated": {
                "datasetMetricConfigs": [{
                    "taskType": "QuestionAndAnswer",
                    "dataset": {"name": "Custom", "datasetLocation": {"s3Uri": dataset_s3_uri}},
                    "metricNames": METRICS,
                }],
                "evaluatorModelConfig": {
                    "bedrockEvaluatorModels": [{"modelIdentifier": JUDGE_MODEL_ID}]
                },
            }
        },
        inferenceConfig={
            "models": [{"bedrockModel": {
                "modelIdentifier": GENERATOR_MODEL_ID,
                "inferenceParams": '{"temperature": 0.0, "maxTokens": 1024}',
            }}]
        },
        outputDataConfig={"s3Uri": output_s3_uri},
    )
    return response["jobArn"]


# =============================================================================
# Poll job completion
# =============================================================================

def poll_job(job_arn: str, poll_interval: int = 60, max_wait: int = 7200) -> str:
    """Poll until the job reaches a terminal status. Returns final status."""
    elapsed = 0
    while elapsed < max_wait:
        response = bedrock.get_evaluation_job(jobIdentifier=job_arn)
        status = response["status"]
        print(f"  [{elapsed}s] {job_arn.split('/')[-1]} → {status}")
        if status in ("COMPLETED", "FAILED", "STOPPED"):
            return status
        time.sleep(poll_interval)
        elapsed += poll_interval
    return "TIMEOUT"


# =============================================================================
# Parse results from S3
# =============================================================================

def parse_job_results(output_s3_uri: str, strategy: str) -> dict:
    """Read the evaluation output from S3 and aggregate per-metric averages.

    The real output layout (confirmed against
    https://docs.aws.amazon.com/bedrock/latest/userguide/model-evaluation-report-s3.html):
    completed jobs write one JSON-line file per metric under a "datasets/"
    directory (e.g. "datasets/Correctness_input-dataset.jsonl"), and each
    line nests the score under "automatedEvaluationResult.scores[]" —
    {"metricName": ..., "result": ...} — NOT a flat {"metricName", "score"}
    record. Reading the flat shape silently matches nothing and every
    average comes back None.
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)
    # Output URI looks like s3://bucket/prefix/jobs/<jobId>/...
    # The results file is typically jobs/<jobId>/datasets/<metric>_input-dataset.jsonl
    bucket = output_s3_uri.replace("s3://", "").split("/")[0]
    prefix = "/".join(output_s3_uri.replace("s3://", "").split("/")[1:])

    metric_sums = {m: 0.0 for m in METRICS}
    metric_counts = {m: 0 for m in METRICS}

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".jsonl"):
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode("utf-8")
            for line in body.split("\n"):
                if not line.strip():
                    continue
                rec = json.loads(line)
                scores = rec.get("automatedEvaluationResult", {}).get("scores", [])
                for score in scores:
                    metric_name = score.get("metricName")
                    value = score.get("result")
                    if metric_name in METRICS and isinstance(value, (int, float)):
                        metric_sums[metric_name] += float(value)
                        metric_counts[metric_name] += 1

    averages = {}
    for m in METRICS:
        averages[m.lower()] = (
            metric_sums[m] / metric_counts[m] if metric_counts[m] > 0 else None
        )
    return averages


# =============================================================================
# Main flow
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform-output", required=True,
                        help="Path to terraform output JSON with knowledge_base_ids and corpus_bucket.")
    parser.add_argument("--questions", required=True,
                        help="Path to questions.jsonl with the 25-question corpus.")
    parser.add_argument("--eval-role-arn", required=True,
                        help="IAM role ARN for the evaluation job (see Chapter 8 Section 8.5 gotcha #5).")
    parser.add_argument("--output-csv", default="benchmark-results.csv",
                        help="Where to write the comparison table.")
    args = parser.parse_args()

    with open(args.terraform_output) as f:
        tf_out = json.load(f)
    kb_ids = tf_out["knowledge_base_ids"]["value"]
    corpus_bucket = tf_out["corpus_bucket"]["value"]

    results: list[StrategyResult] = []

    # Phase 1: submit all jobs
    print("Phase 1: submitting eval jobs for all 5 strategies...")
    for strategy, kb_id in kb_ids.items():
        print(f"\n--- Strategy: {strategy} (KB: {kb_id}) ---")
        result = StrategyResult(strategy=strategy, kb_id=kb_id)
        try:
            dataset_uri = build_prompt_dataset(
                questions_file=args.questions,
                kb_id=kb_id,
                output_bucket=corpus_bucket,
                output_key=f"eval-input/{strategy}.jsonl",
            )
            output_uri = f"s3://{corpus_bucket}/eval-output/{strategy}/"
            result.job_arn = submit_eval_job(
                strategy=strategy,
                kb_id=kb_id,
                dataset_s3_uri=dataset_uri,
                output_s3_uri=output_uri,
                role_arn=args.eval_role_arn,
            )
            result.status = "SUBMITTED"
            print(f"  Submitted: {result.job_arn}")
        except Exception as exc:
            result.error = str(exc)
            result.status = "ERROR"
            print(f"  ERROR: {exc}")
        results.append(result)

    # Phase 2: poll for completion
    print("\nPhase 2: polling for completion (this may take 45-90 minutes)...")
    for result in results:
        if result.job_arn:
            result.status = poll_job(result.job_arn)
            if result.status == "COMPLETED":
                averages = parse_job_results(
                    output_s3_uri=f"s3://{corpus_bucket}/eval-output/{result.strategy}/",
                    strategy=result.strategy,
                )
                result.correctness = averages.get("correctness")
                result.completeness = averages.get("completeness")
                result.helpfulness = averages.get("helpfulness")
                result.faithfulness = averages.get("faithfulness")

    # Phase 3: write comparison table
    print("\nPhase 3: writing comparison table...")
    print(f"\n{'Strategy':<14} {'Correctness':>11} {'Completeness':>12} {'Helpfulness':>11} {'Faithfulness':>12}")
    print("-" * 65)
    with open(args.output_csv, "w") as f:
        f.write("strategy,correctness,completeness,helpfulness,faithfulness,status\n")
        for r in results:
            def fmt(v): return f"{v:.3f}" if v is not None else "  -  "
            row = f"{r.strategy:<14} {fmt(r.correctness):>11} {fmt(r.completeness):>12} {fmt(r.helpfulness):>11} {fmt(r.faithfulness):>12}"
            print(row)
            f.write(f"{r.strategy},{r.correctness or ''},{r.completeness or ''},"
                    f"{r.helpfulness or ''},{r.faithfulness or ''},{r.status}\n")

    print(f"\nResults written to {args.output_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
