# Chapter 7: AgentCore Memory

This folder accompanies Chapter 7 of *AgentCore in Production: The Operator's Playbook*.

## What's here

```
chapter-07/
├── memory/
│   └── acme-support-memory.yaml        # CloudFormation with 4 built-in strategies
├── agent/
│   └── support_agent_with_memory.py    # Strands agent integrating Memory
├── custom-strategies/
│   └── billing-preferences.json        # Custom extraction prompt override
├── utils/
│   └── memory_helpers.py               # MemoryClient + 4 retrieval patterns
└── README.md                           # This file
```

## What this delivers

**A complete CloudFormation template** for an AgentCore Memory resource with
all four built-in strategies enabled: User Preference, Semantic Memory (two
strategies — per-user facts and tenant-wide facts), Summary Memory, and
Episodic Memory. Namespaces key off `{actorId}` — the real CreateMemory API's
only per-request identity placeholder (`{sessionId}` and `{memoryStrategyId}`
are the other two; there is no `{tenantId}` or `{userId}`). The runtime's
`actor_id = f"{tenant_id}/{user_id}"` gives per-user namespaces the same
multi-tenant isolation pattern from Chapter 2. Tenant-wide facts, which have
no user to scope to, use a reserved sentinel actor per tenant instead — see
"Tenant-wide facts" below.

**A working Strands agent example** that uses Memory across sessions. The
agent in `support_agent_with_memory.py` does what the customer in Chapter 7
Section 7.1 wished Acme's agent had done: load user preferences at session
start, recognize the user across sessions, and avoid re-asking for
information already provided.

**A custom strategy override** showing how to tune extraction for a specific
domain. The included `billing-preferences.json` narrows the User Preference
strategy to only extract billing-related preferences, demonstrating the
pattern from Chapter 7 Section 7.5. Useful as a starting point when you
need domain-specific extraction.

**A Python helpers module** wrapping AgentCore Memory APIs with the four
patterns the chapter describes as canonical: session-start hydration,
per-turn event recording, explicit session-end signaling, and the thin
data-plane-only client wrapper.

### Tenant-wide facts: the sentinel-actor pattern

The real CreateMemory API has no namespace placeholder for "this tenant,
any user" — only `{actorId}`, `{sessionId}`, and `{memoryStrategyId}`
exist, and `{actorId}` always resolves to one literal event's actor. The
`AcmeTenantFacts` strategy works around this with a reserved sentinel
actor per tenant, `f"{tenant_id}/__tenant_facts__"`
(`memory_helpers.tenant_facts_actor_id`). Ordinary per-user conversation
events never populate this namespace automatically — there's no
server-side mechanism to route "this fact belongs to the whole tenant"
out of a per-actor template. Instead, the agent calls the
`record_tenant_fact` tool explicitly when it learns something durable
and tenant-wide (see `support_agent_with_memory.py`), which writes
through `memory_helpers.record_tenant_fact_event`. Reading back uses
`memory_helpers.tenant_facts_namespace`.

This keeps the whole design in one Memory resource. Genuine cross-tenant
episodic-pattern sharing (see Strategy 4 in the CFN template's comments)
would need a dedicated Memory resource per tenant instead — a bigger
architectural change this template intentionally doesn't attempt.

## How to deploy

### Step 1 — Deploy the Memory resource

```bash
aws cloudformation deploy \
  --template-file memory/acme-support-memory.yaml \
  --stack-name acme-support-memory \
  --parameter-overrides Environment=staging ShortTermMemoryExpiryDays=30 \
  --region us-west-2
```

The output exports the Memory ID and ARN. Note them — they're needed by
the agent.

### Step 2 — Deploy the agent

The `support_agent_with_memory.py` example is designed to run inside an
AgentCore Runtime. Use the deploy script from Chapter 6
(`chapter-06/scripts/deploy_runtime.py`) and pass `MEMORY_ID` as an
environment variable on the runtime. `support_agent_with_memory.py`
imports `utils/memory_helpers.py` via `sys.path` (it adds `agent/..` to
the path and imports `utils.memory_helpers`), so package the container
with `agent/` and `utils/` as siblings under the same root — copy the
whole `chapter-07/` folder in, not just `agent/`:

```bash
export MEMORY_ID=<from-step-1-output>
# ... build the container ...
python ../chapter-06/scripts/deploy_runtime.py --create --name acme-support-agent
```

### Step 3 — Invoke with tenant + user context

The agent expects every invocation to include `tenant_id`, `user_id`,
and `session_id`. Without these, memory cannot be partitioned correctly.

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "arn:aws:..." \
  --runtime-session-id "globex-corp-user-maria-001-session-abc-123" \
  --payload '{
    "prompt": "What was my last invoice amount?",
    "tenant_id": "globex-corp",
    "user_id": "user-maria-001",
    "session_id": "session-abc-123",
    "is_first_turn": true
  }'
```

The `is_first_turn` flag triggers the memory hydration pattern — preferences
and recent summaries are loaded into the agent's context before reasoning.
Subsequent turns in the same session do not re-hydrate (memory was already
in context).

### Step 4 — End sessions explicitly

When the user ends a conversation, call the agent's `end_session()` helper
to trigger summary generation:

```python
from support_agent_with_memory import end_session

end_session(
    tenant_id="globex-corp",
    user_id="user-maria-001",
    session_id="session-abc-123",
)
```

This is asynchronous — the summary appears in long-term memory 20-40
seconds later (sooner for summary strategies, longer for episodic).

## Applying the custom strategy override

The built-in User Preference strategy captures preferences broadly. For
a billing-focused agent, the included `billing-preferences.json` shows
how to override both the extraction and consolidation prompts so only
billing-specific preferences are captured and merged.

The file's top-level `customMemoryStrategy` object is one entry for the
`memoryStrategies` list accepted directly by the bedrock-agentcore-control
`CreateMemory`/`UpdateMemory` API (validated against the real API shape —
see the chapter's fix notes) — pass it as
`{"memoryStrategies": [{"customMemoryStrategy": <that object>}]}`.

To apply it via the console instead (Memory resource → Strategies → Add
custom strategy → Built-in with override), don't paste the JSON file
directly — the console's prompt fields only want the
`configuration.userPreferenceOverride.extraction.appendToPrompt` and
`.consolidation.appendToPrompt` string values, plus the model ID and
namespace, entered separately. The chosen model
(`claude-haiku-4-5-20251001`) keeps extraction and consolidation cost
minimal.

Cost difference: built-in strategies bill at $0.75 per stored unit
(includes extraction inference). Custom strategies bill at $0.25 per
stored unit + your own Bedrock inference at the rate of the model you
chose. Haiku is the right default for extraction — the task is simple
enough that Sonnet is overkill.

## Anti-patterns this code helps avoid

Each of the five anti-patterns from Chapter 7 Section 7.7 has a
mitigation in this folder:

1. **Treating Memory as conversation log.** The agent records events
   via `record_user_event` / `record_assistant_event`. Long-term
   strategies extract structured records asynchronously; raw text
   doesn't pollute long-term memory.

2. **No namespace strategy.** The CloudFormation template uses
   `users/{actorId}/...` patterns for user-scoped namespaces, where
   `actor_id = f"{tenant_id}/{user_id}"`. Cross-tenant leakage across
   real users is structurally prevented by that composite. The one
   deliberate exception is `AcmeTenantFacts`, which aggregates across
   users of one tenant on purpose via a reserved sentinel actor per
   tenant (`{tenant_id}/__tenant_facts__`) — see "Tenant-wide facts"
   above; it still can't leak across *tenants*, only across users
   within the same tenant, which is the intended behavior.

3. **Forgetting episodic latency.** The agent's system prompt does
   not assume immediate availability of episodic insights. Comments
   in `memory_helpers.py::end_session_with_summary` document the
   latency expectation.

4. **Hardcoding extraction prompts.** The custom strategy override
   lives in JSON, versionable in Git, with explicit comments about
   when to update it.

5. **Unbounded memory accumulation.** The CFN parameter
   `ShortTermMemoryExpiryDays` (default 30) caps raw event retention.
   For long-term records, AWS retention policies apply; configure
   based on your compliance needs.

## License

Code and configurations in this folder are MIT licensed. The
accompanying book chapter is copyrighted; the code is explicitly
intended for reuse.
