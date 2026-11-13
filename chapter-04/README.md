# Chapter 4 — Bedrock Guardrails: The Model-Layer Companion to Policy

This folder accompanies Chapter 4 of *AgentCore in Production: The Operator's Playbook*.

> **2nd edition (October 2026).** Adds Gate 4 in the agent loop with
> `InvokeGuardrailChecks` (June 16, 2026) and two Dogwood policies that run
> Guardrails inside AgentCore Policy at the Gateway (June 17, 2026). See
> Chapter 4, Sections 4.3 and 4.5.

## What's here

```
chapter-04/
├── guardrails/
│   └── acme-support-guardrail.yaml     # CloudFormation reference config
├── utils/
│   ├── tag_user_input.py               # Input tagging (InvokeModel) and guardContent (Converse)
│   └── tool_result_checks.py           # Gate 4 with InvokeGuardrailChecks (detect-only)
├── policies/
│   ├── gate3_forbid_prompt_injection_in_ticket_text.dogwood
│   └── gate4_suppress_ssn_in_tool_output.dogwood
└── tests/
    └── test_guardrail_corpus.py        # ApplyGuardrail-based test harness
```

## Tiers, languages, and cost (October 2026)

- Content filters and denied topics have Classic and Standard tiers (June
  2025). Standard covers up to 60 languages and handles typos and context
  better, and requires cross-Region inference through a guardrail profile.
  Contextual grounding gained a Standard tier on October 20, 2026 for
  conversational and reasoning answers; Classic grounding supports English,
  French, and Spanish. The template below uses the default (Classic) tiers;
  see the comment in the template to switch.
- Each filter can use the action `NONE` (detect without blocking) for shadow
  rollouts.
- Pricing per 1,000 text units: content filters $0.15, denied topics $0.15,
  built-in PII $0.10, contextual grounding $0.10, word filters and regex free.
  `InvokeGuardrailChecks`: $0.07 content, $0.08 prompt attack, $0.10 PII.

## Guardrails at the Gateway

The files in `policies/` are AgentCore Policy statements (Dogwood) with
`when guardrails { ... }` conditions. Submit them with
`definition={"policy": {"statement": ...}}` (see
`chapter-03/ci/submit_for_validation.py`). The Gateway execution role needs
`bedrock:InvokeGuardrailChecks`. Available in seven Regions as of October
2026 (us-east-1, us-east-2, us-west-2, ap-southeast-2, ap-northeast-1,
eu-west-2, eu-north-1). Untested by the author.

## What this delivers

**A reference Guardrail configuration** for a customer-facing multi-tenant
support agent. The CloudFormation template in `guardrails/` deploys a complete
Guardrail with all six safeguard types tuned for Acme SaaS's use case:

- Content filters at HIGH strength for sexual, violence, hate, misconduct
  (INSULTS at MEDIUM on input, HIGH on output)
- Prompt attack detection at HIGH strength on input
- PII protection (built-in types + custom Acme regex patterns)
- Topic restrictions (legal advice, financial advice, medical diagnosis)
- Contextual grounding at threshold 0.75, plus a RELEVANCE check at 0.65
- A managed profanity word list

**A dynamic input tagging utility** for protecting against prompt injection.
The `wrap_user_input()` function generates per-request unique tags so attackers
cannot pre-compute the tag string and inject content outside the guard
boundary. See Section 4.4.2 of the book for the threat model this addresses.

**A test harness using ApplyGuardrail** to validate the Guardrail configuration
against a corpus of representative inputs. Run before every deployment to
catch regressions in classification behavior.

## Deploying the reference Guardrail

```bash
aws cloudformation deploy \
  --template-file guardrails/acme-support-guardrail.yaml \
  --stack-name acme-support-guardrail \
  --parameter-overrides Environment=staging \
  --region us-east-1
```

This creates a Guardrail in DRAFT mode and a numbered version. Output values
include the Guardrail ID and ARN.

## Running the test harness

```bash
pip install boto3 pytest

# Set the Guardrail to test against
export GUARDRAIL_ID=<id-from-deployment>
export GUARDRAIL_VERSION=DRAFT    # or specific version number
export AWS_REGION=us-east-1

# Run the corpus
pytest tests/test_guardrail_corpus.py -v
```

The harness uses the `ApplyGuardrail` API, which evaluates content against
the Guardrail **without invoking any foundation model**. This makes the
tests fast (no model latency), cheap (no token cost), and portable across
any model the Guardrail is later attached to. The classifiers themselves are
probabilistic, so a case near a threshold can flip between runs; keep those
cases out of the blocking set or assert on ranges.

## Using the dynamic tagging utility

**Important: the tag mechanism below (`amazon-bedrock-guardrails-guardContent_xyz`
text tags) only works with `InvokeModel` / `InvokeModelWithResponseStream`.**
Per AWS's documentation, `Converse`/`ConverseStream` does not recognize these
tags at all — it uses a different, structured mechanism instead. Pick the
section below that matches the API you call. If you are not sure which one
your codebase uses, check for `bedrock_runtime.converse(...)` /
`converse_stream(...)` versus `invoke_model(...)` / `invoke_model_with_response_stream(...)`.

### With InvokeModel / InvokeModelWithResponseStream

The tags alone are not sufficient — the request body must also declare the
tag suffix under `amazon-bedrock-guardrailConfig`, or the Guardrail has no
way to know which suffix to look for. Use `build_protected_prompt_with_config`,
which returns both the tagged prompt and that body fragment:

```python
import json
from utils.tag_user_input import build_protected_prompt_with_config

system_prompt = "You are Acme SaaS support. Help with billing questions only."
user_message = request.json["message"]

prompt, guardrail_cfg = build_protected_prompt_with_config(
    system_prompt=system_prompt,
    user_input=user_message,
    additional_context=retrieved_kb_context,    # optional, trusted
)

response = bedrock_runtime.invoke_model(
    modelId="anthropic.claude-...",
    guardrailIdentifier=GUARDRAIL_ID,
    guardrailVersion=GUARDRAIL_VERSION,
    body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
        **guardrail_cfg,    # {"amazon-bedrock-guardrailConfig": {"tagSuffix": ...}}
    }),
)
```

`guardrailIdentifier` and `guardrailVersion` are separate arguments to
`invoke_model()` itself, not part of the JSON body. Every call generates a
fresh random tag suffix, so the tag — and the matching `tagSuffix` in the
body — is unguessable per-request.

`build_protected_prompt` (without `_with_config`) still works exactly as
before for callers who manage the tag suffix and request body themselves;
`build_protected_prompt_with_config` exists because it is easy to forget the
body needs to reference the suffix at all.

### With Converse / ConverseStream

Converse has its own native mechanism: a `guardContent` field on a message
content block (`GuardrailConverseContentBlock`), with no tag suffix
involved. Use `build_protected_converse_messages`, which wraps the untrusted
user input (and, if supplied, tags retrieved context as a grounding source
for the contextual grounding checks this Guardrail configures):

```python
from utils.tag_user_input import build_protected_converse_messages

system_prompt = "You are Acme SaaS support. Help with billing questions only."
user_message = request.json["message"]

messages = build_protected_converse_messages(
    user_input=user_message,
    additional_context=retrieved_kb_context,    # optional, tagged "grounding_source"
)

response = bedrock_runtime.converse(
    modelId="anthropic.claude-...",
    system=[{"text": system_prompt}],
    messages=messages,
    guardrailConfig={
        "guardrailIdentifier": GUARDRAIL_ID,
        "guardrailVersion": GUARDRAIL_VERSION,
        "trace": "enabled",
    },
)
```

**Qualifiers decide which policies run, not just what role a block plays.**
This is the detail that silently disables the protection you think you
configured. Per the [contextual grounding
documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html):

| Qualifier | Contextual grounding | Every other policy |
|---|---|---|
| `grounding_source` | evaluated as reference | **not evaluated** |
| `query` | evaluated as query | **not evaluated** |
| `guard_content` | evaluated as response | evaluated |
| *(none)* | evaluated as response | evaluated |
| `["grounding_source", "guard_content"]` | evaluated as reference | evaluated |
| `["query", "guard_content"]` | evaluated as query | evaluated |

"Every other policy" means prompt-attack detection, content filters, word
filters, topic filters and PII detection — everything this chapter's Guardrail
is for. So untrusted user input tagged `["query"]` alone would sail past all of
it while still looking correctly instrumented. `build_protected_converse_messages`
therefore tags user input `["query", "guard_content"]`: `query` so the grounding
check has a question to score relevance against, `guard_content` so the filters
still apply.

Retrieved context gets `["grounding_source"]` alone on purpose — it is trusted,
and excluding it avoids re-scanning the same corpus every turn. If your
retrieval source is not trusted, add `guard_content` to it too.

Once any message contains a `guardContent` block, the Guardrail evaluates
only the content inside those blocks — everything else in the message is
skipped. The system prompt above is left untagged deliberately, since it is
trusted; wrap it in `guardContent` too (see `build_guard_content_block`) only
if you also want the Guardrail to evaluate it.

## Adapting for your environment

Four substitutions are needed before this is production-ready for your use:

1. **Replace the Acme-specific topic policies** in `acme-support-guardrail.yaml`
   with topics relevant to your domain. The current denied topics (legal,
   financial, medical advice) are examples — your domain has its own.

2. **Replace the Acme regex patterns** for sensitive information. The current
   patterns (`ACM-XXXXXX-XX`, `AKACME...`) are illustrative. Your organization's
   sensitive patterns will differ — internal IDs, API key formats, customer
   codes, etc.

3. **Replace the placeholder blocked-word list** (`competitor-name-1`,
   `internal-codename-x`) in the `WordPolicyConfig` section with terms
   relevant to your business — actual competitor names, internal codenames,
   deprecated product names, or anything your legal or brand team has
   flagged. The managed profanity list is fine as-is; the custom word list
   is placeholder text.

4. **Expand the test corpus** in `test_guardrail_corpus.py`. The included
   corpus has thirteen cases, including two contextual-grounding cases that
   send a `grounding_source` and a `query` alongside the answer under test —
   the only way the GROUNDING and RELEVANCE filters get anything to score.
   Grow it from real input patterns in your traffic logs and add a case
   for every incident review.

## License

Code and configurations in this folder are MIT licensed. The accompanying
book chapter is copyrighted; the code is explicitly intended for reuse.
