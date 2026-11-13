"""Offline tests for interceptor/trusted_context_interceptor.py (no AWS)."""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "interceptor"))

from trusted_context_interceptor import decode_jwt_claims, stamp_trusted_context  # noqa: E402


def jwt(claims: dict) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"Bearer {part({'alg': 'none'})}.{part(claims)}.sig"


def event(tool: str, arguments: dict, claims: dict | None = None) -> dict:
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {"gatewayRequest": {
            "headers": {"Authorization": jwt(claims or {"sub": "user-1"})},
            "body": {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": tool, "arguments": arguments}},
        }},
    }


def args_of(result: dict) -> dict:
    return result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]


def test_decode_claims():
    assert decode_jwt_claims(jwt({"sub": "abc"}))["sub"] == "abc"
    assert decode_jwt_claims("Bearer not-a-jwt") == {}


def test_forged_trusted_field_is_rejected_before_cedar():
    result = stamp_trusted_context(
        event("devops-prod___terminate_instance",
              {"instance_id": "i-1", "approval_verified": True}))
    response = result["mcp"]["transformedGatewayResponse"]
    assert "error" in response["body"]
    assert "approval_verified" in response["body"]["error"]["message"]


def test_approval_verified_only_when_service_says_so():
    calls = []

    def verifier(approval_id, tool, resource_id, sub):
        calls.append((approval_id, tool, resource_id, sub))
        return approval_id == "apr-42"

    good = stamp_trusted_context(
        event("devops-prod___terminate_instance", {"instance_id": "i-1", "approval_id": "apr-42"}),
        verify_approval=verifier)
    bad = stamp_trusted_context(
        event("devops-prod___terminate_instance", {"instance_id": "i-1", "approval_id": "apr-99"}),
        verify_approval=verifier)
    assert args_of(good)["approval_verified"] is True
    assert args_of(bad)["approval_verified"] is False
    assert calls[0] == ("apr-42", "devops-prod___terminate_instance", "i-1", "user-1")


def test_default_verifier_fails_closed():
    result = stamp_trusted_context(
        event("data___drop_table", {"table_name": "orders", "approval_id": "apr-1"}))
    assert args_of(result)["approval_verified"] is False


def test_ticket_tenant_comes_from_ticketing_system():
    result = stamp_trusted_context(
        event("billing___get_invoice_details",
              {"tenant_id": "logistics-co", "ticket_id": "ticket-4471"}),
        lookup_ticket_tenant=lambda ticket, sub: "logistics-co" if ticket == "ticket-4471" else None)
    assert args_of(result)["ticket_tenant"] == "logistics-co"


def test_unknown_ticket_writes_nothing():
    result = stamp_trusted_context(
        event("billing___get_invoice_details", {"tenant_id": "x", "ticket_id": "ticket-1"}))
    assert "ticket_tenant" not in args_of(result)


def test_auth_age_from_auth_time():
    result = stamp_trusted_context(
        event("billing___process_refund", {"amount": 10},
              claims={"sub": "u", "auth_time": 1_000, "iat": 5_000}),
        now=4_600)
    assert args_of(result)["auth_age_seconds"] == 3_600


def test_non_tool_calls_pass_through():
    ev = event("x", {})
    ev["mcp"]["gatewayRequest"]["body"] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    result = stamp_trusted_context(ev)
    assert result["mcp"]["transformedGatewayRequest"]["body"]["method"] == "tools/list"
