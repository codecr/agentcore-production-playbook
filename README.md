# AgentCore in Production — Companion Repository

This is the companion code repository for **_AgentCore in Production: The
Operator's Playbook_**.

📖 **Get the book:** second edition, paperback ISBN 979-8142224118
([Amazon](https://www.amazon.com/dp/B0HRJ4XMP2)).

**Editions.** `main` carries the code for the second edition (November 2026),
also tagged `v2.0-second-edition`. The code exactly as published with the first
edition (August 2026) is tagged `v1.0-first-edition`.

## What this is

Eleven chapters, eleven folders, each a self-contained set of runnable artifacts —
Cedar policies, Guardrail configurations, Terraform, Lambda functions,
CloudFormation templates, and Python scripts — that the book exhibits and
explains. There is no root build, no shared dependency lockfile, and no
cross-chapter import. Each `chapter-NN/` folder stands on its own: clone the
whole repo, or download just the one chapter you need.

Every chapter's own `README.md` is the source of truth for that chapter —
what it contains, how to deploy it, and how to adapt it to your own
environment. Start there.

## Chapters

| # | Chapter | What's in the folder |
|---|---|---|
| 3 | AgentCore Policy with Cedar and Dogwood | Production Cedar policies on trusted inputs, temporal Dogwood policies, a trusted-context interceptor, a CI validation pipeline, a pytest suite |
| 4 | Bedrock Guardrails | A reference Guardrail (CloudFormation), dynamic input tagging, a test corpus |
| 5 | Automated Reasoning | A sample AR policy document, 19 test claims, a validation script |
| 6 | Session Storage | Runtime deploy scripts, a resumable agent, session lifecycle helpers |
| 7 | AgentCore Memory | A 4-strategy Memory resource (CloudFormation), a memory-aware agent, retrieval helpers |
| 8 | Bedrock Knowledge Bases | A 5-strategy chunking benchmark (Terraform), an evaluation runner, ingestion alarms |
| 9 | Structured Outputs | A production schema library, a before/after agent, a schema registry with compatibility checks |
| 10 | AgentCore Evaluations | Online evaluation config, custom evaluators, a golden-set CI quality gate, alarms |
| 11 | Agent Registry and Observability at Scale | Registry record normalization, the full publish lifecycle, IAM personas, cross-agent tracing |
| 12 | Multi-Agent Systems and A2A | Hop-authority checks, per-hop Cedar and Dogwood policies, cross-log-group trace queries, an amplification budget, a dry-run kill switch |
| 13 | The Operations Playbook | A production launch checklist, aggregate budget enforcement, payment Cedar and Dogwood policies, an incident runbook, a kill-switch procedure |

Chapters 1, 2, and the appendices have no companion code — they're conceptual
and architectural, not implementation.

## Conventions across every chapter

- **Placeholder values only.** Account IDs, ARNs, and identifiers are
  placeholders (`111122223333`, `REGION`/`ACCOUNT`/`GATEWAY_ID`-style
  templates). Replace them with your own before deploying anything.
- **Fictional example company.** Every chapter uses the same fictional
  company, **Acme SaaS** (and its lending subsidiary, **Acme Lending**), to
  keep the multi-tenant, multi-service examples coherent across chapters.
  `globex-corp` is Acme's example customer tenant.
- **Second edition (November 2026).** The code tracks the AgentCore API as of
  October 26, 2026. Agent Registry, AgentCore Payments, Harness, and
  Optimization are generally available; Session Storage is still in preview.
  Each chapter's README dates its status claims. `MIGRATIONS.md` files and the
  book's errata list what changed from the first edition, including policies
  that were unsafe as first published.
- **Staged rollout for policy changes.** Where the book uses Cedar policy
  enforcement (Chapters 3, 12, and 13), the pattern is always `LOG_ONLY` before
  `ENFORCE`, validated with `FAIL_ON_ANY_FINDINGS`. Don't skip the log phase.

## Requirements

Each chapter's `README.md` lists exactly what it needs, installed inline
(`pip install ...`) rather than through a shared lockfile — dependencies
differ across chapters. In general, expect:

- Python 3.10+
- `boto3` (recent — several chapters depend on APIs added in 2026)
- AWS CLI v2, for chapters using CloudFormation or the AgentCore CLI
- Terraform, for chapters 8 and 11
- An AWS account with access to the Bedrock and AgentCore features that
  chapter uses — most examples run locally without AWS for testing, but
  deploying them does not

## License

Code, configurations, and templates in this repository are **MIT licensed**
(see [LICENSE](LICENSE)) —
copy them into your own projects freely. The book text itself is copyrighted
and not included here; this repository is the code that accompanies it, not
the book.

## Issues

Found a bug in the companion code, or something that doesn't match the book?
Open an issue on this repository.
