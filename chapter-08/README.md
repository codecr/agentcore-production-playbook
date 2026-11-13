# Chapter 8: Bedrock Knowledge Bases

This folder accompanies Chapter 8 of *AgentCore in Production: The Operator's Playbook*, 2nd edition (October 2026).

## What changed since the first edition

| Area | First edition | Now |
|---|---|---|
| Eval role IAM actions | `bedrock-agent-runtime:Retrieve` / `:RetrieveAndGenerate` (not IAM actions) | `bedrock:Retrieve` on the KB ARNs, `bedrock:RetrieveAndGenerate` on `*` (no resource-level support), `bedrock:InvokeModel` on the inference profile and its foundation models |
| `run_evaluation.py` | Model-evaluation job with pre-generated answers, unprefixed metric names | Native `RagEvaluation` jobs with `Builtin.*` metrics; optional retrieve-only job (`--retrieval-metrics`); `--repeats N` with mean and range |
| Judge allowlist (gotcha 4) | Sonnet 4.x and Opus 4.x excluded | Resolved: Sonnet 4.5/4.6 and Opus 4.5-4.8 allowed. Nova Pro kept as a cross-family judge |
| Ingestion alarms | Filters matched only the *managed* KB log schema | Filters match both the classic and the managed schema; dashboard referenced a metric that no stack published (fixed) |
| Retrieval example | None | `retrieval/retrieve_for_tenant.py`: tenant filter plus reranking, no hybrid (S3 Vectors does not support it) |
| Managed Knowledge Base | Did not exist | Chapter 8 Section 8.5; the comparison code is at github.com/codecr/mkb-vs-chunking |

**Status.** Run against a live account on 2026-11-11: the Terraform, the ingestion of all five Knowledge Bases, `run_evaluation.py`, `run_baseline.py`, and the benchmark in Section 8.4 (Table 8.2). Not run: `retrieve_for_tenant.py`, the ingestion alarms, and the Managed Knowledge Base comparison.

**Numbering.** The gotchas below use the book's numbering (Section 8.6): eight, with the Lambda custom chunker as number 8.

## What's here

```
chapter-08/
├── benchmark/
│   ├── terraform/
│   │   ├── main.tf                  # 5 KBs, all 5 chunking strategies, S3 Vectors backend
│   │   └── lambda/
│   │       └── custom_chunker.py    # POST_CHUNKING Lambda used by the "custom" strategy
│   ├── questions.jsonl              # 25-question ground-truth corpus (April 2026, public docs)
│   ├── synthetic-corpus/            # 6 documents of invented facts, fact table, sha256 (November 2026 re-run)
│   ├── questions-synthetic.jsonl    # 25 questions over the synthetic corpus
│   ├── run_evaluation.py            # Submits RAG eval jobs, polls, aggregates results
│   ├── run_baseline.py              # No-retrieval baseline row (generator alone, same judge)
│   └── results-2026-11-*.csv        # Raw results of the re-run in Section 8.4, Table 8.2
├── retrieval/
│   └── retrieve_for_tenant.py       # The Retrieve call from Section 8.7
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

**An evaluation runner** that submits native Bedrock RAG evaluation jobs
against all five KBs, polls for completion, and aggregates the results into
a comparison table, optionally with a retrieve-only job and repeated runs.
The generator is Sonnet 4.6; the judge is Nova Pro (cross-family).

**The November 2026 re-run (Section 8.4, Table 8.2).** `synthetic-corpus/` holds six
documents of invented, verifiable facts (a fact table and a sha256 are included), so the
generator cannot answer from memory: with no retrieval it scored 0.02 on Correctness
(`run_baseline.py`). To reproduce: apply the Terraform, copy `synthetic-corpus/*.md` to
`s3://<corpus bucket>/corpus/` (the data sources index only that prefix), ingest the five
Knowledge Bases, then run `run_evaluation.py --questions questions-synthetic.jsonl
--retrieval-metrics --repeats 2` and `run_baseline.py`. The run cost about $4.5 with
Nova 2 Lite as generator and Nova Pro as judge; the retrieve-only job alone
(`--no-generation`) costs about a fifth of a full repetition. Claude 5.x models cannot
be the generator in these jobs (see the header of `run_evaluation.py`).

**A log-delivery pipeline and CloudWatch alarms for ingestion failures.**
This is the most important operational deliverable. The "runbook that
didn't exist" incident from Chapter 8 Section 8.1 happens when a team
configures a Knowledge Base and trusts that the documents in S3 are also in
the vector store. They are not, necessarily, and the console does not tell
you. Knowledge Base ingestion has no native `AWS/Bedrock` CloudWatch
metrics (the first edition of the book named two that do not exist) — the
only observability Bedrock offers is JSON logs delivered through the
CloudWatch Logs vended-log-delivery pipeline, plus the statistics in
`GetIngestionJob`. This template
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

Read these numbers with the limits Section 8.4 now states: the corpus was
public AWS documentation the generator already knew (NONE scored 0.261
while indexing one of three files), the run was 3 documents and 25
questions once, semantic chunking ingested only one document, and the
retrieval metrics were not run. NONE and SEMANTIC each indexed the HTML
file only (1 of 3), not zero.

Two groups, not a continuous ranking. The cliff between FIXED_SIZE (0.880)
and NONE (0.261) is caused by ingestion failures, not by chunking quality.
NONE and SEMANTIC simply cannot ingest typical technical documentation
because of the hard limits documented in the gotchas below.

## The eight infrastructure gotchas

Before running anything, read these.

### 1. Titan v2's 50,000-character / 8,192-token per-request limit

**Symptom:** NONE chunking fails with embedding errors on most files.

**Cause:** NONE sends the full file to the embedding model. Titan v2 caps
at 50K characters or 8K tokens per call. Typical technical PDFs are 10-50×
that size.

**Fix:** NONE is only viable for pre-chunked corpora (FAQ entries, short
tickets, individual product cards). For anything larger, choose FIXED_SIZE,
HIERARCHICAL, or a Lambda-driven custom chunker (see gotcha #8 below).

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

### 4. The judge allowlist (resolved in 2026)

**Was:** In April 2026, Sonnet 4.5/4.6 and Opus 4.x were rejected as judges
("model(s) ... are not supported").

**Now:** The Bedrock Evaluations judge list includes Claude Sonnet 4.5 and
4.6 and Opus 4.5 through 4.8 (plus Haiku 4.5, Nova 2 Lite, and others);
check the current list at
https://docs.aws.amazon.com/bedrock/latest/userguide/evaluation-judge.html.
Two notes still apply: the console offers inference profiles that the job
later rejects, and a Legacy model unused for 30 days is denied even when
listed.

**Recommendation:** keep `amazon.nova-pro-v1:0` when the generator is
Claude. A judge from another model family avoids self-preference bias.

### 5. The IAM dual-policy requirement for Bedrock Evaluations

**Symptom:** Eval job fails with "does not have permission to call the
KB API" even though the role has the right action permissions.

**Cause:** Like every service role, the evaluation role needs both a trust
policy and a permission policy. The trap is that a fault on either side
produces the same message, which names only the permission side. A third
cause produces it too (live-tested 2026-11-11): a role whose `InvokeModel`
covers a different generator than the one the job names. The job fails with
"does not have permission to call the Knowledge Base API" although the
Knowledge Base permissions are fine. Keep `generator_model_id` in `main.tf`
equal to `GENERATOR_MODEL_ID` in `run_evaluation.py`.

**Fix:** `main.tf` provisions `aws_iam_role.eval_role` with both sides
configured — pass its ARN (`terraform output -raw eval_role_arn`) to
`run_evaluation.py --eval-role-arn`:
- Trust policy: allows `bedrock.amazonaws.com` to assume the role, scoped
  to `aws:SourceArn` matching `evaluation-job/*`
- Permission policy: `bedrock:Retrieve` on the specific KB ARNs (not a
  wildcard — a wildcard here reproduced the same misleading error),
  `bedrock:RetrieveAndGenerate` on `"*"` (it does not support
  resource-level permissions), `bedrock:InvokeModel` on the generator's
  inference profile, the foundation models behind it, and the judge, and S3
  read/write on the corpus bucket. The first edition used
  `bedrock-agent-runtime:*` action names, which IAM does not recognize.

### 6. The metric partition between evaluation jobs

**Symptom:** You configure an eval job with retrieval-only metrics
(`ContextRelevance`, `ContextCoverage`) plus generation metrics
(`Correctness`, `Faithfulness`) and the job fails or silently runs only
some of them.

**Cause:** Bedrock Evaluations partitions metrics into two categories that
cannot be combined in one job. Retrieval-only metrics require a different
job configuration from generation metrics.

**Fix:** Run two separate eval jobs per KB. `run_evaluation.py
--retrieval-metrics` submits the retrieve-only job alongside the
generation job. The April 2026 numbers come from the generation job only.

### 7. Ingestion failures don't show prominently in the console — and there is no `AWS/Bedrock` metric to alarm on either

**Symptom:** A document exists in S3, the KB shows the data source as
"synced", but queries return "no information available" for content you
know is in that document. Looking for a native CloudWatch metric to alarm
on turns up nothing — Knowledge Base ingestion does not publish one.

**Cause:** The Bedrock console's data-source view does not flag
per-document failures. The overall sync status can be COMPLETE while
individual documents failed to process (oversized for SEMANTIC, encoding
issues, etc.). The only observability Bedrock exposes for ingestion is JSON
log events delivered through the CloudWatch Logs vended-log-delivery
pipeline — not metrics. Two event types carry everything, in two
documented schemas. Classic (self-configured) KBs
([knowledge-bases-logging.html](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-bases-logging.html)):
`StartIngestionJob.StatusChanged` with `event.ingestion_job_status` and
`event.resource_statistics.number_of_resources_failed`, and
`StartIngestionJob.ResourceStatusChanged` with `event.status` (`FAILED`,
`EMBEDDING_FAILED`, `INDEXING_FAILED`, `PARTIALLY_INDEXED`,
`RESOURCE_IGNORED`, ...) and `event.status_reasons`. Managed KBs
([kb-managed-observability.html](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-observability.html)):
a job-level `event.message` such as `Crawling completed`, and per document
`event.connector_document_status.Status` plus `event.error_message`.

**Fix:** Deploy `monitoring/ingestion-alarms.yaml`. It provisions the
delivery pipeline itself (`AWS::Logs::DeliverySource` /
`DeliveryDestination` / `Delivery`, plus the resource policy that same-account
delivery to CloudWatch Logs requires), three `AWS::Logs::MetricFilter`
resources that turn the documented log fields of either schema into custom
metrics, and three alarms against those derived metrics: ERROR-level events
or a classic job ending `FAILED`; per-document failures or ignored documents;
and silent rot when no job has completed in 7 days. Deploy this stack for
every KB in production. Getting a field name
wrong here fails silently: the filter never matches, the metric is never
published, and the alarm sits in INSUFFICIENT_DATA forever (or, with
`TreatMissingData: breaching`, alarms permanently). Neither `cfn-lint` nor
`validate-template` can catch that.

### 8. The Lambda custom chunker: three failure modes with near-identical errors

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
  --retrieval-metrics --repeats 3 \
  --output-csv benchmark-results.csv
```

For a run that answers the book's own criticism of the April benchmark
(Section 8.4): use a corpus the generator cannot
know (internal or synthetic documents), pre-split files below 1,000,000
characters so SEMANTIC ingests everything, and add a no-retrieval baseline
by asking the same questions to the generator with no Knowledge Base.

The eval role (`aws_iam_role.eval_role` in `main.tf`) is provisioned by
the same `terraform apply` from Step 1 — see gotcha #5 above for why both
its trust policy and its permission policy matter.

The single generation pass in April took 45-90 minutes and cost about
$18-20. Retrieval jobs and repeats multiply both.

### Step 5 — Read the results

The CSV will look something like the table above. Inspect it. The story
is the *gap* between the top three strategies and the bottom two, and
the small margins within the top three. If your numbers match the
expected shape, the benchmark replicated successfully.

## Deploy the monitoring before going to production

Even if you don't run the full benchmark, **deploy the monitoring stack for
every KB in production**. `monitoring/ingestion-alarms.yaml` provisions the
CloudWatch Logs vended-log-delivery pipeline for that KB (there is no native
metric to alarm on — see gotcha #7) plus the three alarms. Stack it per-KB:

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

Skipping this is book gotcha seven (Section 8.6) and the cause of incidents
like the one in Section 8.1.

## Reference

The original benchmark and the Terraform with full debug instrumentation
lives at https://github.com/codecr/bedrock-chunking-benchmark — the version
in this folder is a curated subset focused on the book's narrative.

## License

Code and configurations in this folder are MIT licensed. The accompanying
book chapter is copyrighted; the code is explicitly intended for reuse.
