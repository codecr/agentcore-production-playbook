# Chapter 11: Agent Registry and Observability at Scale

This folder accompanies Chapter 11 of *AgentCore in Production: The Operator's Playbook*.

## What's here

```
chapter-11/
├── registry/
│   ├── record_shapes.py      # Normalization layer for the 4 descriptor shapes
│   ├── publish_records.py    # Full lifecycle: create, submit, approve, reject, search
│   └── personas.tf           # The 4 IAM personas + EventBridge approval routing
├── observability/
│   └── cross_agent_trace.py  # Correlation across agent boundaries + inventory audit
└── README.md
```

## What this delivers

**A normalization layer for the four record types.** AWS Agent Registry wants
its descriptors in a different shape for each of MCP, A2A, AGENT_SKILLS, and
CUSTOM — three distinct patterns across four types. `record_shapes.py` hides
that behind one interface, validates locally the omissions the API reports
with unhelpful errors (A2A's `protocolVersion`, AGENT_SKILLS' plural key),
and enforces two fields the Registry does not require but every catalog
needs: an owner and a contact.

```bash
python registry/record_shapes.py
# [ok] acme-support-agent (A2A) — owner Squad Support Platform
# ...
#   A2A            -> {'a2a': ['agentCard']}
#   MCP            -> {'mcp': ['server', 'tools']}
#   AGENT_SKILLS   -> {'agentSkills': ['skillDefinition', 'skillMd']}
#   CUSTOM         -> {'custom': ['inlineContent']}
```

**The full record lifecycle as a script.** `publish_records.py` walks DRAFT →
PENDING_APPROVAL → APPROVED or REJECTED with the two API details that cost an
afternoon each: the parameter is `status`, not `newStatus`, and `statusReason`
is required on *every* transition including approval. The script refuses
reasons shorter than twenty characters, because "ok" is not an audit trail.

It also includes `prove-boundary`, which attempts an approval with whatever
credentials you are holding and reports the result. Run it as a Publisher and
you should see `AccessDeniedException`. A governance control nobody has tested
is a governance control nobody should trust.

**The four personas as Terraform.** `personas.tf` provisions Administrator,
Publisher, Curator, and Consumer roles with explicit `Deny` statements at the
boundaries — a Publisher that could approve its own record turns the workflow
into theater. It also wires EventBridge to SNS so a submitted record reaches
a human, since a submission nobody sees sits in PENDING_APPROVAL forever and
teaches Publishers to route around the catalog.

**Cross-agent correlation and inventory reconciliation.**
`cross_agent_trace.py` carries a business-level correlation id across agent
hops — through payload headers, because agent-to-agent calls do not reliably
preserve OpenTelemetry context — and reconstructs a full timeline from
CloudWatch Logs Insights when a customer asks what happened. The same module
reconciles deployed runtimes against catalog records and reports the bucket
that matters: agents running in production that nobody registered.

```bash
python observability/cross_agent_trace.py
# demonstrates propagation and round-trip survival, no AWS required
```

## Prerequisites and preview status

AWS Agent Registry entered **preview on April 9, 2026**. As of this writing:

- **Five preview Regions**: `us-east-1`, `us-west-2`, `ap-southeast-2`,
  `ap-northeast-1`, `eu-west-1`
- **`boto3 >= 1.42.87`** — earlier SDKs do not have the methods at all
- **AWS CLI v2 >= 2.34.28** — the CLI arrived after the SDK. If
  `aws bedrock-agentcore-control list-registries` returns `Found invalid
  choice`, check `aws --version`
- **Terraform resource for the registry itself: it's complicated.**
  `hashicorp/aws` shipped `aws_bedrockagentcore_registry` in v6.52.0
  (2026-06-24) — so the registry can now be provisioned in Terraform. But
  as of v6.53.0 (2026-07-01) that same resource is already marked
  deprecated: AWS is moving Agent Registry from the `bedrock-agentcore`
  namespace to a dedicated `agent-registry` namespace on 2026-08-06, with
  the old namespace sunsetting 2026-09-17. `personas.tf` deliberately keeps
  the registry itself on the boto3 path (`create_registry()` in
  `publish_records.py`) rather than wiring up a resource that would be dead
  within weeks — check the provider's CHANGELOG for the `agent-registry`
  namespace before migrating. Neither provider ships a `_record` resource
  yet regardless of namespace, so `publish_records.py`'s boto3 path for
  records stays either way
- **Cost during preview: zero.** At GA, pricing is expected to combine live
  record count with discovery API calls

Check all of the above before planning a rollout; this is the fastest-moving
surface in the book.

## Quickstart

```bash
pip install "boto3>=1.42.87"
export AWS_REGION=us-east-1

# 1. Personas and approval routing
cd registry
terraform init
terraform apply -var="curator_email=platform-curators@acme.example" \
                -var='trusted_principal_arns=["arn:aws:iam::111122223333:role/SSOEngineering"]'

# 2. Create the registry (Administrator credentials)
# CreateRegistry's response has only registryArn — no registryId field.
# The script derives the id from the ARN's trailing segment and prints
# both; use that id (not the ARN) for --registry-id below.
python publish_records.py create-registry --name acme-agent-catalog

# 3. Publish and submit (Publisher credentials)
python publish_records.py publish --registry-id <id>
python publish_records.py submit  --registry-id <id> --all

# 4. Adjudicate (Curator credentials)
python publish_records.py approve --registry-id <id> --record-id <rid> \
  --reason "Owner and contact documented; tenant scoping enforced at the Gateway."

# 5. Search (Consumer credentials)
python publish_records.py search --registry-id <id> --query "explain an invoice to a customer"

# 6. Reconcile deployed against registered
# cross_agent_trace.py lives under chapter-11/observability/, a sibling of
# registry/ — leave the registry/ directory before importing it, or the
# import fails with ModuleNotFoundError.
cd ..
python -c "from observability.cross_agent_trace import reconcile; \
           import json; print(json.dumps(reconcile('<registry-id>'), indent=2))"
```

## A note on search

The Registry advertises hybrid search — keyword and semantic with rank
fusion. In practice, short queries behave close to keyword matching: no
stemming, no synonym expansion, word order matters. `payments` finds a record
whose description says "payments"; `payment` may not. Longer, natural-language
queries are where the semantic half earns its keep.

The operational consequence is that **the catalog is only as discoverable as
its worst description**. Write descriptions using the words a consumer would
actually type, avoid bare generic nouns, and treat rejection-with-a-reason as
the mechanism that teaches Publishers to write better ones. `record_shapes.py`
enforces a minimum length and flags generic-word-heavy descriptions locally,
before a Curator has to.

## License

Code and configurations in this folder are MIT licensed. The accompanying
book chapter is copyrighted; the code is explicitly intended for reuse.
