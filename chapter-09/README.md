# Chapter 9 — Structured Outputs: From Probable Shape to Guaranteed Shape

This folder accompanies Chapter 9 of *AgentCore in Production: The Operator's Playbook* (2nd edition, October 2026).

## What's here

```
chapter-09/
├── schemas/
│   └── production_schemas.py     # 3 output schemas + 1 strict tool spec + output-config helpers
├── agent/
│   └── triage_agent.py           # The same agent, before and after
├── utils/
│   └── schema_registry.py        # Versioning, Bedrock-subset checks, check_ranges, warm_cache
├── tests/
│   └── test_schemas.py           # 49 tests, no AWS credentials required
└── README.md
```

## What changed in the 2nd edition

- **No `minimum`, `maximum`, `multipleOf`, `minLength`, or `maxLength` in any
  schema.** Bedrock's supported subset of JSON Schema rejects them with HTTP
  400. The 1st edition shipped `"minimum": 1` on `amount_cents` and a 0..1
  bound on `confidence`; both bounds now live in `check_ranges`, a post-parse
  check, and in the property descriptions.
- **`additionalProperties: false` is mandatory** for Bedrock, not just a
  convention. The registry flags any object without it.
- **New Bedrock-subset checks**: unsupported keywords, `minItems` other than
  0/1, external `$ref`, more than 12 optional properties, more than 16
  union-typed (for example nullable) properties.
- **`$schema` removed** from the schemas sent to Bedrock. It is not in the
  documented supported-feature list, and local validation uses
  `Draft202012Validator` explicitly, so it carried no information.
- **`stopReason` check** in `TriageAgentAfter`: a response truncated by
  `maxTokens` is incomplete JSON even with a schema.
- **120 s read timeout** for the first request against a new schema (grammar
  compilation is documented at up to ~90 s). See
  `utils.schema_registry.structured_outputs_client`.
- **InvokeModel helper**: `as_invoke_model_output_config` (Claude takes the
  schema as an object in `output_config.format`; Converse takes a string).

## What this delivers

**A schema library** written to the production conventions from Chapter 9
Section 9.6: `additionalProperties: false` everywhere, every property
required (optionality modeled as nullable types), enums instead of free
strings wherever the value space is closed, a description on every property,
shallow nesting, and no ranges or lengths in the schema. Three output schemas
(ticket triage, entity extraction with provenance, and a policy decision with
an explicit abstention path) plus one strict tool specification for a refund
tool.

**The before-and-after agent.** `triage_agent.py` contains both
implementations of the Acme SaaS triage agent side by side:
`TriageAgentBefore` with its format instructions, extraction helper,
structure validator, retry loop, and keyword fallback; and
`TriageAgentAfter`, which checks `stopReason`, parses once, and runs
`check_ranges` before the unchanged router sees the result. Failures raise
`TriageIncomplete`, which is meant to page someone, not to be swallowed.

**A schema registry** that treats schemas the way Chapter 3 treated Cedar
policies: named, versioned, validated before deployment, and diffed between
versions so a breaking change is labeled as one. It also warms Bedrock's
grammar cache at startup so the compilation cost lands on your deploy rather
than on the first user after it.

**A local test suite** that runs with no AWS credentials and no network.
Illegal Draft 2020-12 syntax, keywords outside the Bedrock subset, a missing
`additionalProperties`, too many optional or nullable fields, an
out-of-range value after parsing, a tool spec without `strict: true`, and a
breaking change shipped without a version bump are all caught here, before
any request reaches Bedrock.

## Running the tests

```bash
pip install pytest jsonschema
pytest tests/test_schemas.py -v
```

All 49 tests pass offline. Add them to CI and run them on every change to a
schema.

## Running the agent

```bash
pip install boto3
export AWS_REGION=us-east-1
export TRIAGE_MODEL_ID=us.anthropic.claude-haiku-5-5

python agent/triage_agent.py
```

The model must support Structured Outputs. Since October 20, 2026, every
Anthropic Claude model on Bedrock does, along with models from DeepSeek,
Google, MiniMax, Mistral, Moonshot, NVIDIA, OpenAI, and Qwen. Amazon Nova and
Titan are not on AWS's provider list. Support varies by Region, endpoint, and
inference profile (for example, Claude Haiku 4.5 is excluded on the India
`in.anthropic` profile): check the model card before assuming. The triage agent and
`warm_cache` ran against Claude Haiku 5.5 on 2026-11-11 (after dropping `temperature`,
which Claude 5.x models reject).

## The two mechanisms

**Response schemas** constrain what the model writes back:

```python
outputConfig={
    "textFormat": {
        "type": "json_schema",
        "structure": {
            "jsonSchema": {
                "schema": json.dumps(MY_SCHEMA),   # Converse: a STRING
                "name": "ticket_triage_v1",
                "description": "...",
            }
        },
    }
}
```

With `InvokeModel` and Claude, the field is `output_config.format` and the
schema is an object (`as_invoke_model_output_config`). Open-weight models use
`response_format`.

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

For an agent, the second one usually matters more.

## Things that will bite you

1. **Converse wants the schema as a JSON string**; InvokeModel with Claude
   wants an object. Passing the wrong one produces an error that reads like a
   schema problem.
2. **Unsupported keywords return HTTP 400**: `minimum`, `maximum`,
   `multipleOf`, `minLength`, `maxLength`, recursive schemas, external `$ref`,
   `additionalProperties` other than `false`.
3. **Limits**: 12 optional properties per schema, 16 union-typed properties.
   The optional-property limit is enforced during compilation, so that 400
   can take ~90 s to arrive.
4. **First compile takes up to ~90 s.** Set the client read timeout to at
   least 120 s and warm the cache at deploy time. Grammars are cached for 24
   hours from first access, per account, keyed on the schema itself. A
   changed schema compiles fresh regardless of its name; the versioned
   `qualified_name` exists for logs, dashboards, and consumers.
5. **Check `stopReason` before parsing.** `max_tokens` means truncated,
   incomplete JSON even with a schema.
6. **Structured Outputs and Anthropic's citations feature cannot be combined**
   in the same request (HTTP 400).
7. **The schema constrains shape, not truth.** Chapter 9 Section 9.7 covers
   what the schema does not buy you; Chapters 5 and 10 cover what does.

## License

Code in this folder is MIT licensed. The accompanying book chapter is
copyrighted; the code is explicitly intended for reuse.
