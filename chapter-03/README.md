# Chapter 3 — AgentCore Policy with Cedar: The Wall Outside the Reasoning Loop

This folder accompanies Chapter 3 of *AgentCore in Production: The Operator's Playbook*.

> **2nd edition (October 2026).** Policies 01, 02, 03, 05, 06, 09, 13 and 14
> were redesigned so every condition rests on a trusted input (JWT claims,
> the Target, `context.system.now`, an interceptor-verified fact, or session
> history). Policies 09, 10, 11, 12 and 14 are now `forbid ... unless`
> because as permits they widened access. Policy 13 moved to Dogwood, and
> policies 16 and 17 are new Dogwood temporal policies. See `MIGRATIONS.md`
> and Chapter 3, Sections 3.5 and 3.7.

## What's here

```
chapter-03/
├── policies/                     # 14 Cedar policies (01-12, 14, 15)
│   └── temporal/                 # Dogwood temporal policies: 13, 16 (+16a), 17
├── interceptor/
│   └── trusted_context_interceptor.py   # Gateway REQUEST interceptor (Section 3.5)
├── tests/
│   ├── test_tenant_isolation.py         # policies 04, 05, 06
│   ├── test_trusted_inputs.py           # provenance regressions (01-03, 08/12, 09, 14)
│   ├── test_trusted_context_interceptor.py
│   └── historical-corpus.jsonl          # replayed by ci/decision_diff.py
├── ci/
│   ├── policy-validation.yml
│   ├── submit_for_validation.py         # Cedar and Dogwood; waits for ACTIVE / CREATE_FAILED
│   └── decision_diff.py
├── observability/
│   └── policy_denies.logsinsights       # Section 3.9 query
└── MIGRATIONS.md
```

## Before you use these

**These policies are templates, not finished products.** Three things need to happen before they work in your environment:

1. **Replace the placeholder ARN.** Every policy contains:
   ```
   resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID"
   ```
   Replace `REGION`, `ACCOUNT`, and `GATEWAY_ID` with values from your deployment. The CI pipeline's Cedar-syntax-validation step (`ci/policy-validation.yml`) does a version of this substitution too, reading `REGION`/`ACCOUNT`/`GATEWAY_ID` from the environment (with defaults so the step still runs if you haven't set them) so it has something concrete to parse — that's a validation-time convenience, not a substitute for replacing the placeholder in the policy you actually deploy.

2. **Match the action names to your Gateway Targets.** Cedar actions follow the pattern `AgentCore::Action::"TargetName___tool_name"`. The names in these policies (`devops___restart_service`, `billing___process_refund`, etc.) reflect the example Acme SaaS deployment. Yours will differ.

3. **Validate before enforcement.** Run every policy through `validationMode=FAIL_ON_ANY_FINDINGS` in the `create_policy` API call. AgentCore's automated reasoning will flag overly permissive policies, overly restrictive policies, and policies with unsatisfiable conditions.

## Trusted inputs: the interceptor

Policies 02, 06 and 14 read fields that only the REQUEST interceptor may write
(`approval_verified`, `ticket_tenant`, `auth_age_seconds`). Deploy
`interceptor/trusted_context_interceptor.py` as a Lambda, wire
`verify_approval` and `lookup_ticket_tenant` to your approval service and
ticketing system, and attach it to the Gateway with
`interceptionPoints=["REQUEST"]` and `inputConfiguration.passRequestHeaders=True`.
Declare those fields in the tools' input schemas (Policy validates conditions
against the schema generated from them) and describe them as "set by the
gateway; leave empty". The interceptor rejects any call where the model filled
them. The Gateway runs the REQUEST interceptor before Policy evaluation, per
AWS's documentation and confirmed against a live Gateway on 2026-11-11.

## Temporal (Dogwood) policies

Files in `policies/temporal/` are Dogwood, a superset of Cedar accepted by
AgentCore Policy since August 6, 2026. `cedarpy` cannot parse them, so they are
validated only on AWS (`definition={"policy": {"statement": ...}}`). Each
request must carry `x-amzn-bedrock-agentcore-policy-session-id`, chosen by
your backend (for example HMAC(user, conversation)), never by the agent. The
Gateway role needs `bedrock-agentcore:GetWorkloadAccessToken`. Changing any
temporal policy invalidates live sessions; through the Gateway's MCP path the
author observed HTTP 200 with JSON-RPC code -32005 (not the documented 409).
Quotas: 20 temporal policies per engine, 3 operators per policy, 24 h window.

## How to deploy

The AgentCore CLI (`@aws/agentcore`, v1.0 since October 2026) is one path; recheck flags against `agentcore add policy --help` for your version:

```bash
# Install
npm install -g @aws/agentcore

# In your project root
agentcore add policy --name TenantScopedDataAccess \
                     --engine AcmePolicyEngine \
                     --source policies/04_tenant_scoped_data_access.cedar \
                     --gateway acme-support-gateway

# Repeat for each policy, or script the loop
```

For boto3, use `bedrock-agentcore-control` `create_policy` (boto3 >= 1.43.100
for the Dogwood `policy` definition member). `create_policy` is asynchronous:
poll `get_policy` (or the `policy_active` waiter) for `ACTIVE` or
`CREATE_FAILED`. There is no policy versioning inside the engine; Git is the
version store and rollback means re-applying the previous file.

## Staged rollout: LOG_ONLY first

Never go from "no policy" to ENFORCE mode in one step. The pattern that works:

1. Create new policies with `enforcementMode=LOG_ONLY` (per-policy shadow
   mode inside an ENFORCE engine), or associate a whole new engine in
   `LOG_ONLY` mode
2. Watch the `LogOnlyDecisionFlips` metric and run
   `observability/policy_denies.logsinsights` for 1-2 weeks
3. Inspect every request that would have been denied: was it correct?
4. Promote to `ACTIVE` as a separate, manual step

`LOG_ONLY` is billed like enforcement (per authorization request), so it is
a dry run, not a free one.

## Testing

The `tests/` folder contains 42 tests: tenant isolation (04, 05, 06), the
provenance regressions from Section 3.5, and the interceptor. Dogwood
policies are not covered locally.

Tests use the `cedarpy` Python bindings to evaluate policies locally — no AWS calls, no network, fast iteration:

```bash
cd tests/
pip install "cedarpy==4.8.7" pytest
pytest -v
```

The `cedarpy` version is pinned deliberately: its public API (`PolicySet.from_str`, the `Decision` enum) has changed across minor versions, and this repo's code is written against 4.8.7.

For property-based testing with `hypothesis`, see Chapter 3 Section 3.8 of the book.

## CI/CD pattern

The `ci/` folder contains a GitHub Actions workflow that:

1. On every PR touching `policies/`, `tests/` or `interceptor/`, runs Cedar syntax validation (Cedar files only)
2. Runs the pytest suite
3. Calls `create_policy` with `validationMode=FAIL_ON_ANY_FINDINGS` against a staging Policy Engine, via `ci/submit_for_validation.py`, for Cedar and Dogwood files, and waits for the verdict
4. Diffs the decisions against a corpus of historical requests (`tests/historical-corpus.jsonl`), via `ci/decision_diff.py`
5. Blocks merge if any check fails

`ci/submit_for_validation.py` supports a `--dry-run` flag that prints what it would submit without calling AWS — useful for exercising the workflow without a real staging Policy Engine. `ci/decision_diff.py` runs fully locally against two Cedar policy directories (no AWS calls); its `--baseline-policies` flag accepts the documented `branch:path` form for compatibility with this workflow's invocation, but it does not check out that git ref itself — it only reads whatever is on disk at the path component today. Wire up a real `git worktree` checkout of the baseline ref if you need a true point-in-time comparison rather than a second local directory.

See Chapter 3 Section 3.9 for the full workflow.

## License

The Cedar policies in this folder are licensed MIT so you can use them in your own work without restriction. The accompanying book chapter is copyrighted; the policies are explicitly intended to be reused.

## Reading order

If you are using this folder without having read Chapter 3, the recommended reading order is:

1. Start with `policies/04_tenant_scoped_data_access.cedar`, the canonical example
2. Then `policies/01_allow_writes_only_in_staging_dev.cedar` and `03_...`, the environment guard built on the Target and a claim
3. Then `policies/08_amount_limit_by_role.cedar` with `12_numeric_range_constraints.cedar`: why limits are forbids
4. Then `interceptor/` with policies 02, 06 and 14
5. Then `policies/temporal/`

Chapter 3 explains the *why* behind each one. These files explain the *what*.
