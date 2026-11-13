"""
trusted_context_interceptor.py: Gateway REQUEST interceptor that writes the
values Cedar is allowed to trust.

Accompanies Chapter 3, Section 3.5 ("Who wrote this value?") of
*AgentCore in Production: The Operator's Playbook*, 2nd edition.

The rule from Section 3.5: a policy may CONSTRAIN anything the model wrote,
but it may only TRUST values that something outside the reasoning loop
wrote. AgentCore Gateway runs a REQUEST interceptor before it builds the
Cedar request, and Policy evaluates the transformed request. That ordering is
what makes this file work:

  1. Reject any call in which the model supplied a trusted field itself.
  2. Look up facts the model can only point at (an approval ID, a ticket ID)
     in the systems that own them.
  3. Write the verified facts into the tool arguments, where Cedar reads them
     as context.input.<field>.

Trusted fields written here:
  approval_verified  (policy 02)  approval service says a human approved
                                  this exact action and resource, once
  ticket_tenant      (policies 05/06)  ticketing system says this caller's
                                  open ticket concerns that tenant
  auth_age_seconds   (policy 14)  now - auth_time (or iat) from the JWT

Deployment notes (live-tested 2026-11-11; verify in your account):
  - Configure the interceptor with interceptionPoints=["REQUEST"] and
    inputConfiguration.passRequestHeaders=True, or the JWT never arrives.
  - The Gateway's JWT authorizer has already validated the token signature
    before the interceptor runs; this code only decodes the payload.
  - Policy validation checks conditions against the Cedar schema generated
    from each tool's input schema, so every trusted field must also be
    declared in the tool's input schema. Describe it there as "set by the
    gateway; leave empty". The model will see the field. That is why step 1
    exists.

Run against a live Gateway on 2026-11-11: the interceptor ran before Policy, Policy
evaluated the transformed request (a valid approval was allowed, an invalid or missing
one denied), and a forged approval_verified was rejected with -32602. The unit tests in
tests/test_trusted_context_interceptor.py exercise the logic offline.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Callable, Optional

TRUSTED_FIELDS = ("approval_verified", "ticket_tenant", "auth_age_seconds")

# Tools whose policies read each trusted field.
APPROVAL_TOOLS = {
    "devops-prod___terminate_instance": "instance_id",
    "devops-prod___delete_resource": "resource_id",
    "data___drop_table": "table_name",
}
TICKET_TOOLS = {
    "billing___get_invoice_details",
    "billing___list_invoices",
    "support___get_ticket",
}
FRESHNESS_TOOLS = {
    "billing___process_refund",
    "data___export_user_data",
}

# Pluggable lookups. Replace with clients for your approval service and
# ticketing system. Each must fail closed: return False/None when unsure.
# verify_and_consume_approval(approval_id, tool, resource_id, requester_sub) -> bool
ApprovalVerifier = Callable[[str, str, str, str], bool]
# lookup_ticket_tenant(ticket_id, caller_sub) -> tenant or None
TicketLookup = Callable[[str, str], Optional[str]]


def _deny_everything_approval(*_args) -> bool:
    return False


def _no_ticket(*_args) -> Optional[str]:
    return None


def decode_jwt_claims(authorization_header: str) -> dict:
    """Decode the JWT payload. Signature was validated by the Gateway."""
    token = authorization_header.removeprefix("Bearer ").strip()
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def _error(request_id, message: str) -> dict:
    """Short-circuit: the Gateway returns this without calling the target."""
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": message},
                },
            }
        },
    }


def _passthrough(gateway_request: dict, body: dict) -> dict:
    transformed = {"body": body}
    if "headers" in gateway_request:
        transformed["headers"] = gateway_request["headers"]
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": transformed},
    }


def stamp_trusted_context(
    event: dict,
    verify_approval: ApprovalVerifier = _deny_everything_approval,
    lookup_ticket_tenant: TicketLookup = _no_ticket,
    now: Optional[float] = None,
) -> dict:
    gateway_request = event.get("mcp", {}).get("gatewayRequest", {})
    body = gateway_request.get("body", {})
    if body.get("method") != "tools/call":
        return _passthrough(gateway_request, body)

    params = body.setdefault("params", {})
    tool = params.get("name", "")
    args = params.setdefault("arguments", {})

    forged = [field for field in TRUSTED_FIELDS if field in args]
    if forged:
        # The model filled a field only the gateway may fill. Refuse rather
        # than silently overwrite, so the attempt shows up in the logs.
        return _error(body.get("id"), f"Rejected: caller supplied gateway-only field(s) {forged}")

    headers = gateway_request.get("headers", {})
    claims = decode_jwt_claims(headers.get("Authorization", headers.get("authorization", "")))
    caller_sub = str(claims.get("sub", ""))
    current = time.time() if now is None else now

    if tool in FRESHNESS_TOOLS:
        authenticated_at = claims.get("auth_time", claims.get("iat"))
        if authenticated_at is not None:
            args["auth_age_seconds"] = int(current - float(authenticated_at))

    if tool in APPROVAL_TOOLS:
        resource_key = APPROVAL_TOOLS[tool]
        approval_id = str(args.get("approval_id", ""))
        resource_id = str(args.get(resource_key, ""))
        approved = bool(approval_id and resource_id and caller_sub) and verify_approval(
            approval_id, tool, resource_id, caller_sub
        )
        args["approval_verified"] = bool(approved)

    if tool in TICKET_TOOLS and "ticket_id" in args:
        tenant = lookup_ticket_tenant(str(args["ticket_id"]), caller_sub)
        if tenant:
            args["ticket_tenant"] = tenant

    return _passthrough(gateway_request, body)


def lambda_handler(event, context):
    # Wire real clients here, e.g. an approvals table with a conditional
    # update that marks the approval consumed, and your ticketing API.
    return stamp_trusted_context(event)
