"""
run_baseline.py - the no-retrieval baseline row for the chunking benchmark.

Sends the same questions to the generator alone (no Knowledge Base) and scores
the answers with the same judge, so the benchmark can show how much of a score
comes from retrieval and how much from what the model already knows.

Run against the synthetic corpus in synthetic-corpus/ (invented facts), the
baseline scored Correctness 0.02 and Completeness 0.25 (2026-11-11, CT-08-10).

Usage:
    python run_baseline.py --terraform-output terraform/output.json \
        --questions questions-synthetic.jsonl \
        --eval-role-arn "$(cd terraform && terraform output -raw eval_role_arn)"

This is a ModelEvaluation job, so the task type is "General" (QuestionAndAnswer is
rejected for model-as-judge jobs) and Faithfulness, which needs retrieved context,
is not scored. Generator and judge come from GENERATOR_MODEL_ID and JUDGE_MODEL_ID,
as in run_evaluation.py; the eval role's InvokeModel must cover both.
"""

import argparse
import json
import os
import sys
import time

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
GENERATOR_PROFILE_ID = os.environ.get("GENERATOR_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
JUDGE_MODEL_ID = os.environ.get("JUDGE_MODEL_ID", "amazon.nova-pro-v1:0")
METRICS = ["Builtin.Correctness", "Builtin.Completeness", "Builtin.Helpfulness"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--terraform-output", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--eval-role-arn", required=True)
    args = ap.parse_args()

    bucket = json.load(open(args.terraform_output, encoding="utf-8"))["corpus_bucket"]["value"]
    s3 = boto3.client("s3", region_name=AWS_REGION)
    bedrock = boto3.client("bedrock", region_name=AWS_REGION)
    account = boto3.client("sts").get_caller_identity()["Account"]

    rows = [json.loads(line) for line in open(args.questions, encoding="utf-8") if line.strip()]
    body = "\n".join(json.dumps({"prompt": r["question"], "referenceResponse": r["ground_truth"]}) for r in rows)
    dataset_key = "eval-input/questions-model.jsonl"   # outside corpus/, which the data sources index
    s3.put_object(Bucket=bucket, Key=dataset_key, Body=body.encode("utf-8"))

    stamp = int(time.time())
    output_uri = f"s3://{bucket}/eval-output/baseline/{stamp}/"
    job = bedrock.create_evaluation_job(
        jobName=f"baseline-nokb-{stamp}",
        roleArn=args.eval_role_arn,
        applicationType="ModelEvaluation",
        inferenceConfig={"models": [{"bedrockModel": {"modelIdentifier":
            f"arn:aws:bedrock:{AWS_REGION}:{account}:inference-profile/{GENERATOR_PROFILE_ID}"}}]},
        outputDataConfig={"s3Uri": output_uri},
        evaluationConfig={"automated": {
            "datasetMetricConfigs": [{
                "taskType": "General",
                "dataset": {"name": "BaselineDataset",
                            "datasetLocation": {"s3Uri": f"s3://{bucket}/{dataset_key}"}},
                "metricNames": METRICS}],
            "evaluatorModelConfig": {"bedrockEvaluatorModels": [{"modelIdentifier": JUDGE_MODEL_ID}]}}},
    )
    arn = job["jobArn"]
    print("submitted", arn.split("/")[-1])
    while True:
        status = bedrock.get_evaluation_job(jobIdentifier=arn)["status"].upper()
        print("  ", status)
        if status in ("COMPLETED", "FAILED", "STOPPED"):
            break
        time.sleep(60)
    if status != "COMPLETED":
        print(bedrock.get_evaluation_job(jobIdentifier=arn).get("failureMessages"))
        return 1

    bucket_out, _, prefix = output_uri.replace("s3://", "").partition("/")
    sums, counts = {}, {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket_out, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".jsonl"):
                continue
            for line in s3.get_object(Bucket=bucket_out, Key=obj["Key"])["Body"].read().decode().split("\n"):
                if not line.strip():
                    continue
                record = json.loads(line)
                scores = [r for t in record.get("conversationTurns", []) for r in t.get("results", [])]
                scores += record.get("automatedEvaluationResult", {}).get("scores", [])
                for res in scores:
                    if isinstance(res.get("result"), (int, float)):
                        name = res["metricName"]
                        sums[name] = sums.get(name, 0.0) + res["result"]
                        counts[name] = counts.get(name, 0) + 1
    for name in METRICS:
        if counts.get(name):
            print(f"{name:<24}{sums[name] / counts[name]:>7.3f}  (n={counts[name]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
