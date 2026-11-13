# =============================================================================
# personas.tf — The four AWS Agent Registry IAM personas, plus approval routing
# =============================================================================
# Accompanies Chapter 11 of "AgentCore in Production: The Operator's Playbook".
#
# AWS names four personas for the Registry, and they map to four IAM policies
# with genuinely different permissions. This is the cleanest role separation
# in AgentCore, and it is worth implementing exactly rather than collapsing
# into "one admin role and everyone else".
#
#   Administrator — owns registry infrastructure. Creates registries, sets
#                   authentication, wires EventBridge, can override any
#                   status transition.
#   Publisher     — creates and iterates records, submits for approval.
#                   CANNOT approve its own records, cannot delete registries.
#   Curator       — approves, rejects, deprecates. CANNOT create or edit
#                   record content. Its only power is state transitions.
#   Consumer      — searches approved records. Data plane only. This is the
#                   role whose credentials sign requests from Claude Code or
#                   any other MCP client.
#
# 2ND EDITION (October 2026): AWS Agent Registry went GA on August 31, 2026
# under its own namespace, `agent-registry`. Every action below moved from the
# `bedrock-agentcore:` prefix to `agent-registry:`, ARNs became
# arn:aws:agent-registry:<region>:<account>:registry/<id>[/record/<rid>], and
# the preview namespace shut down on October 30, 2026.
#
# The registry itself can now be managed as code (CloudFormation
# AWS::AgentRegistry::Registry / ::RegistryRecord, Terraform, CDK). This file
# still provisions only the personas and the approval routing, so it works
# whichever tool owns the registry. Do NOT attach the AWS managed policy
# AgentRegistryFullAccess to Publishers: it grants UpdateRegistryRecordStatus,
# which is exactly the self-approval this file exists to prevent.
#
# Not applied against AWS by the author for the GA namespace; action names are
# taken from the Service Authorization Reference for agent-registry.
# =============================================================================

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.42" # personas and eventing only; no registry resource used here
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "org_name" {
  type    = string
  default = "acme"
}

variable "curator_email" {
  type        = string
  description = "Address that receives approval requests"
}

variable "trusted_principal_arns" {
  type        = list(string)
  description = "Principals allowed to assume these persona roles (SSO roles, CI roles)"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # `.name` is deprecated in the aws_region data source in favor of
  # `.region` (confirmed via `terraform providers schema -json` against
  # hashicorp/aws v6.57.1 — `name` carries "deprecated": true).
  region = data.aws_region.current.region

  # Registry ARNs are region- and account-scoped:
  #   arn:aws:agent-registry:<region>:<account>:registry/<id>
  # Wildcarded here because the registry is created out-of-band by boto3.
  # Once the provider supports the resource, narrow this to the specific ARN.
  registry_arn_pattern = "arn:aws:agent-registry:${local.region}:${local.account_id}:registry/*"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.trusted_principal_arns }
      Action    = "sts:AssumeRole"
    }]
  })
}

# =============================================================================
# Persona 1 — Administrator
# =============================================================================

resource "aws_iam_role" "registry_administrator" {
  name               = "${var.org_name}-registry-administrator"
  description        = "Owns registry infrastructure. Can override any status transition."
  assume_role_policy = local.assume_role_policy
}

resource "aws_iam_role_policy" "administrator" {
  role = aws_iam_role.registry_administrator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "FullRegistryControl"
        Effect = "Allow"
        Action = [
          "agent-registry:CreateRegistry",
          "agent-registry:UpdateRegistry",
          "agent-registry:DeleteRegistry",
          "agent-registry:GetRegistry",
          "agent-registry:ListRegistries",
          "agent-registry:CreateRegistryRecord",
          "agent-registry:UpdateRegistryRecord",
          "agent-registry:DeleteRegistryRecord",
          "agent-registry:GetRegistryRecord",
          "agent-registry:ListRegistryRecords",
          "agent-registry:SubmitRegistryRecordForApproval",
          "agent-registry:UpdateRegistryRecordStatus",
          "agent-registry:SearchDiscoverableRegistryRecords",
          "agent-registry:ListDiscoverableRegistryRecords",
          "agent-registry:GetDiscoverableRegistryRecord",
          "agent-registry:InvokeRegistryMcp",
        ]
        Resource = [local.registry_arn_pattern, "${local.registry_arn_pattern}/*"]
      },
      {
        # CreateRegistry and ListRegistries take no resource ARN.
        Sid      = "AccountLevelRegistryActions"
        Effect   = "Allow"
        Action   = ["agent-registry:CreateRegistry", "agent-registry:ListRegistries"]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# Persona 2 — Publisher
# =============================================================================
# The explicit Deny is the important part. Without it, a Publisher whose
# principal also carries a broad managed policy could approve its own record,
# and the whole workflow becomes theater.

resource "aws_iam_role" "registry_publisher" {
  name               = "${var.org_name}-registry-publisher"
  description        = "Squad builders. Create and submit records; cannot approve them."
  assume_role_policy = local.assume_role_policy
}

resource "aws_iam_role_policy" "publisher" {
  role = aws_iam_role.registry_publisher.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AuthorRecords"
        Effect = "Allow"
        Action = [
          "agent-registry:CreateRegistryRecord",
          "agent-registry:UpdateRegistryRecord",
          "agent-registry:GetRegistryRecord",
          "agent-registry:ListRegistryRecords",
          "agent-registry:SubmitRegistryRecordForApproval",
          "agent-registry:SearchDiscoverableRegistryRecords",
          "agent-registry:GetRegistry",
        ]
        Resource = [local.registry_arn_pattern, "${local.registry_arn_pattern}/*"]
      },
      {
        Sid      = "ListRegistriesAccountLevel"
        Effect   = "Allow"
        Action   = ["agent-registry:ListRegistries"]
        Resource = "*"
      },
      {
        Sid    = "CannotAdjudicateOwnWork"
        Effect = "Deny"
        Action = [
          "agent-registry:UpdateRegistryRecordStatus",
          "agent-registry:DeleteRegistry",
          "agent-registry:DeleteRegistryRecord",
        ]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# Persona 3 — Curator
# =============================================================================
# Can change state and nothing else. Notably cannot edit a record to make it
# acceptable — the only feedback channel is rejection with a reason, which
# forces the conversation to happen with the Publisher rather than silently.

resource "aws_iam_role" "registry_curator" {
  name               = "${var.org_name}-registry-curator"
  description        = "Approval gate. State transitions only; cannot author records."
  assume_role_policy = local.assume_role_policy
}

resource "aws_iam_role_policy" "curator" {
  role = aws_iam_role.registry_curator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AdjudicateRecords"
        Effect = "Allow"
        Action = [
          "agent-registry:UpdateRegistryRecordStatus",
          "agent-registry:GetRegistryRecord",
          "agent-registry:ListRegistryRecords",
          "agent-registry:SearchDiscoverableRegistryRecords",
          "agent-registry:GetRegistry",
        ]
        Resource = [local.registry_arn_pattern, "${local.registry_arn_pattern}/*"]
      },
      {
        Sid      = "ListRegistriesAccountLevel"
        Effect   = "Allow"
        Action   = ["agent-registry:ListRegistries"]
        Resource = "*"
      },
      {
        Sid    = "CannotAuthor"
        Effect = "Deny"
        Action = [
          "agent-registry:CreateRegistryRecord",
          "agent-registry:UpdateRegistryRecord",
          "agent-registry:CreateRegistry",
          "agent-registry:DeleteRegistry",
          # Defense-in-depth, matching the Publisher's pattern: the Curator's
          # allow-list above never grants DeleteRegistryRecord, but an
          # explicit Deny survives a future broad managed-policy attachment
          # that the allow-list alone would not.
          "agent-registry:DeleteRegistryRecord",
        ]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# Persona 4 — Consumer
# =============================================================================
# The most restricted role and the one used most often: these are the
# credentials behind every IDE, every client agent, every developer asking
# the catalog a question.

resource "aws_iam_role" "registry_consumer" {
  name               = "${var.org_name}-registry-consumer"
  description        = "Search approved records. Data plane only. Used by MCP clients."
  assume_role_policy = local.assume_role_policy
}

resource "aws_iam_role_policy" "consumer" {
  role = aws_iam_role.registry_consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DiscoverOnly"
        Effect = "Allow"
        Action = [
          "agent-registry:SearchDiscoverableRegistryRecords",
          "agent-registry:ListDiscoverableRegistryRecords",
          # BatchGetDiscoverableRegistryRecord authorizes against this action.
          "agent-registry:GetDiscoverableRegistryRecord",
          # The registry's MCP endpoint (IDEs, client agents).
          "agent-registry:InvokeRegistryMcp",
        ]
        Resource = [local.registry_arn_pattern, "${local.registry_arn_pattern}/*"]
      },
      {
        Sid    = "NoWrites"
        Effect = "Deny"
        Action = [
          "agent-registry:CreateRegistryRecord",
          "agent-registry:UpdateRegistryRecord",
          "agent-registry:UpdateRegistryRecordStatus",
          "agent-registry:DeleteRegistryRecord",
        ]
        Resource = "*"
      }
    ]
  })
}

# =============================================================================
# Approval routing — EventBridge to SNS
# =============================================================================
# A submitted record that nobody sees sits in PENDING_APPROVAL forever, and
# an approval queue with a multi-day latency teaches Publishers to route
# around the Registry. Wire the notification on day one.

resource "aws_sns_topic" "registry_approvals" {
  name = "${var.org_name}-registry-approvals"
}

resource "aws_sns_topic_subscription" "curator_email" {
  topic_arn = aws_sns_topic.registry_approvals.arn
  protocol  = "email"
  endpoint  = var.curator_email
}

resource "aws_cloudwatch_event_rule" "record_submitted" {
  name        = "${var.org_name}-registry-record-submitted"
  description = "Notify the Curator when a record is submitted for approval"
  # GA: the Registry emits native events on the default bus. The preview's
  # source (aws.bedrock-agentcore) and the CloudTrail-based pattern used in
  # the first edition no longer match anything.
  event_pattern = jsonencode({
    source        = ["aws.agent-registry"]
    "detail-type" = ["Registry Record State changed to Pending Approval"]
  })
}

resource "aws_cloudwatch_event_target" "notify_curator" {
  rule      = aws_cloudwatch_event_rule.record_submitted.name
  target_id = "notify-curator"
  arn       = aws_sns_topic.registry_approvals.arn

  input_transformer {
    input_paths = {
      recordArn = "$.resources[0]"
      recordId  = "$.detail.registryRecordId"
      registry  = "$.detail.registryId"
      time      = "$.time"
    }
    input_template = <<-EOT
      "A registry record was submitted for approval.

      Registry: <registry>
      Record:   <recordId>
      ARN:      <recordArn>
      At:       <time>

      Review it in the AgentCore console under Discover > Agent Registry,
      or approve/reject with a substantive statusReason via the API.
      Remember: the reason is required, it is permanent, and it is the
      answer to 'why was this approved?' six months from now."
    EOT
  }
}

resource "aws_sns_topic_policy" "allow_eventbridge" {
  arn = aws_sns_topic.registry_approvals.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.registry_approvals.arn
      # Confused-deputy guard: without this, any EventBridge rule in any
      # account could be pointed at this topic by an attacker who knows its
      # ARN. Scoping to this account and this specific rule's ARN means only
      # our own record_submitted rule can publish here.
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = local.account_id
        }
        ArnEquals = {
          "aws:SourceArn" = aws_cloudwatch_event_rule.record_submitted.arn
        }
      }
    }]
  })
}

# =============================================================================
# Outputs
# =============================================================================

output "persona_role_arns" {
  value = {
    administrator = aws_iam_role.registry_administrator.arn
    publisher     = aws_iam_role.registry_publisher.arn
    curator       = aws_iam_role.registry_curator.arn
    consumer      = aws_iam_role.registry_consumer.arn
  }
}

output "approval_topic_arn" {
  value = aws_sns_topic.registry_approvals.arn
}
