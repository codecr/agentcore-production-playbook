# Chapter 11: Agent Registry and Observability at Scale

This folder accompanies Chapter 11 of *AgentCore in Production: The Operator's
Playbook*, 2nd edition (October 2026).

> **If you used the 1st-edition code:** it targeted the AWS Agent Registry
> preview in the `bedrock-agentcore` namespace, which **shut down on
> October 30, 2026**. Everything in `registry/` now targets the GA
> `agent-registry` namespace. Existing preview data had to be migrated with
> AWS's tool in the `agentcore-samples` repository; see the
> [migration guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html).

## What's here

```
chapter-11/
├── registry/
│   ├── record_shapes.py      # GA record model: recordType + one primary descriptor
│   ├── publish_records.py    # create, submit, approve/reject/deprecate, search, prove-boundary
│   └── personas.tf           # 4 IAM personas (agent-registry:*) + EventBridge -> SNS
├── observability/
│   └── cross_agent_trace.py  # correlation id propagation, multi-log-group query, reconcile
└── README.md
```

## What changed at GA (August 31, 2026)

| | Preview (1st edition) | GA (this folder) |
|---|---|---|
| boto3 clients | `bedrock-agentcore-control`, `bedrock-agentcore` | `agent-registry-control`, `agent-registry` |
| Endpoints | `*.amazonaws.com` | `agent-registry[-control].{region}.api.aws` |
| IAM prefix / ARNs | `bedrock-agentcore:` / `arn:aws:bedrock-agentcore:…` | `agent-registry:` / `arn:aws:agent-registry:…` |
| Record type | `descriptorType`: MCP, A2A, AGENT_SKILLS, CUSTOM | `recordType`: AGENT, MCP, SKILL, CUSTOM (+ GATEWAY from auto-detection) |
| Record name | `name` (display) | `name` (dedup key) + `displayName` |
| Descriptors | type-specific nesting, `inlineContent` | one primary key (`a2aAgentCard`, `mcpServer`, `agentSkillsDefinition`, `custom`), `data`, `dataSchemaVersion`, `additionalData` |
| Auto-approval | `approvalConfiguration={"autoApproval": bool}` | `approvalConfiguration={"autoApprovalRules": []}` or `["APPROVE_ALL"]` |
| Search | `SearchRegistryRecords` | `SearchDiscoverableRegistryRecords` (one registry, max 20 results), plus `ListDiscoverableRegistryRecords`, `BatchGetDiscoverableRegistryRecord` |
| Events | CloudTrail-based rule, source `aws.bedrock-agentcore` | native events, source `aws.agent-registry`, 5 record and 7 registry detail types |
| Managed policy | `BedrockAgentCoreFullAccess` | `AgentRegistryFullAccess` (the old one was **not** extended) |
| IaC | boto3 only | CloudFormation (`AWS::AgentRegistry::Registry`, `::RegistryRecord`), Terraform, CDK |

New at GA and used by the chapter: Organizations-wide **auto-detection** of
AgentCore Runtimes and Gateways (records named `aws-autodetected-…`, with
provenance), **RAM sharing** with four managed permissions (read-only,
consumer, publisher, admin), custom metadata on records, KMS keys, tags, and
pricing (first 5,000 live records and first 1M searches per month free).
Still five Regions: `us-east-1`, `us-west-2`, `eu-west-1`, `ap-southeast-2`,
`ap-northeast-1`.

Workload identities and OAuth credential providers stay in the
`bedrock-agentcore` namespace. Do not search-and-replace every
`bedrock-agentcore` string in your IAM policies.

## Verification status

The registry code is validated **offline** against the botocore service
models for `agent-registry-control` and `agent-registry` (botocore 1.43.x):
operation names, required parameters, and request shapes. It has **not** been
run against the live GA service by the author. Before relying on it, check:

1. `dataSchemaVersion` values in `record_shapes.py` (taken from AWS's GA
   sample: `2025-12-11` for MCP servers, `2025-06-18` for MCP tools, `0.3`
   for A2A cards).
2. The search `filters` document shape (`{"recordType": {"$eq": "MCP"}}`).
3. The span attribute path used by `cross_agent_trace.build_query`
   (`attributes.acme.correlation_id`) against a real per-agent log group.

## Quickstart

```bash
pip install --upgrade boto3        # any 2026-08 or later release has agent-registry
export AWS_REGION=us-east-1

# 1. Personas and approval routing
cd registry
terraform init
terraform apply -var="curator_email=platform-curators@acme.example" \
                -var='trusted_principal_arns=["arn:aws:iam::111122223333:role/SSOEngineering"]'

# 2. Create the registry (Administrator). Asynchronous: wait for READY.
python publish_records.py create-registry --name acme-agent-catalog

# 3. Publish and submit (Publisher)
python publish_records.py publish --registry-id <id>
python publish_records.py submit  --registry-id <id> --all

# 4. Adjudicate (Curator); the reason is required, max 255 characters
python publish_records.py approve --registry-id <id> --record-id <rid> \
  --reason "Owner and contact documented; tenant scoping enforced at the Gateway."

# 5. Search (Consumer)
python publish_records.py search --registry-id <id> --query "explain an invoice to a customer"

# 6. Prove the Publisher cannot approve (run with Publisher credentials)
python publish_records.py prove-boundary --registry-id <id> --record-id <rid>

# 7. Reconcile deployed against registered
cd ..
python -c "from observability.cross_agent_trace import reconcile; \
           import json; print(json.dumps(reconcile('<registry-id>'), indent=2))"
```

Local demonstrations, no AWS needed:

```bash
python registry/record_shapes.py
python observability/cross_agent_trace.py
```

## Tracing after July 20, 2026

Agents created from July 20, 2026 write spans to a per-agent log group,
`/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>`; older agents
write to `aws/spans` unless opted in (`UNIFIED_TRACES_DESTINATION_ENABLED=true`
plus a recent ADOT). `cross_agent_trace.span_log_groups()` builds the list
from your deployed runtimes and includes `aws/spans`; a Logs Insights query
takes up to 50 log groups. Synchronous `InvokeAgentRuntime` calls accept W3C
`traceParent`/`traceState`/`baggage`; the correlation id in the payload is
what survives queues, events, and third-party hops.

## License

Code and configurations in this folder are MIT licensed. The accompanying
book chapter is copyrighted; the code is explicitly intended for reuse.
