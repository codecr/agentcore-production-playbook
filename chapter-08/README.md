# Chapter 8: Bedrock Knowledge Bases

This folder accompanies Chapter 8 of *AgentCore in Production: The Operator's Playbook*.

## What's here

```
chapter-08/
├── benchmark/
│   ├── terraform/
│   │   ├── main.tf                  # 5 KBs, all 5 chunking strategies, S3 Vectors backend
│   │   └── lambda/
│   │       └── custom_chunker.py    # POST_CHUNKING Lambda used by the "custom" strategy
│   ├── questions.jsonl              # 25-question ground-truth corpus
│   └── run_evaluation.py            # Submits eval jobs, polls, aggregates results
├── monitoring/
│   └── ingestion-alarms.yaml        # Log-delivery pipeline + alarms (the runbook-incident lesson)
└── README.md                        # This file
```

## What this delivers

**A reproducible chunking benchmark.** The Terraform in `benchmark/terraform/`
provisions five Knowledge Bases differing only in `ChunkingConfiguration`:
NONE, FIXED_SIZE, HIERARCHICAL, SEMANTIC, and a fifth ("custom") that layers a
Lambda-based `customTransformationConfiguration` on top of NONE. Identical
embedding model, identical S3 Vectors backend, identical corpus. The only
variable is the chunking strategy.

**A note on "CUSTOM":** the real `ChunkingConfiguration.chunking_strategy`
enum is `FIXED_SIZE | HIERARCHICAL | SEMANTIC | NONE` — there is no `CUSTOM`
value. A Lambda-driven custom chunker is wired by setting
`chunking_strategy = NONE` (Bedrock's built-in chunker steps aside) and
attaching a `POST_CHUNKING` Lambda transformation that does the actual
splitting (`lambda/custom_chunker.py`). "Custom" in this repo's KB names,
Terraform locals, and result tables is our own label for that combination,
not a literal API value. See
[the AWS doc on custom transformation Lambdas](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-custom-transformation.html).

**A 25-question evaluation corpus** with ground-truth answers about Bedrock
Knowledge Bases itself. Each question has a precise reference response, so
LLM-as-a-judge scoring is bounded by an objective comparison rather than
free-form judgment.

**An evaluation runner** that submits Bedrock Evaluations jobs against all
five KBs in parallel, polls for completion, and aggregates the results into
a comparison table. The generator is Sonnet 4.6; the judge is Nova Pro
(cross-family, on the allowlist).

**A log-delivery pipeline and CloudWatch alarms for ingestion failures.**
This is the most important operational deliverable. The "runbook that
didn't exist" incident from Chapter 8 Section 8.1 happens when a team
configures a Knowledge Base and trusts that the documents in S3 are also in
the vector store. They are not, necessarily, and the console does not tell
you. Knowledge Base ingestion has no native `AWS/Bedrock` CloudWatch
metrics — the only observability Bedrock offers is JSON logs delivered
through the CloudWatch Logs vended-log-delivery pipeline. This template
wires that pipeline (`AWS::Logs::DeliverySource` / `DeliveryDestination` /
`Delivery`), extracts the documented log fields into custom metrics via
metric filters, and alarms on those. Three alarms catch the failure modes
that produce that specific incident.

## The expected results

When you run this benchmark on a corpus of real technical documentation
(AWS docs, internal manuals, runbooks, etc.), the results follow a specific
shape:

| Strategy | Correctness | Completeness | Helpfulness | Faithfulness |
|----------|-------------|--------------|-------------|--------------|
| custom        | **0.940** | **0.790** | 0.873     | **0.820** |
| hierarchical  | 0.920     | 0.750     | **0.887** | 0.810     |
| fixed-size    | 0.880     | 0.760     | 0.880     | 0.810     |
| none          | 0.261     | 0.210     | 0.710     | 0.228     |
| semantic      | 0.160     | 0.104     | 0.580     | 0.140     |

(Strategy labels match `local.strategies` in `main.tf` exactly — "fixed-size",
not "fixed" — so they're greppable straight from the Terraform output.)

Two groups, not a continuous ranking. The cliff between FIXED_SIZE (0.880)
and NONE (0.261) is caused by ingestion failures, not by chunking quality.
NONE and SEMANTIC simply cannot ingest typical technical documentation
because of the hard limits documented in the gotchas below.

## The eight infrastructure gotchas

Before you run anything, internalize these. Three of them have killed
deployments I've seen.

### 1. Titan v2's 50,000-character / 8,192-token per-request limit

**Symptom:** NONE chunking fails with embedding errors on most files.

**Cause:** NONE sends the full file to the embedding model. Titan v2 caps
at 50K characters or 8K tokens per call. Typical technical PDFs are 10-50×
that size.

**Fix:** NONE is only viable for pre-chunked corpora (FAQ entries, short
tickets, individual product cards). For anything larger, choose FIXED_SIZE,
HIERARCHICAL, or a Lambda-driven custom chunker (see gotcha #4 below).

### 2. SEMANTIC chunking's empirical 1 MB per-file limit

**Symptom:** Ingestion fails with
`File body text exceeds size limit of 1000000 for semantic chunking`.

**Cause:** SEMANTIC has a hard 1,000,000-character cap per file that is
not documented at the moment you select the strategy. Most real technical
documentation exceeds this.

**Fix:** If your files are reliably under 1 MB, SEMANTIC is fine. If any
are larger, switch to FIXED_SIZE or pre-split the corpus before upload.
This is the cause of the "runbook that didn't exist" incident.

### 3. S3 Vectors' 2 KB filterable-metadata limit

**Symptom:** Ingestion fails with a cryptic metadata-size error after
chunks are created.

**Cause:** S3 Vectors caps filterable metadata at 2,048 bytes per vector.
The default `AMAZON_BEDROCK_TEXT` metadata key carries the chunk content,
which can easily exceed 2 KB.

**Fix:** Declare `nonFilterableMetadataKeys` at index creation, including
at minimum `AMAZON_BEDROCK_TEXT`. In the `hashicorp/aws` provider this is
nested inside a `metadata_configuration {}` block on `aws_s3vectors_index`,
not a top-level argument — the Terraform in this folder does this correctly.
S3 Vectors indexes are also immutable: get this wrong and the fix is
`destroy`/`apply`, not an in-place update.

### 4. The Lambda custom chunker: three failure modes with near-identical errors

**Symptom:** The `custom` strategy's ingestion job fails with
`Access denied for lambda:InvokeFunction`, or the Lambda is invoked but
crashes with `ValueError: Invalid S3 URI: intermediate/.../...json`.

**Cause:** Wiring a Lambda-based chunker correctly requires three separate
fixes that produce overlapping symptoms — fixing one or two still fails,
with an error that looks like the one you just "fixed":
1. The KB role needs `lambda:InvokeFunction` on the Lambda's ARN, but
   Bedrock invokes the qualified ARN (`<arn>:$LATEST`), so the policy
   `Resource` must list both the bare and the `:*`-suffixed ARN.
2. Lambda also requires a resource-based policy (`aws_lambda_permission`)
   granting `bedrock.amazonaws.com` permission to invoke the function —
   an IAM policy on the KB role alone is not sufficient.
3. The handler's `event["inputFiles"][*]["contentBatches"][*]["key"]` is a
   bare S3 key relative to `event["bucketName"]`, not an `s3://bucket/key`
   URI. Treating it as a URI is the most common way this breaks.

**Fix:** All three are wired in `main.tf` (`aws_lambda_permission`, the
qualified-ARN resource list on the KB role policy) and
`lambda/custom_chunker.py` (reads `event["bucketName"]` + relative keys).
See https://docs.aws.amazon.com/bedrock/latest/userguide/kb-custom-transformation.html
for the full event/response contract.

### 5. Sonnet 4.6, 4.5, and Opus 4.x are NOT on the Bedrock Evaluations judge allowlist

**Symptom:** Eval job creation fails with "model not supported" when you
specify Sonnet 4.x as the judge.

**Cause:** Bedrock Evaluations maintains a fixed allowlist that has not
been updated with the newest Claude models. The allowed judges are Sonnet
3.5/3.7, Haiku 3/3.5, Nova Pro, Llama 3.1 70B, and Mistral Large.

**Fix:** Use `amazon.nova-pro-v1:0` as the judge. Cross-family judging
also reduces intra-family self-evaluation bias, so this is a feature, not
a workaround.

### 6. The IAM dual-policy requirement for Bedrock Evaluations

**Symptom:** Eval job fails with "does not have permission to call the
KB API" even though the role has the right action permissions.

**Cause:** Bedrock Evaluations needs BOTH a trust policy AND a permission
policy. A trust policy alone (or vice versa) produces the misleading
"permission" error that sends teams debugging the wrong direction.

**Fix:** `main.tf` provisions `aws_iam_role.eval_role` with both sides
configured — pass its ARN (`terraform output -raw eval_role_arn`) to
`run_evaluation.py --eval-role-arn`:
- Trust policy: allows `bedrock.amazonaws.com` to assume the role, scoped
  to `aws:SourceArn` matching `evaluation-job/*`
- Permission policy: the specific KB ARNs (not a wildcard — a wildcard
  here reproduces the same misleading error), `bedrock:InvokeModel` on the
  generator and judge models, and S3 read/write on the corpus bucket

### 7. The metric partition between evaluation jobs

**Symptom:** You configure an eval job with retrieval-only metrics
(`ContextRelevance`, `ContextCoverage`) plus generation metrics
(`Correctness`, `Faithfulness`) and the job fails or silently runs only
some of them.

**Cause:** Bedrock Evaluations partitions metrics into two categories that
cannot be combined in one job. Retrieval-only metrics require a different
job configuration from generation metrics.

**Fix:** Run two separate eval jobs per KB if you want both metric
categories. This benchmark runs only generation metrics (Correctness,
Completeness, Helpfulness, Faithfulness) for that reason.

### 8. Ingestion failures don't show prominently in the console — and there is no `AWS/Bedrock` metric to alarm on either

**Symptom:** A document exists in S3, the KB shows the data source as
"synced", but queries return "no information available" for content you
know is in that document. Looking for a native CloudWatch metric to alarm
on turns up nothing — Knowledge Base ingestion does not publish one.

**Cause:** The Bedrock console's data-source view does not flag
per-document failures. The overall sync status can be COMPLETE while
individual documents failed to process (oversized for SEMANTIC, encoding
issues, etc.). The only observability Bedrock exposes for ingestion is JSON
log events delivered through the CloudWatch Logs vended-log-delivery
pipeline — not metrics. Two event types carry everything:
`StartIngestionJob.StatusChanged` (job level, with a human-readable
`event.message` such as `Crawling completed`) and
`StartIngestionJob.ResourceStatusChanged` (per document, with
`event.connector_document_status.Status`, the per-stage
`crawl_status`/`sync_status`/`index_status`, `event.chunk_statistics`, and an
`event.error_message` that is present only on failure).

**Fix:** Deploy `monitoring/ingestion-alarms.yaml`. It provisions the
delivery pipeline itself (`AWS::Logs::DeliverySource` /
`DeliveryDestination` / `Delivery`, plus the resource policy that same-account
delivery to CloudWatch Logs requires), three `AWS::Logs::MetricFilter`
resources that turn the documented log fields into custom metrics, and
three alarms against those derived metrics: ERROR-level ingestion events
(there is no job-level "job failed" event to match on — job logs carry only a
message, so `level` is the honest signal), per-document failures via
`event.connector_document_status.Status = "FAILED"` (the field AWS's own
documented Logs Insights query uses for this), and silent rot when no job has
completed in 7 days. Deploy this stack for every KB in production — see
https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-observability.html
for the full log schema, including the exact field names. Getting a field name
wrong here fails silently: the filter never matches, the metric is never
published, and the alarm sits in INSUFFICIENT_DATA forever (or, with
`TreatMissingData: breaching`, alarms permanently). Neither `cfn-lint` nor
`validate-template` can catch that.

## How to run the benchmark

### Step 1 — Deploy the infrastructure

```bash
cd benchmark/terraform
terraform init
terraform apply
terraform output -json > output.json
```

The apply creates the corpus bucket, the S3 Vectors index, the 5 KBs,
the custom-chunker Lambda, and the IAM roles (KB ingestion role plus the
Bedrock Evaluations role). Cost during the apply itself is negligible.

### Step 2 — Upload a corpus

```bash
# Replace with your actual corpus (3-10 PDFs of technical documentation works well)
aws s3 sync ./my-corpus/ s3://$(terraform output -raw corpus_bucket)/docs/
```

### Step 3 — Trigger initial ingestion for each KB

```bash
for kb in $(terraform output -json knowledge_base_ids | jq -r 'keys[]'); do
  kb_id=$(terraform output -json knowledge_base_ids | jq -r ".$kb")
  ds_id=$(terraform output -json data_source_ids | jq -r ".$kb")
  aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "$kb_id" \
    --data-source-id "$ds_id"
done
```

Watch for failures. **This is where NONE and SEMANTIC fail.** Don't fight
those failures — they are the benchmark's expected outcome.

### Step 4 — Run the evaluation

```bash
cd ../   # back to benchmark/
python run_evaluation.py \
  --terraform-output terraform/output.json \
  --questions questions.jsonl \
  --eval-role-arn "$(cd terraform && terraform output -raw eval_role_arn)" \
  --output-csv benchmark-results.csv
```

The eval role (`aws_iam_role.eval_role` in `main.tf`) is provisioned by
the same `terraform apply` from Step 1 — see gotcha #6 above for why both
its trust policy and its permission policy matter.

Expect this to take 45-90 minutes and cost approximately $18 USD.

### Step 5 — Read the results

The CSV will look something like the table above. Inspect it. The story
is the *gap* between the top three strategies and the bottom two, and
the small margins within the top three. If your numbers match the
expected shape, the benchmark replicated successfully.

## Deploy the monitoring before going to production

Even if you don't run the full benchmark, **deploy the monitoring stack for
every KB in production**. `monitoring/ingestion-alarms.yaml` provisions the
CloudWatch Logs vended-log-delivery pipeline for that KB (there is no native
metric to alarm on — see gotcha #8) plus the three alarms. Stack it per-KB:

```bash
aws cloudformation deploy \
  --template-file monitoring/ingestion-alarms.yaml \
  --stack-name kb-monitoring-acme-docs \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    KnowledgeBaseId=ABCD1234EFGH \
    KnowledgeBaseName=acme-docs \
    NotificationTopicArn=arn:aws:sns:us-east-1:111122223333:platform-alerts \
    Environment=production
```

The deploying principal needs `logs:PutDeliverySource`,
`logs:PutDeliveryDestination`, `logs:CreateDelivery`,
`logs:PutResourcePolicy`, and `bedrock:AllowVendedLogDeliveryForResource`
in addition to normal CloudFormation permissions — these are one-time
delivery-pipeline setup calls the stack's own resources can't grant
themselves.

Skipping this is Chapter 8 anti-pattern #3 and the cause of incidents
like the one in Section 8.1.

## Reference

The original benchmark and the Terraform with full debug instrumentation
lives at https://github.com/codecr/bedrock-chunking-benchmark — the version
in this folder is a curated subset focused on the book's narrative.

## License

Code and configurations in this folder are MIT licensed. The accompanying
book chapter is copyrighted; the code is explicitly intended for reuse.
