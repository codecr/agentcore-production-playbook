"""
test_tenant_isolation.py — Sample test suite for policies 04 and 05.

This file demonstrates the testing pattern described in Chapter 3 Section 3.8.
Each policy gets at minimum: a positive test (happy path permitted) and a
negative test for each guard condition (each "no" path is denied).

Requires:
    pip install cedarpy pytest

Note: cedarpy is a Python binding to the Cedar reference implementation.
It evaluates policies locally without any AWS calls. Use it for fast
iteration and CI; the production engine on AgentCore is API-compatible
with these results.
"""

import pytest
from pathlib import Path

import cedarpy


# ============================================================================
# Helpers
# ============================================================================

POLICY_DIR = Path(__file__).parent.parent / "policies"
GATEWAY_ARN = (
    'AgentCore::Gateway::'
    '"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gateway"'
)


def load_policy(filename: str) -> str:
    """Load a Cedar policy file, with the placeholder ARN substituted."""
    raw = (POLICY_DIR / filename).read_text()
    return raw.replace(
        'arn:aws:bedrock-agentcore:REGION:ACCOUNT:gateway/GATEWAY_ID',
        'arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gateway',
    )


def evaluate(
    policy_text: str,
    principal: str,
    action: str,
    resource: str,
    context: dict,
    entities: list = None,
):
    """Evaluate a single policy against a request and return ALLOW/DENY."""
    return cedarpy.is_authorized(
        request={
            'principal': principal,
            'action': action,
            'resource': resource,
            'context': context,
        },
        policies=policy_text,
        entities=entities or [],
    )


# ============================================================================
# Policy 04 — Tenant scoped data access
# ============================================================================

class TestPolicy04TenantScopedDataAccess:
    """The canonical Acme SaaS multi-tenant policy."""

    @pytest.fixture
    def policy(self):
        return load_policy('04_tenant_scoped_data_access.cedar')

    def test_same_tenant_is_permitted(self, policy):
        """When the principal's tenant matches the request's tenant_id, ALLOW."""
        result = evaluate(
            policy_text=policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'globex-corp'}},
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-globex-001'},
                    'attrs': {},
                    'parents': [],
                    'tags': {'tenant': 'globex-corp'},
                }
            ],
        )
        assert result.decision == cedarpy.Decision.Allow

    def test_different_tenant_is_denied(self, policy):
        """When the principal's tenant differs from the request's, DENY."""
        result = evaluate(
            policy_text=policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co'}},
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-globex-001'},
                    'attrs': {},
                    'parents': [],
                    'tags': {'tenant': 'globex-corp'},
                }
            ],
        )
        assert result.decision == cedarpy.Decision.Deny

    def test_missing_tenant_claim_is_denied(self, policy):
        """When the principal has no tenant tag, DENY (default-deny)."""
        result = evaluate(
            policy_text=policy,
            principal='AgentCore::OAuthUser::"user-no-tenant"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'globex-corp'}},
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-no-tenant'},
                    'attrs': {},
                    'parents': [],
                    'tags': {},  # No tenant tag
                }
            ],
        )
        assert result.decision == cedarpy.Decision.Deny

    def test_missing_tenant_id_in_request_is_denied(self, policy):
        """When the request has no tenant_id, DENY."""
        result = evaluate(
            policy_text=policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {}},  # No tenant_id
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-globex-001'},
                    'attrs': {},
                    'parents': [],
                    'tags': {'tenant': 'globex-corp'},
                }
            ],
        )
        assert result.decision == cedarpy.Decision.Deny

    def test_unlisted_action_is_denied(self, policy):
        """An action not in the policy's action list is denied."""
        result = evaluate(
            policy_text=policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___delete_invoice"',  # Not in policy
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'globex-corp'}},
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-globex-001'},
                    'attrs': {},
                    'parents': [],
                    'tags': {'tenant': 'globex-corp'},
                }
            ],
        )
        assert result.decision == cedarpy.Decision.Deny


# ============================================================================
# Policy 05 — Forbid cross-tenant lookups (the belt-and-suspenders rule)
# ============================================================================

class TestPolicy05ForbidCrossTenantLookups:
    """Tests for the explicit forbid that catches cross-tenant attempts."""

    @pytest.fixture
    def policy(self):
        return load_policy('05_forbid_cross_tenant_lookups.cedar')

    def test_explicitly_forbids_cross_tenant(self, policy):
        """A cross-tenant request is explicitly forbidden."""
        result = evaluate(
            policy_text=policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co'}},
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-globex-001'},
                    'attrs': {},
                    'parents': [],
                    'tags': {'tenant': 'globex-corp'},
                }
            ],
        )
        assert result.decision == cedarpy.Decision.Deny

    def test_same_tenant_does_not_forbid(self, policy):
        """A same-tenant request is not forbidden by this policy.

        Note: This policy is `forbid`, so a non-match just means it doesn't
        fire — the request would still be denied by default-deny unless
        some other policy (like 04) permits it. Combined together (04 + 05),
        same-tenant is permitted and cross-tenant is forbidden.
        """
        result = evaluate(
            policy_text=policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'globex-corp'}},
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-globex-001'},
                    'attrs': {},
                    'parents': [],
                    'tags': {'tenant': 'globex-corp'},
                }
            ],
        )
        # Default-deny because policy 05 alone doesn't permit; only forbids.
        assert result.decision == cedarpy.Decision.Deny


# ============================================================================
# Combined evaluation: policies 04 AND 05 together
# ============================================================================

class TestPolicies04And05Combined:
    """When evaluated together, policy 04 permits same-tenant and policy 05
    explicitly forbids cross-tenant. Cedar's forbid-overrides-permit model
    ensures the right outcome in every case."""

    @pytest.fixture
    def combined_policy(self):
        return (
            load_policy('04_tenant_scoped_data_access.cedar')
            + '\n\n'
            + load_policy('05_forbid_cross_tenant_lookups.cedar')
        )

    def test_same_tenant_permitted(self, combined_policy):
        result = evaluate(
            policy_text=combined_policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'globex-corp'}},
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-globex-001'},
                    'attrs': {},
                    'parents': [],
                    'tags': {'tenant': 'globex-corp'},
                }
            ],
        )
        assert result.decision == cedarpy.Decision.Allow

    def test_cross_tenant_denied_by_forbid(self, combined_policy):
        """Even if some future bug made policy 04 too permissive, policy 05
        explicitly forbids and forbid-overrides-permit ensures denial."""
        result = evaluate(
            policy_text=combined_policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co'}},
            entities=[
                {
                    'uid': {'type': 'AgentCore::OAuthUser', 'id': 'user-globex-001'},
                    'attrs': {},
                    'parents': [],
                    'tags': {'tenant': 'globex-corp'},
                }
            ],
        )
        assert result.decision == cedarpy.Decision.Deny


# ============================================================================
# Combined evaluation: policies 05 AND 06 together (the admin audit override)
# ============================================================================

def _admin_entities(uid: str, tenant: str, role: str) -> list:
    """An OAuthUser carrying both a tenant tag and a role tag."""
    return [
        {
            'uid': {'type': 'AgentCore::OAuthUser', 'id': uid},
            'attrs': {},
            'parents': [],
            'tags': {'tenant': tenant, 'role': role},
        }
    ]


class TestPolicies05And06Combined:
    """Policy 06 grants tenant admins a ticket-verified cross-tenant override.

    Cedar's forbid-overrides-permit model means a bare `forbid` in policy 05
    would make policy 06 permanently dead: 05 would win every time and the
    override could never fire. Policy 05 therefore carries an `unless` clause
    that mirrors 06's permit condition exactly.

    These tests exist to keep those two clauses in sync. If someone edits
    either policy's condition without editing the other, the first test here
    goes red — which is the whole point, because the failure mode otherwise
    is silent: the override simply stops working and nothing complains.
    """

    @pytest.fixture
    def combined_policy(self):
        return (
            load_policy('05_forbid_cross_tenant_lookups.cedar')
            + '\n\n'
            + load_policy('06_admin_tenant_override_with_audit.cedar')
        )

    @pytest.fixture
    def policy_06_only(self):
        return load_policy('06_admin_tenant_override_with_audit.cedar')

    def test_override_fires_in_isolation(self, policy_06_only):
        """Baseline: policy 06 alone permits the ticket-verified cross-tenant call.

        ticket_tenant is written by the REQUEST interceptor after it checked
        the ticket with the ticketing system (interceptor/), never by the model.
        """
        result = evaluate(
            policy_text=policy_06_only,
            principal='AgentCore::OAuthUser::"user-globex-admin"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co',
                               'ticket_tenant': 'logistics-co'}},
            entities=_admin_entities('user-globex-admin', 'globex-corp',
                                     'tenant_admin'),
        )
        assert result.decision == cedarpy.Decision.Allow

    def test_override_survives_policy_05(self, combined_policy):
        """The regression test that matters: 05's `unless` lets 06 through.

        Before policy 05 grew its `unless` clause, this returned Deny and the
        admin override was dead code the moment both policies were deployed.
        """
        result = evaluate(
            policy_text=combined_policy,
            principal='AgentCore::OAuthUser::"user-globex-admin"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co',
                               'ticket_tenant': 'logistics-co'}},
            entities=_admin_entities('user-globex-admin', 'globex-corp',
                                     'tenant_admin'),
        )
        assert result.decision == cedarpy.Decision.Allow

    def test_override_does_not_extend_to_writes(self, combined_policy):
        """Policy 05's carve-out covers the three read tools only."""
        result = evaluate(
            policy_text=combined_policy,
            principal='AgentCore::OAuthUser::"user-globex-admin"',
            action='AgentCore::Action::"billing___process_refund"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co',
                               'ticket_tenant': 'logistics-co'}},
            entities=_admin_entities('user-globex-admin', 'globex-corp',
                                     'tenant_admin'),
        )
        assert result.decision == cedarpy.Decision.Deny

    def test_admin_without_verified_ticket_is_denied(self, combined_policy):
        """No verified ticket, no crossing."""
        result = evaluate(
            policy_text=combined_policy,
            principal='AgentCore::OAuthUser::"user-globex-admin"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co'}},
            entities=_admin_entities('user-globex-admin', 'globex-corp',
                                     'tenant_admin'),
        )
        assert result.decision == cedarpy.Decision.Deny

    def test_ticket_for_a_different_tenant_is_denied(self, combined_policy):
        """A real ticket only opens the tenant it concerns."""
        result = evaluate(
            policy_text=combined_policy,
            principal='AgentCore::OAuthUser::"user-globex-admin"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co',
                               'ticket_tenant': 'umbrella-inc'}},
            entities=_admin_entities('user-globex-admin', 'globex-corp',
                                     'tenant_admin'),
        )
        assert result.decision == cedarpy.Decision.Deny

    def test_non_admin_cannot_use_the_override(self, combined_policy):
        """A support agent with a verified ticket still cannot cross
        tenants. The override is scoped to the role claim."""
        result = evaluate(
            policy_text=combined_policy,
            principal='AgentCore::OAuthUser::"user-globex-001"',
            action='AgentCore::Action::"billing___get_invoice_details"',
            resource=GATEWAY_ARN,
            context={'input': {'tenant_id': 'logistics-co',
                               'ticket_tenant': 'logistics-co'}},
            entities=_admin_entities('user-globex-001', 'globex-corp',
                                     'support_agent'),
        )
        assert result.decision == cedarpy.Decision.Deny
