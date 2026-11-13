# Chapter 7: AgentCore Memory

This folder accompanies Chapter 7 of *AgentCore in Production: The Operator's Playbook*, 2nd edition (October 2026).

## What's here

```
chapter-07/
├── memory/
│   └── acme-support-memory.yaml        # CloudFormation: 5 strategies, custom namespace keys, TTLs
├── agent/
│   └── support_agent_with_memory.py    # Strands agent; identity from the invocation, never from tools
├── policies/
│   ├── memory-actor-isolation.cedar    # FGAC: actorId must equal the JWT sub
│   └── memory-namespace-isolation.cedar# FGAC: retrieval pinned to a namespace claim
├── custom-strategies/
│   └── billing-preferences.json        # Built-in-with-override example
├── utils/
│   ├── memory_helpers.py               # Data-plane client, hydration, PII-filtered writes
│   └── forget_actor.py                 # The purge function from Section 7.8
└── README.md
```

## What changed since the first edition

| Area | First edition | Now |
|---|---|---|
| Namespaces | `users/{actorId}/...`, tenant folded into `actorId`, sentinel actor for tenant facts | Custom namespace keys (Aug 2026): `/tenant/{tenantid}/actor/{actorId}/...`; tenant facts under `/tenant/{tenantfacts}/facts/`, a key only verified writes supply |
| Actor ID | `f"{tenant_id}/{user_id}"` | The identity provider's `sub`, so Memory FGAC can compare `actorId == sub` |
| Agent tools | Took `tenant_id` and `user_id` as model-supplied arguments | Read identity set by the entrypoint; the model cannot choose whose memory it reads |
| Tenant facts | A model tool could write them | Only application code that verified the fact (`record_tenant_fact_event`) |
| Retention | Only short-term `EventExpiryDuration` | Long-term TTL per strategy group (Oct 13, 2026) |
| Deletion | Not covered | `utils/forget_actor.py` plus the procedure in Section 7.8 |
| Stack output `MemoryId` | `!Ref`, which returns the ARN | `!GetAtt ...MemoryId` |
| Session end | Described as a summary trigger | A closing marker only; AWS documents no trigger |
| Model ID | `us.anthropic.claude-sonnet-4-7-v1:0` (not a real ID) | `us.anthropic.claude-sonnet-4-6`, overridable with `MODEL_ID` |

## Status

The template, the billing-preferences override, the tenant-facts isolation, the per-user purge, and the hydration read were run against a live account on 2026-11-11, and the corrections are described in the headers of those files and in Chapter 7. Not run: the two Cedar policies behind a Gateway Memory connector, the PII filter on writes, and the agent itself.

## Deploy

### 1. The Memory resource

```bash
aws cloudformation deploy \
  --template-file memory/acme-support-memory.yaml \
  --stack-name acme-support-memory \
  --parameter-overrides Environment=staging ShortTermMemoryExpiryDays=30 \
  --region us-west-2
```

The stack exports `MemoryId` and `MemoryArn`. The deploying principal also needs `bedrock-agentcore:UpdateMemoryRecordExpirySpecification`, because the template sets TTLs; without it the request fails with `AccessDeniedException`.

### 2. The agent

Run `support_agent_with_memory.py` in AgentCore Runtime with `MEMORY_ID` set. Package `agent/` and `utils/` as siblings (the agent adds `..` to `sys.path`). Optional: `GUARDRAIL_ID` (a guardrail whose sensitive-information policy anonymizes identifiers), `MODEL_ID`, `MEMORY_MIN_SCORE` (default 0.45, the relevance floor discussed in Section 7.5).

### 3. Invoke

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/example-agent" \
  --runtime-session-id "globex-corp-maria-session-0000000000000001" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"prompt": "What was my last invoice amount?", "tenant_id": "globex-corp", "actor_id": "maria-sub-0001", "session_id": "session0001", "is_first_turn": true}' \
  response.json
```

In production, `tenant_id` and `actor_id` come from the validated JWT, not the payload. Replace `resolve_identity()` accordingly.

### 4. Fine-grained access control (optional, recommended for OAuth callers)

1. Create a Gateway with an AgentCore Memory connector target named `AcmeMemory`.
2. Create a policy engine, add the two policies in `policies/` (replace the Gateway ARN), and attach it to the Gateway in `LOG_ONLY`.
3. Read the evaluation logs for a few days, then switch to `ENFORCE`.

Batch record operations are not Cedar actions. Govern them with IAM, and grant them only to the deletion pipeline.

## Purging a person (Section 7.8)

```bash
python utils/forget_actor.py --memory-id <id> --actor-id <sub> --tenant-id globex-corp           # dry run
python utils/forget_actor.py --memory-id <id> --actor-id <sub> --tenant-id globex-corp --execute
```

The script clears short-term events first, then long-term records in the user's own namespaces. Shared namespaces (tenant facts, tenant reflections) need a manual search and review. A TTL does not replace this: expired records can take days to disappear.

## Cost reference (October 2026)

Short-term: $1.00/GB ingested, $0.20/GB retrieved, $0.10/GB-month, each event billed between 12 KB and 64 KB (since October 6, 2026; previously $0.25 per 1,000 events). Long-term: $0.75 per 1,000 records per month (built-in) or $0.25 (override, self-managed) plus your model; $0.50 per 1,000 retrievals. Section 7.7 works through Acme's numbers (about $41/month at 10,000 conversations).

## License

Code and configurations in this folder are MIT licensed. The accompanying book chapter is copyrighted.
