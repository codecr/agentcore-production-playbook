# Chapter 9: Structured Outputs

This folder accompanies Chapter 9 of *AgentCore in Production: The Operator's Playbook*.

## What's here

```
chapter-09/
├── schemas/
│   └── production_schemas.py     # 3 output schemas + 1 strict tool spec
├── agent/
│   └── triage_agent.py           # The same agent, before and after
├── utils/
│   └── schema_registry.py        # Versioning, validation, compatibility checks
├── tests/
│   └── test_schemas.py           # 34 tests, no AWS credentials required
└── README.md
```

## What this delivers

**A schema library** written to the production conventions from Chapter 9
Section 9.6: `additionalProperties: false` everywhere, every property
required (optionality modeled as nullable types), enums instead of free
strings wherever the value space is closed, a description on every property,
and shallow nesting. Three output schemas — ticket triage, entity extraction
with provenance, and a policy decision with an explicit abstention path —
plus one strict tool specification for a refund tool.

**The before-and-after agent.** `triage_agent.py` contains both
implementations of the Acme SaaS triage agent side by side:
`TriageAgentBefore` with its format instructions, its extraction helper, its
structure validator, its retry loop, and its keyword fallback; and
`TriageAgentAfter`, which is the same agent with the contract moved into the
schema. The diff between the two classes is the chapter's argument in code.

**A schema registry** that treats schemas the way Chapter 3 treated Cedar
policies: named, versioned, validated before deployment, and diffed between
versions so a breaking change is labeled as one. It also supports warming
Bedrock's grammar cache at startup so the compilation cost lands on your
deploy rather than on the first user after it.

**A local test suite** that runs with no AWS credentials and no network.
Illegal Draft 2020-12 syntax, a missing `additionalProperties`, a tool spec
without `strict: true`, and a breaking change shipped without a version bump
are all caught here, before any request reaches Bedrock.

## Running the tests

```bash
pip install pytest jsonschema
pytest tests/test_schemas.py -v
```

All 34 tests pass offline. Add these to CI and run them on every change to a
schema.

## Running the agent

```bash
pip install boto3
export AWS_REGION=us-east-1
export TRIAGE_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0

python agent/triage_agent.py
```

The model must be one that supports Structured Outputs. As of mid-2026 that
means Claude Haiku 4.5, Sonnet 4.5, Opus 4.5, Opus 4.6, and selected
open-weight models. Amazon Nova and Titan do **not** support the feature;
neither do Claude 3.x, Claude Sonnet 4, or Opus 4.0/4.1. Check the current
list before assuming.

## The two mechanisms

Structured Outputs ships as two complementary mechanisms, and production
agents usually want both.

**Response schemas** constrain what the model writes back to you:

```python
outputConfig={
    "textFormat": {
        "type": "json_schema",
        "structure": {
            "jsonSchema": {
                "schema": json.dumps(MY_SCHEMA),   # a STRING, not a dict
                "name": "ticket_triage_v1",
                "description": "...",
            }
        },
    }
}
```

**Strict tool definitions** constrain the arguments the model produces when
it calls a tool:

```python
toolConfig={
    "tools": [{
        "toolSpec": {
            "name": "issue_refund",
            "strict": True,                # <- schema enforcement on inputs
            "inputSchema": {"json": REFUND_INPUT_SCHEMA},
        }
    }]
}
```

For an agent, the second one usually matters more. It is the difference
between a malformed tool call being rejected at the Gateway and never being
generated in the first place.

## Things that will bite you

1. **The schema goes in as a JSON string.** `json.dumps(schema)`, not the
   dict. Passing the dict produces an error message that reads like a schema
   problem when it is a serialization problem.

2. **An invalid schema fails immediately with HTTP 400**, not during
   generation. This is a feature — the test suite here catches it earlier
   still.

3. **Grammar compilation is cached for about 24 hours** and keyed on the
   schema name. Two different schemas sharing a name is a subtle and
   miserable bug; the registry's `qualified_name` includes the version to
   prevent it.

4. **Structured Outputs and Anthropic's citations feature cannot be combined
   in the same request.** Pick one per call.

5. **The schema constrains shape, not truth.** A perfectly valid
   `{"priority": "P1"}` can still be the wrong priority. Chapter 9 Section
   9.7 covers what the schema does not buy you, and Chapters 5 and 10 cover
   what does.

## License

Code in this folder is MIT licensed. The accompanying book chapter is
copyrighted; the code is explicitly intended for reuse.
