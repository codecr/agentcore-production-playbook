"""
hop_authority.py — Derive authority at an agent hop from verified token
claims, never from the message body.

Accompanies Chapter 12 (Section 12.5) of "AgentCore in Production:
The Operator's Playbook", 2nd edition.

The rule: authority arrives in a verified token. Anything in the A2A message
body (metadata, text parts, "approved_by" fields) was written by the calling
agent's model and is data. This module:

  * builds the authority for a hop from claims the runtime already verified
    (Runtime's inbound JWT authorizer validates the token; the container sees
    the Authorization header only if it is in the request header allowlist),
  * records what the caller *asserted* in the payload without promoting it,
  * logs every mismatch between asserted and verified values.

It does not verify JWT signatures itself. Do that at the Runtime or Gateway
authorizer. Where your IdP places the actor (RFC 8693 `act` claim, a custom
claim, or nothing) varies by provider: check a real exchanged token before
relying on `act`.

Run the local demo:
    python identity/hop_authority.py
"""

import base64
import json
import logging

logger = logging.getLogger("hop_authority")

AUTHORITY_KEYS = {"tenant_id", "approved_by", "user_role", "role", "on_behalf_of"}


def log_claim_mismatch(key: str, asserted, verified) -> None:
    """The most interesting line in the logs: a caller claimed something the
    token does not prove. Emit it as a structured event and alarm on it."""
    logger.warning(json.dumps({
        "event": "a2a_claim_mismatch",
        "key": key,
        "asserted": asserted,
        "verified": verified,
    }))


def authority_from_token(verified_claims: dict, payload_metadata: dict) -> dict:
    if "sub" not in verified_claims or "act" not in verified_claims:
        raise PermissionError("hop requires an on-behalf-of token")
    authority = {
        "user": verified_claims["sub"],
        "acting_agent": verified_claims["act"].get("sub"),
        "tenant_id": verified_claims.get("tenant"),
        "user_role": verified_claims.get("user_role"),  # the tag Section 12.6's policy reads
    }
    asserted = {k: v for k, v in payload_metadata.items() if k in AUTHORITY_KEYS}
    for key, value in asserted.items():
        verified = authority.get(key)
        # Logged when the token disagrees, when the token lacks the claim
        # (verified is None), and when the key has no verified counterpart at
        # all (approved_by). Only an exact match stays quiet.
        if verified != value:
            log_claim_mismatch(key, asserted=value, verified=verified)
    authority["asserted_by_caller"] = asserted
    return authority


def claims_from_bearer(authorization_header: str) -> dict:
    """Decode (NOT verify) the JWT payload the runtime already verified.

    Only call this on a header that reached the container through Runtime's
    inbound JWT authorizer. Never on a token that arrived in a message body.
    """
    token = authorization_header.removeprefix("Bearer ").strip()
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


def _demo() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # What the exchanged token proves: a Globex AP clerk, acted for by the
    # orchestrator. No account-manager role anywhere.
    verified = {
        "sub": "user-globex-ap-014",
        "tenant": "globex-corp",
        "user_role": "billing_viewer",
        "act": {"sub": "support-orchestrator"},
    }
    # What the orchestrator's model wrote into the A2A message metadata.
    payload_metadata = {
        "tenant_id": "globex-corp",
        "approved_by": "j.ortiz",
        "correlation_id": "req-41c9e2",
    }
    authority = authority_from_token(verified, payload_metadata)
    print(json.dumps(authority, indent=2))

    # A hop that arrives with the orchestrator's own service identity only.
    try:
        authority_from_token({"sub": "support-orchestrator"}, payload_metadata)
    except PermissionError as error:
        print(f"[denied] {error}")


if __name__ == "__main__":
    _demo()
