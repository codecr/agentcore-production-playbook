"""
kill_switch.py — Plan (and optionally apply) the containment of one
delegated agent, narrowest switch first.

Accompanies Chapter 12 (Section 12.10). DRY-RUN BY DEFAULT: it prints the
plan and the API calls it would make. Nothing is applied without --apply.

The switches, from narrowest to broadest:

  1. forbid-delegation   stateless Cedar forbid on the callee's A2A action at
                         the CALLER's Gateway (new delegations stop)
  2. forbid-action       stateless Cedar forbid on the callee's consequential
                         tool at the CALLEE's Gateway (the action stops)
  3. remove-target       delete the passthrough target from the Gateway
  4. restrict-runtime    tighten the runtime's inbound authorizer
                         (allowedWorkloadConfiguration) — manual, printed only
  5. deprecate-record    Agent Registry: stops discovery, nothing else

Design rules from the chapter:
  * kill switches are STATELESS policies: changing a temporal (Dogwood)
    policy invalidates every live policy session on that engine (-32005);
  * a denial from a switched-off agent ends the delegation and hands off to
    a human: the plan includes that policy alongside the switch;
  * every switch records who pulled it and why.

Status: the forbid it generates was created, enforced, and removed against a live A2A
passthrough Gateway on 2026-11-11, and it left the policy session valid. `--apply`
itself was not run; parameter names follow the CreatePolicy shape used in chapter-03.

Usage:
    python containment/kill_switch.py --callee refunds-agent --reason "INC-2291 unapproved credits" --pulled-by oncall-platform
"""

import argparse
import getpass
import json
from datetime import datetime, timezone

GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw"

FORBID_TEMPLATE = """// KILL SWITCH {switch} — pulled by {pulled_by} at {at}
// reason: {reason}
forbid (
  principal,
  action == AgentCore::Action::"{action}",
  resource == AgentCore::Gateway::"{gateway}"
);"""

ALLOWED_PULLERS = {"owning-squad", "oncall-platform", "security"}


def plan(callee: str, consequential_tool: str, reason: str, pulled_by: str) -> list[dict]:
    if pulled_by not in ALLOWED_PULLERS:
        raise SystemExit(f"{pulled_by!r} may not pull switches; allowed: {sorted(ALLOWED_PULLERS)}")
    if len(reason) < 20:
        raise SystemExit("reason must say why, for the person who reverts this")
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # A2A passthrough action id: <target>___POST:/ (live-tested 2026-11-11, CT-12-03). The
    # target name takes hyphens, so pass --callee refunds-agent, not refunds_agent.
    delegation_action = f"{callee}___POST:/"
    steps = [
        {
            "switch": "forbid-delegation",
            "where": "caller gateway policy engine",
            "api": "bedrock-agentcore-control:CreatePolicy",
            "policy_name": f"killswitch_{callee}_delegation",
            "statement": FORBID_TEMPLATE.format(
                switch="forbid-delegation", pulled_by=pulled_by, at=at, reason=reason,
                action=delegation_action, gateway=GATEWAY_ARN),
            "interrupts_live_sessions": False,
        },
        {
            "switch": "forbid-action",
            "where": "callee gateway policy engine",
            "api": "bedrock-agentcore-control:CreatePolicy",
            "policy_name": f"killswitch_{callee}_{consequential_tool.split('___')[-1]}",
            "statement": FORBID_TEMPLATE.format(
                switch="forbid-action", pulled_by=pulled_by, at=at, reason=reason,
                action=consequential_tool, gateway=GATEWAY_ARN),
            "interrupts_live_sessions": False,
        },
        {
            "switch": "caller-behavior",
            "where": "orchestrator configuration",
            "note": "A denial from a switched-off agent ends the delegation and "
                    "escalates to a human. Do not let the orchestrator re-plan "
                    "around it (see the chapter, Section 12.10).",
        },
        {
            "switch": "deprecate-record (optional, discovery only)",
            "where": "agent-registry",
            "api": "agent-registry-control: status transition to DEPRECATED with statusReason",
            "note": "Stops discovery. Does not stop any caller already bound to the endpoint.",
        },
        {
            "switch": "remove-target / restrict-runtime (escalation only)",
            "where": "gateway / runtime authorizer",
            "note": "Use when the policy layer is suspect or a side path exists. "
                    "Sessions already running in the callee are not stopped.",
        },
    ]
    return steps


def apply(steps: list[dict], caller_engine_id: str, callee_engine_id: str, region: str) -> None:
    import boto3
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    engines = {"caller gateway policy engine": caller_engine_id,
               "callee gateway policy engine": callee_engine_id}
    for step in steps:
        if step.get("api") != "bedrock-agentcore-control:CreatePolicy":
            continue
        response = control.create_policy(
            policyEngineId=engines[step["where"]],
            name=step["policy_name"],
            validationMode="FAIL_ON_ANY_FINDINGS",
            definition={"cedar": {"statement": step["statement"]}},
        )
        print(f"[applied] {step['switch']} -> policy {response.get('policyId')}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--callee", required=True, help="passthrough target name, e.g. refunds-agent")
    parser.add_argument("--tool", default="refunds___issue_credit")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--pulled-by", required=True, choices=sorted(ALLOWED_PULLERS))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--caller-engine-id")
    parser.add_argument("--callee-engine-id")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    steps = plan(args.callee, args.tool, args.reason, args.pulled_by)
    print(json.dumps({"operator": getpass.getuser(), "steps": steps}, indent=2))
    if args.apply:
        if not (args.caller_engine_id and args.callee_engine_id):
            raise SystemExit("--apply needs --caller-engine-id and --callee-engine-id")
        apply(steps, args.caller_engine_id, args.callee_engine_id, args.region)
    else:
        print("\n[dry-run] nothing applied. Re-run with --apply and engine ids.")


if __name__ == "__main__":
    main()
