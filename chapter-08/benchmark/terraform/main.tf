# =============================================================================
# Bedrock Knowledge Bases Chunking Benchmark — Terraform
# =============================================================================
# Accompanies Chapter 8 of "AgentCore in Production: The Operator's Playbook".
#
# What this provisions:
#   - 1 S3 bucket for the document corpus
#   - 1 S3 Vectors bucket and index (the cost-efficient backend)
#   - 5 Knowledge Bases, identical except for ChunkingConfiguration:
#       1. KB-none           (NONE chunking)
#       2. KB-fixed-size     (FIXED_SIZE, max_tokens=512, overlap=20%)
#       3. KB-hierarchical   (HIERARCHICAL, parent=1500, child=300)
#       4. KB-semantic       (SEMANTIC, breakpoint=95th percentile)
#       5. KB-custom         (NONE chunking + Lambda POST_CHUNKING transformation —
#                             there is no CUSTOM chunking_strategy value in the API)
#   - IAM roles for KB ingestion and Bedrock Evaluations
#   - CloudWatch alarms for ingestion failures (the runbook-incident lesson)
#
# Expected cost to run the full benchmark: ~$18 USD (mostly Sonnet 4.6
# inference for the 25-question generator pass, plus Nova Pro judge).
# S3 Vectors keeps storage cost near zero.
#
# Reference for the complete project (with debug instrumentation and the
# original benchmark from gerardo.dev): https://github.com/codecr/bedrock-chunking-benchmark
# =============================================================================

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.70"   # S3 Vectors support
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# -----------------------------------------------------------------------------
# Variables
# -----------------------------------------------------------------------------

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "benchmark_name" {
  type    = string
  default = "kb-chunking-benchmark"
}

variable "embedding_model_arn" {
  type    = string
  default = "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
}

# Matches GENERATOR_MODEL_ID / JUDGE_MODEL_ID in run_evaluation.py — kept as separate
# variables (not hardcoded ARNs) so the eval IAM role's permission policy stays in
# sync with whatever models the benchmark run actually invokes.
variable "generator_model_id" {
  type    = string
  default = "us.anthropic.claude-sonnet-4-6"
}

variable "judge_model_id" {
  type    = string
  default = "amazon.nova-pro-v1:0"
}

# -----------------------------------------------------------------------------
# S3 bucket for the corpus (shared by all 5 KBs)
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "corpus" {
  bucket = "${var.benchmark_name}-corpus-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "corpus" {
  bucket = aws_s3_bucket.corpus.id
  versioning_configuration { status = "Enabled" }
}

# -----------------------------------------------------------------------------
# S3 Vectors backend (one bucket, one index per KB)
# -----------------------------------------------------------------------------
# IMPORTANT — Gotcha #3 from Chapter 8 Section 8.5: declare nonFilterableMetadataKeys
# at index creation to avoid the 2KB filterable-metadata cap killing ingestion.

resource "aws_s3vectors_vector_bucket" "main" {
  vector_bucket_name = "${var.benchmark_name}-vectors"
}

locals {
  strategies = {
    "none"         = { chunking_strategy = "NONE",         fixed_size_config = null,  hierarchical_config = null, semantic_config = null, custom_lambda = null }
    "fixed-size"   = { chunking_strategy = "FIXED_SIZE",   fixed_size_config = { max_tokens = 512, overlap_percentage = 20 }, hierarchical_config = null, semantic_config = null, custom_lambda = null }
    "hierarchical" = { chunking_strategy = "HIERARCHICAL", fixed_size_config = null, hierarchical_config = { parent_max_tokens = 1500, child_max_tokens = 300, overlap_tokens = 60 }, semantic_config = null, custom_lambda = null }
    "semantic"     = { chunking_strategy = "SEMANTIC",     fixed_size_config = null, hierarchical_config = null, semantic_config = { max_tokens = 300, buffer_size = 1, breakpoint_percentile_threshold = 95 }, custom_lambda = null }
    # "custom" is a label for OUR benchmark, not a real ChunkingConfiguration.chunking_strategy
    # value. The real API enum is FIXED_SIZE | HIERARCHICAL | SEMANTIC | NONE — there is no CUSTOM
    # value. A Lambda-driven custom chunker is wired by setting chunking_strategy = NONE (Bedrock's
    # built-in chunker steps aside and treats the whole file as a single pre-chunk candidate) and
    # attaching a custom_transformation_configuration POST_CHUNKING Lambda step that reads that
    # candidate and writes back the real chunks. See
    # https://docs.aws.amazon.com/bedrock/latest/userguide/kb-custom-transformation.html
    "custom"       = { chunking_strategy = "NONE",         fixed_size_config = null, hierarchical_config = null, semantic_config = null, custom_lambda = aws_lambda_function.custom_chunker.arn }
  }
}

resource "aws_s3vectors_index" "kb" {
  for_each = local.strategies

  vector_bucket_name = aws_s3vectors_vector_bucket.main.vector_bucket_name
  index_name         = "kb-${each.key}"
  dimension          = 1024
  # GOTCHA — the S3 Vectors provider enums are lowercase ("cosine", "float32"),
  # unlike the Bedrock KB embedding_data_type below ("FLOAT32"). Mixing the two
  # up is an easy, silent-until-apply mistake since both are just strings.
  distance_metric    = "cosine"
  data_type          = "float32"

  # GOTCHA #3 — Without this, the 2KB filterable-metadata cap will silently
  # kill ingestion when chunks carry mid-size metadata like AMAZON_BEDROCK_TEXT.
  # non_filterable_metadata_keys nests inside metadata_configuration {} — it is
  # not a top-level index argument.
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

# -----------------------------------------------------------------------------
# IAM role for Knowledge Base ingestion
# -----------------------------------------------------------------------------

resource "aws_iam_role" "kb_role" {
  name = "${var.benchmark_name}-kb-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Service = "bedrock.amazonaws.com" },
      Action = "sts:AssumeRole",
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "kb_role_inline" {
  role = aws_iam_role.kb_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject","s3:ListBucket"], Resource = [aws_s3_bucket.corpus.arn, "${aws_s3_bucket.corpus.arn}/*"] },
      { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = [var.embedding_model_arn] },
      # Scoped to the one vector bucket this benchmark provisions, not Resource: "*".
      { Effect = "Allow", Action = ["s3vectors:*"], Resource = [
          aws_s3vectors_vector_bucket.main.vector_bucket_arn,
          "${aws_s3vectors_vector_bucket.main.vector_bucket_arn}/*"
        ]
      },
      # GOTCHA #2 (blog Problem 2) — Bedrock invokes the Lambda using the qualified
      # ARN (<arn>:$LATEST), not the base ARN. Both must be listed or IAM won't match.
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [
          aws_lambda_function.custom_chunker.arn,
          "${aws_lambda_function.custom_chunker.arn}:*"
        ]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Custom chunker Lambda (used by KB-custom only)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "custom_chunker" {
  name = "${var.benchmark_name}-custom-chunker"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Service = "lambda.amazonaws.com" },
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "custom_chunker_basic" {
  role       = aws_iam_role.custom_chunker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "archive_file" "custom_chunker_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/custom_chunker.py"
  output_path = "${path.module}/lambda/custom_chunker.zip"
}

resource "aws_lambda_function" "custom_chunker" {
  function_name    = "${var.benchmark_name}-custom-chunker"
  role             = aws_iam_role.custom_chunker.arn
  runtime          = "python3.12"
  handler          = "custom_chunker.handler"
  filename         = data.archive_file.custom_chunker_zip.output_path
  source_code_hash = data.archive_file.custom_chunker_zip.output_base64sha256
  timeout          = 60
  memory_size      = 512
}

# GOTCHA #2 (blog Problem 1) — an IAM permission on the KB role is not enough.
# Lambda also requires a resource-based policy that lets bedrock.amazonaws.com
# invoke this specific function. The console creates this automatically when
# you configure a custom chunker there; raw Terraform must declare it explicitly.
resource "aws_lambda_permission" "bedrock_invoke_custom_chunker" {
  statement_id  = "AllowBedrockKBInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.custom_chunker.function_name
  principal     = "bedrock.amazonaws.com"
  source_arn    = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"
}

# -----------------------------------------------------------------------------
# The 5 Knowledge Bases
# -----------------------------------------------------------------------------

resource "aws_bedrockagent_knowledge_base" "kb" {
  for_each = local.strategies

  name     = "${var.benchmark_name}-${each.key}"
  role_arn = aws_iam_role.kb_role.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = var.embedding_model_arn
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = 1024
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.kb[each.key].index_arn
    }
  }
}

resource "aws_bedrockagent_data_source" "ds" {
  for_each = local.strategies

  knowledge_base_id = aws_bedrockagent_knowledge_base.kb[each.key].id
  name              = "corpus-${each.key}"

  data_source_configuration {
    type = "S3"
    s3_configuration { bucket_arn = aws_s3_bucket.corpus.arn }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = each.value.chunking_strategy

      dynamic "fixed_size_chunking_configuration" {
        for_each = each.value.fixed_size_config != null ? [each.value.fixed_size_config] : []
        content {
          max_tokens         = fixed_size_chunking_configuration.value.max_tokens
          overlap_percentage = fixed_size_chunking_configuration.value.overlap_percentage
        }
      }

      # GOTCHA — the provider block is named "level_configuration" (singular),
      # repeated once per level (parent, then child). "level_configurations" is
      # not a real block name and fails schema validation.
      dynamic "hierarchical_chunking_configuration" {
        for_each = each.value.hierarchical_config != null ? [each.value.hierarchical_config] : []
        content {
          overlap_tokens = hierarchical_chunking_configuration.value.overlap_tokens
          level_configuration {
            max_tokens = hierarchical_chunking_configuration.value.parent_max_tokens
          }
          level_configuration {
            max_tokens = hierarchical_chunking_configuration.value.child_max_tokens
          }
        }
      }

      # GOTCHA — the argument is "max_token" (singular), not "max_tokens".
      dynamic "semantic_chunking_configuration" {
        for_each = each.value.semantic_config != null ? [each.value.semantic_config] : []
        content {
          max_token                       = semantic_chunking_configuration.value.max_tokens
          buffer_size                      = semantic_chunking_configuration.value.buffer_size
          breakpoint_percentile_threshold  = semantic_chunking_configuration.value.breakpoint_percentile_threshold
        }
      }
    }

    # Wires the "custom" strategy's Lambda chunker. chunking_configuration above
    # is NONE for this strategy; this POST_CHUNKING step is what actually splits
    # the document — see the comment on local.strategies["custom"].
    dynamic "custom_transformation_configuration" {
      for_each = each.value.custom_lambda != null ? [each.value.custom_lambda] : []
      content {
        intermediate_storage {
          s3_location { uri = "s3://${aws_s3_bucket.corpus.bucket}/intermediate/${each.key}/" }
        }
        # GOTCHA — the provider block is named "transformation" (singular), not
        # "transformations", even though the underlying API field is a list.
        transformation {
          step_to_apply        = "POST_CHUNKING"
          transformation_function {
            transformation_lambda_configuration {
              lambda_arn = each.value.custom_lambda
            }
          }
        }
      }
    }
  }
}

# -----------------------------------------------------------------------------
# IAM role for Bedrock Evaluations
# -----------------------------------------------------------------------------
# run_evaluation.py's --eval-role-arn flag (required) has nowhere else in this
# chapter to get a role from, so we provision one here rather than just fixing
# the header comment that claimed this already existed.
#
# GOTCHA #7 (blog) — "does not have permission to call the KB API" is really
# two separate things, either of which alone produces that exact message:
#   1. Trust policy must scope aws:SourceArn to evaluation-job/* (below).
#   2. Permission policy must list the KB ARNs explicitly, not a wildcard.

locals {
  generator_model_arn = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.generator_model_id}"
  judge_model_arn      = "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.judge_model_id}"
}

resource "aws_iam_role" "eval_role" {
  name = "${var.benchmark_name}-eval-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Service = "bedrock.amazonaws.com" },
      Action = "sts:AssumeRole",
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id },
        StringLike   = { "aws:SourceArn" = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:evaluation-job/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "eval_role_inline" {
  role = aws_iam_role.eval_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["bedrock-agent-runtime:Retrieve", "bedrock-agent-runtime:RetrieveAndGenerate"],
        # Specific KB ARNs, not a wildcard — see GOTCHA #7 above.
        Resource = [for k, v in aws_bedrockagent_knowledge_base.kb : v.arn]
      },
      {
        Effect   = "Allow",
        Action   = ["bedrock:InvokeModel"],
        Resource = [local.generator_model_arn, local.judge_model_arn]
      },
      {
        Effect   = "Allow",
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
        Resource = [aws_s3_bucket.corpus.arn, "${aws_s3_bucket.corpus.arn}/*"]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

data "aws_caller_identity" "current" {}

output "corpus_bucket" { value = aws_s3_bucket.corpus.bucket }
output "knowledge_base_ids" {
  value = { for k, v in aws_bedrockagent_knowledge_base.kb : k => v.id }
}
output "data_source_ids" {
  value = { for k, v in aws_bedrockagent_data_source.ds : k => v.data_source_id }
}
output "eval_role_arn" {
  description = "Pass this to run_evaluation.py --eval-role-arn"
  value       = aws_iam_role.eval_role.arn
}
