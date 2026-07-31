# AgentCore in Production — Companion Repository

This is the companion code repository for **_AgentCore in Production: The
Operator's Playbook_**.

📖 **Get the book:** [link coming soon]

## What this is

Ten chapters, ten folders, each a self-contained set of runnable artifacts —
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
| 3 | AgentCore Policy with Cedar | 15 production-grade Cedar policies, a CI validation pipeline, a pytest suite |
| 4 | Bedrock Guardrails | A reference Guardrail (CloudFormation), dynamic input tagging, a test corpus |
| 5 | Automated Reasoning | A sample AR policy document, 19 test claims, a validation script |
| 6 | Session Storage | Runtime deploy scripts, a resumable agent, session lifecycle helpers |
| 7 | AgentCore Memory | A 4-strategy Memory resource (CloudFormation), a memory-aware agent, retrieval helpers |
| 8 | Bedrock Knowledge Bases | A 5-strategy chunking benchmark (Terraform), an evaluation runner, ingestion alarms |
| 9 | Structured Outputs | A production schema library, a before/after agent, a schema registry with compatibility checks |
| 10 | AgentCore Evaluations | Online evaluation config, custom evaluators, a golden-set CI quality gate, alarms |
| 11 | Agent Registry and Observability at Scale | Registry record normalization, the full publish lifecycle, IAM personas, cross-agent tracing |
| 12 | The Operations Playbook | A production launch checklist, aggregate budget enforcement, payment Cedar policies, an incident runbook |

Chapters 1, 2, and the Appendix have no companion code — they're conceptual
and architectural, not implementation.

## Conventions across every chapter

- **Placeholder values only.** Account IDs, ARNs, and identifiers are
  placeholders (`111122223333`, `REGION`/`ACCOUNT`/`GATEWAY_ID`-style
  templates). Replace them with your own before deploying anything.
- **Fictional example company.** Every chapter uses the same fictional
  company, **Acme SaaS** (and its lending subsidiary, **Acme Lending**), to
  keep the multi-tenant, multi-service examples coherent across chapters.
  `globex-corp` is Acme's example customer tenant.
- **Preview features are called out explicitly, with a date.** Several
  AgentCore capabilities used in this book were in public preview at the
  time of writing (Session Storage, AgentCore Payments, AWS Agent Registry).
  Each chapter's README says so and gives the preview start date — check the
  current status before adopting in production.
- **Staged rollout for policy changes.** Where the book uses Cedar policy
  enforcement (Chapters 3 and 12), the pattern is always `LOG_ONLY` before
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

Code, configurations, and templates in this repository are **MIT licensed** —
copy them into your own projects freely. The book text itself is copyrighted
and not included here; this repository is the code that accompanies it, not
the book.

## Issues

Found a bug in the companion code, or something that doesn't match the book?
Open an issue on this repository.
