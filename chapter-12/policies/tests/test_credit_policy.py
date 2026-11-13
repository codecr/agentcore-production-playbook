"""
test_credit_policy.py — Tests for credit_requires_user_principal.cedar.

Accompanies Chapter 12 (Section 12.6). Same pattern as chapter-03/tests/:
one positive case and one negative case per guard. Plus the two cases the
opener of the chapter is made of: the orchestrator's service identity, and a
real user whose role does not cover the amount.

Requires:
    pip install cedarpy pytest

Status: written against the chapter-03 harness; the six tests pass with cedarpy 4.8.7
(run on 2026-11-11).
Only the plain Cedar file is tested here: cedarpy does not evaluate Dogwood
temporal policies (.dogwood files).
"""

from pathlib import Path

import cedarpy
import pytest

POLICY_FILE = Path(__file__).parent.parent / "credit_requires_user_principal.cedar"
GATEWAY = (
    'AgentCore::Gateway::'
    '"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw"'
)
ACTION = 'AgentCore::Action::"refunds___issue_credit"'


def user(uid: str, **tags) -> dict:
    return {
        "uid": {"type": "AgentCore::OAuthUser", "id": uid},
        "attrs": {},
        "parents": [],
        "tags": tags,
    }


def decide(principal_uid: str, entity: dict, tenant_id: str, amount: int):
    result = cedarpy.is_authorized(
        request={
            "principal": f'AgentCore::OAuthUser::"{principal_uid}"',
            "action": ACTION,
            "resource": GATEWAY,
            "context": {"input": {"tenant_id": tenant_id, "amount": amount}},
        },
        policies=POLICY_FILE.read_text(),
        entities=[entity],
    )
    return result.decision


@pytest.fixture
def account_manager():
    return user("user-acme-am-007", tenant="globex-corp", user_role="account_manager")


def test_account_manager_within_limit_is_permitted(account_manager):
    assert decide("user-acme-am-007", account_manager, "globex-corp", 3840) == cedarpy.Decision.Allow


def test_account_manager_over_limit_is_denied(account_manager):
    assert decide("user-acme-am-007", account_manager, "globex-corp", 5001) == cedarpy.Decision.Deny


def test_wrong_tenant_is_denied(account_manager):
    assert decide("user-acme-am-007", account_manager, "logistics-co", 100) == cedarpy.Decision.Deny


def test_billing_admin_capped_at_500():
    admin = user("user-globex-admin", tenant="globex-corp", user_role="billing_admin")
    assert decide("user-globex-admin", admin, "globex-corp", 500) == cedarpy.Decision.Allow
    assert decide("user-globex-admin", admin, "globex-corp", 501) == cedarpy.Decision.Deny


def test_opener_clerk_cannot_credit_3840():
    """Section 12.1: a Globex AP clerk, whatever the A2A message claims."""
    clerk = user("user-globex-ap-014", tenant="globex-corp", user_role="billing_viewer")
    assert decide("user-globex-ap-014", clerk, "globex-corp", 3840) == cedarpy.Decision.Deny


def test_opener_service_identity_without_user_claims_is_denied():
    """Section 12.1: the orchestrator's own identity carries no user_role."""
    service = user("support-orchestrator")
    assert decide("support-orchestrator", service, "globex-corp", 3840) == cedarpy.Decision.Deny
