# Chapter 3: AgentCore Policy with Cedar

This folder accompanies Chapter 3 of *AgentCore in Production: The Operator's Playbook*.

## What's here

```
chapter-03/
├── policies/                    # 15 production-grade Cedar policies
│   ├── 01_allow_writes_only_in_staging_dev.cedar
│   ├── 02_allow_destructive_only_with_explicit_approval.cedar
│   ├── 03_forbid_cross_environment_calls.cedar
│   ├── 04_tenant_scoped_data_access.cedar
│   ├── 05_forbid_cross_tenant_lookups.cedar
│   ├── 06_admin_tenant_override_with_audit.cedar
│   ├── 07_role_based_tool_access.cedar
│   ├── 08_amount_limit_by_role.cedar
│   ├── 09_time_of_day_restrictions.cedar
│   ├── 10_required_parameters_present.cedar
│   ├── 11_input_format_constraints.cedar
│   ├── 12_numeric_range_constraints.cedar
│   ├── 13_rate_limit_destructive_actions.cedar
│   ├── 14_require_session_freshness.cedar
│   └── 15_explicit_forbid_for_known_bad_patterns.cedar
├── tests/                       # Sample pytest test suite + historical corpus
│   ├── test_tenant_isolation.py
│   └── historical-corpus.jsonl  # Synthetic requests replayed by ci/decision_diff.py
└── ci/                          # GitHub Actions workflow for policy validation
    ├── policy-validation.yml
    ├── submit_for_validation.py # Step 3: submits a policy to create_policy (--dry-run supported)
    └── decision_diff.py         # Step 4: replays the corpus against two policy bundles
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

## How to deploy

The recommended deployment path uses the AgentCore CLI (`@aws/agentcore`), which became the official tool in April 2026:

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

For teams that prefer Python or boto3 directly, the older `bedrock-agentcore-control` API still works. See the AWS documentation for `start_policy_generation`, `create_policy`, and `update_policy_engine`.

## Staged rollout: LOG_ONLY first

Never go from "no policy" to ENFORCE mode in one step. The pattern that works:

1. Deploy policies to a Policy Engine in LOG_ONLY mode
2. Monitor CloudWatch logs for 1–2 weeks
3. Inspect every DENY decision: was it correct? If not, fix the policy
4. Inspect every PERMIT that you didn't expect to fire: is the policy too broad?
5. Only after the log shows the policies behaving correctly, promote to ENFORCE

The cost difference between LOG_ONLY and ENFORCE is zero (you pay per evaluation either way). LOG_ONLY is a free dry-run.

## Testing

The `tests/` folder contains a representative test suite for the tenant isolation policies (policies 04 and 05). The pattern extends to the other twelve.

Tests use the `cedarpy` Python bindings to evaluate policies locally — no AWS calls, no network, fast iteration:

```bash
cd tests/
pip install "cedarpy==4.8.7" pytest
pytest test_tenant_isolation.py -v
```

The `cedarpy` version is pinned deliberately: its public API (`PolicySet.from_str`, the `Decision` enum) has changed across minor versions, and this repo's code is written against 4.8.7.

For property-based testing with `hypothesis`, see Chapter 3 Section 3.6 of the book.

## CI/CD pattern

The `ci/` folder contains a GitHub Actions workflow that:

1. On every PR touching `policies/*.cedar`, runs Cedar syntax validation
2. Runs the pytest suite
3. Calls `create_policy` with `validationMode=FAIL_ON_ANY_FINDINGS` against a staging Policy Engine, via `ci/submit_for_validation.py`
4. Diffs the decisions against a corpus of historical requests (`tests/historical-corpus.jsonl`), via `ci/decision_diff.py`
5. Blocks merge if any check fails

`ci/submit_for_validation.py` supports a `--dry-run` flag that prints what it would submit without calling AWS — useful for exercising the workflow without a real staging Policy Engine. `ci/decision_diff.py` runs fully locally against two Cedar policy directories (no AWS calls); its `--baseline-policies` flag accepts the documented `branch:path` form for compatibility with this workflow's invocation, but it does not check out that git ref itself — it only reads whatever is on disk at the path component today. Wire up a real `git worktree` checkout of the baseline ref if you need a true point-in-time comparison rather than a second local directory.

See Chapter 3 Section 3.7 for the full workflow.

## License

The Cedar policies in this folder are licensed MIT so you can use them in your own work without restriction. The accompanying book chapter is copyrighted; the policies are explicitly intended to be reused.

## Reading order

If you are using this folder without having read Chapter 3, the recommended reading order is:

1. Start with `policies/04_tenant_scoped_data_access.cedar` — it's the canonical example
2. Then `policies/01_allow_writes_only_in_staging_dev.cedar` — the simplest environment guard
3. Then `policies/08_amount_limit_by_role.cedar` — shows multi-clause conditions
4. Then the rest in numerical order

Chapter 3 explains the *why* behind each one. These files explain the *what*.
