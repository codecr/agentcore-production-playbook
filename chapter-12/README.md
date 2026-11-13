# Chapter 12: Multi-Agent Systems and A2A in Production

This folder accompanies Chapter 12 of *AgentCore in Production: The Operator's Playbook*, 2nd edition (new in this edition). In the first edition, Chapter 12 was the operations playbook; it is now Chapter 13 and lives in `chapter-13/`.

State of AWS described here: October 2026. A2A servers on AgentCore Runtime (since GA, October 2025), Gateway HTTP passthrough targets with `protocolType: A2A` (July 2026), AgentCore Identity on-behalf-of token exchange (April 2026), temporal policies in Dogwood (August 6, 2026), per-agent span log groups (agents created from July 20, 2026), AWS Agent Registry GA under the `agent-registry` namespace (August 31, 2026).

## What's here

```
chapter-12/
├── identity/
│   └── hop_authority.py               # authority from verified claims, payload claims logged as data
├── policies/
│   ├── credit_requires_user_principal.cedar   # 12.6: no user in the token, no credit
│   ├── approval_before_credit.dogwood              # 12.6: approval as a recorded action (Dogwood)
│   ├── a2a_fanout_limit.dogwood                    # 12.8: per-session delegation limit (Dogwood count)
│   ├── kill_switch_forbid_delegation.cedar    # 12.10: stateless kill switch
│   └── tests/test_credit_policy.py            # cedarpy tests for the Cedar policy
├── observability/
│   ├── multi_log_group_query.py       # 12.7: builds the span log-group list from deployed runtimes
│   └── multi_log_group_query.sh       # 12.7: the snippet from the chapter
├── amplification/
│   └── hop_budget.py                  # 12.8: hop limit, deadline, call budget, single-layer retry
├── containment/
│   └── kill_switch.py                 # 12.10: dry-run containment plan, narrowest switch first
├── evaluation/
│   ├── delegation_golden_cases.jsonl  # 12.9: summary-fidelity and asserted-authority cases
│   └── check_summary_fidelity.py      # 12.9: the code-based assertion on outgoing A2A payloads
└── README.md
```

## Run locally (no AWS access)

```bash
python identity/hop_authority.py
python amplification/hop_budget.py
python evaluation/check_summary_fidelity.py
python observability/multi_log_group_query.py --dry-run
python containment/kill_switch.py --callee refunds-agent \
  --reason "INC-2291 unapproved credits issued" --pulled-by oncall-platform
```

These five demos were run locally (Python 3.12) while drafting the chapter. They print what the chapter describes: the `approved_by` claim logged as a mismatch, 27 leaf attempts under independent retries versus three under single-layer retry, the opener's A2A payload failing the fidelity check, the per-agent log groups plus `aws/spans`, and a containment plan that applies nothing.

## Verification status

Live-tested on 2026-11-10/11 against a lab account: the A2A action identifier and the JSON-RPC method in `context.input` (`<target>___POST:/`, `context.input.method`), `credit_requires_user_principal.cedar` (both statements reach ACTIVE under `FAIL_ON_ANY_FINDINGS`, permit first), `kill_switch_forbid_delegation.cedar`, `a2a_fanout_limit.dogwood` (the sixth call in a policy session is denied), stale-session behavior on A2A passthrough (HTTP 409), and the per-agent log group. The six tests in `policies/tests/` pass locally.

Not verified:

- **Claims on the on-behalf-of token.** `user_role`, `tenant`, and the RFC 8693 `act` claim depend on how your IdP performs the exchange. Inspect a real exchanged token first.
- **`approval_before_credit.dogwood`** ran on 2026-11-12 (with an IAM principal in the lab): it denies a credit before the approval, allows it after an approval for the same invoice in the same policy session, and denies it for another invoice or in another policy session. An approval made in a different session (an account manager approving in their own conversation) is invisible to it; Section 12.6 moves that case to durable state read by the credit tool.
- **`kill_switch.py --apply`** and **`multi_log_group_query.py`** without `--dry-run` make real control-plane and Logs calls and were not run.
- **Rate limits at the Gateway.** The limits were created and reached ACTIVE, but a burst test did not show throttling.

## Requirements

- Python 3.10+
- `boto3` only for the non-dry-run paths (current release; the `agent-registry` client requires an SDK released after August 2026)
- `cedarpy` and `pytest` for `policies/tests/`

## License

MIT for code and policies, as for the rest of the companion repository.
