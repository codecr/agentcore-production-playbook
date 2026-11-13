"""
test_trusted_inputs.py: the 2nd-edition regression suite for Section 3.5.

Each test pins down one of the provenance fixes:
  01  environment comes from the Target (action name), not from an argument
  02  destructive calls need approval_verified, which only the interceptor writes
  03  a staging-bound session can never reach a production Target
  09  business hours come from context.system.now, not from an argument
  08+12  sanity bounds are forbids, so they cannot widen the role caps
  14  freshness comes from auth_age_seconds, written by the interceptor

Requires: pip install "cedarpy==4.8.7" pytest
"""

from pathlib import Path

import cedarpy
import pytest

POLICY_DIR = Path(__file__).parent.parent / "policies"
ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gateway"
GATEWAY = f'AgentCore::Gateway::"{ARN}"'
ALLOW, DENY = cedarpy.Decision.Allow, cedarpy.Decision.Deny


def load(*names: str) -> str:
    texts = []
    for name in names:
        raw = next(POLICY_DIR.glob(f"{name}_*.cedar")).read_text(encoding="utf-8")
        texts.append(raw.replace(
            "arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID", ARN))
    return "\n\n".join(texts)


def oauth(uid: str, **tags) -> tuple[str, list]:
    entity = {"uid": {"type": "AgentCore::OAuthUser", "id": uid},
              "attrs": {"id": uid}, "parents": [], "tags": tags}
    return f'AgentCore::OAuthUser::"{uid}"', [entity]


def iam(role_arn: str) -> tuple[str, list]:
    entity = {"uid": {"type": "AgentCore::IamEntity", "id": role_arn},
              "attrs": {"id": role_arn}, "parents": []}
    return f'AgentCore::IamEntity::"{role_arn}"', [entity]


def decide(policies, principal, action, input_=None, now=None):
    principal_uid, entities = principal
    context = {"input": input_ or {}}
    if now:
        context["system"] = {"now": {"__extn": {"fn": "datetime", "arg": now}}}
    return cedarpy.is_authorized(
        {"principal": principal_uid,
         "action": f'AgentCore::Action::"{action}"',
         "resource": GATEWAY, "context": context},
        policies, entities).decision


# ---------------------------------------------------------------- policy 01
class TestPolicy01EnvironmentFromTarget:
    def test_staging_target_allowed(self):
        assert decide(load("01"), oauth("ops"), "devops-staging___restart_service") == ALLOW

    def test_prod_target_denied_whatever_the_arguments_say(self):
        """The 1st-edition bypass: the model claims staging on a prod call."""
        assert decide(load("01"), oauth("ops"), "devops-prod___restart_service",
                      {"environment": "staging"}) == DENY


# ---------------------------------------------------------------- policy 02
class TestPolicy02ApprovalVerifiedOutsideTheModel:
    def test_verified_approval_allows(self):
        assert decide(load("02"), oauth("ops"), "devops-prod___terminate_instance",
                      {"instance_id": "i-0abc", "approval_verified": True}) == ALLOW

    def test_token_shaped_string_no_longer_works(self):
        """approval_token like "approved-*" was forgeable; it now proves nothing."""
        assert decide(load("02"), oauth("ops"), "devops-prod___terminate_instance",
                      {"instance_id": "i-0abc", "approval_token": "approved-9911"}) == DENY

    def test_unverified_approval_denied(self):
        assert decide(load("02"), oauth("ops"), "devops-prod___terminate_instance",
                      {"instance_id": "i-0abc", "approval_verified": False}) == DENY


# ---------------------------------------------------------------- policy 03
class TestPolicy03SessionBoundToEnvironment:
    @pytest.fixture
    def policies(self):
        # 02 grants prod termination; 03 must still forbid a staging session.
        return load("02", "03")

    def test_staging_session_cannot_reach_prod_even_with_approval(self, policies):
        assert decide(policies, oauth("ops", env="staging"),
                      "devops-prod___terminate_instance",
                      {"approval_verified": True}) == DENY

    def test_session_without_env_claim_cannot_reach_prod(self, policies):
        assert decide(policies, oauth("ops"), "devops-prod___terminate_instance",
                      {"approval_verified": True}) == DENY

    def test_prod_session_with_approval_allowed(self, policies):
        assert decide(policies, oauth("ops", env="prod"),
                      "devops-prod___terminate_instance",
                      {"approval_verified": True}) == ALLOW

    def test_prodops_iam_role_allowed(self, policies):
        role = "arn:aws:sts::111122223333:assumed-role/ProdOpsAgentRole"
        assert decide(policies, iam(role), "devops-prod___terminate_instance",
                      {"approval_verified": True}) == ALLOW

    def test_other_iam_role_denied(self, policies):
        role = "arn:aws:sts::111122223333:assumed-role/StagingAgentRole"
        assert decide(policies, iam(role), "devops-prod___terminate_instance",
                      {"approval_verified": True}) == DENY


# ---------------------------------------------------------------- policy 09
class TestPolicy09ClockFromTheService:
    @pytest.fixture
    def policies(self):
        # A grant for deploys, so we can see 09 subtract from it.
        grant = ('permit (principal, action == AgentCore::Action::"devops-prod___deploy", '
                 f'resource == AgentCore::Gateway::"{ARN}");')
        return load("09") + "\n" + grant

    def test_inside_window_allowed(self, policies):
        assert decide(policies, oauth("ops"), "devops-prod___deploy",
                      now="2026-10-26T16:00:00Z") == ALLOW

    def test_outside_window_denied(self, policies):
        assert decide(policies, oauth("ops"), "devops-prod___deploy",
                      now="2026-10-26T04:30:00Z") == DENY

    def test_model_written_hour_is_ignored(self, policies):
        assert decide(policies, oauth("ops"), "devops-prod___deploy",
                      {"request_hour": 10}, now="2026-10-26T04:30:00Z") == DENY


# ------------------------------------------------------- policies 08 and 12
class TestSanityBoundsCannotWidenRoleCaps:
    @pytest.fixture
    def policies(self):
        return load("08", "12")

    def test_support_agent_within_cap(self, policies):
        assert decide(policies, oauth("s1", role="support_agent"),
                      "billing___process_refund", {"amount": 150}) == ALLOW

    def test_support_agent_over_cap_denied(self, policies):
        """In the 1st edition, permit 12 allowed this ($300 < $50,000)."""
        assert decide(policies, oauth("s1", role="support_agent"),
                      "billing___process_refund", {"amount": 300}) == DENY

    def test_zero_amount_denied_for_manager(self, policies):
        assert decide(policies, oauth("m1", role="manager"),
                      "billing___process_refund", {"amount": 0}) == DENY


# ---------------------------------------------------------------- policy 14
class TestPolicy14FreshnessFromInterceptor:
    @pytest.fixture
    def policies(self):
        return load("08", "14")

    def test_fresh_session_allowed(self, policies):
        assert decide(policies, oauth("s1", role="supervisor"), "billing___process_refund",
                      {"amount": 500, "auth_age_seconds": 120}) == ALLOW

    def test_stale_session_denied(self, policies):
        assert decide(policies, oauth("s1", role="supervisor"), "billing___process_refund",
                      {"amount": 500, "auth_age_seconds": 7200}) == DENY

    def test_missing_age_denied(self, policies):
        assert decide(policies, oauth("s1", role="supervisor"), "billing___process_refund",
                      {"amount": 500}) == DENY
