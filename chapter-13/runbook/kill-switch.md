# Kill switch: stopping one agent in under a minute

Accompanies Chapter 13, Section 13.5. For agents that call other agents, use
the layered table in Chapter 12, Section 12.10, and its
companion `containment/` planner.

**Who may pull it:** whoever is on call for the agent, without asking anyone.
**Who may restart it:** the owner plus one other person, after the cause is
understood. Write both names here: ______________________

None of these commands have been run against AWS by the author. Rehearse them
in staging, with a timer, before you need them. Placeholders use the book's
fictional account `111122223333`.

## 0. Prepare, before any incident

Keep a stateless `forbid` for this agent in the repository, ready to deploy.
Do not make the kill switch a temporal (Dogwood) policy: creating, updating,
or deleting any temporal policy resets every live policy session on the engine.

```cedar
// kill-switch-acme-support-agent.cedar
forbid (
  principal,
  action,
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/example-gw"
)
when {
  principal.hasTag("agent_name") &&
  principal.getTag("agent_name") == "acme-support-agent"
};
```

Measure, once, how long a new policy takes to start denying in your account.
No current AWS document states a propagation time. Record it here: ____ s.

## 1. Stop new work at the entry point (seconds)

Flip the application flag that stops new sessions for this agent. It is your
code; it is only as good as the last test.

## 2. Stop every tool call at the Gateway (seconds to minutes)

```bash
# kill-switch-acme-support-agent.json:
#   {"cedar": {"statement": "<the forbid statement above, as one string>"}}
aws bedrock-agentcore-control create-policy \
  --policy-engine-id <engine-id> \
  --name killswitch_acme_support_agent \
  --enforcement-mode ACTIVE \
  --definition file://kill-switch-acme-support-agent.json
```

Policy names allow letters, digits, and underscores only. Strict validation
(`FAIL_ON_ANY_FINDINGS`, the default) may reject a lone `forbid` as overly
restrictive; if it does, decide in advance whether the kill switch is
deployed with `--validation-mode IGNORE_ALL_FINDINGS`, and write the decision
here. Verify in the Policy decision logs that calls are now denied.

## 3. Stop sessions already running

```bash
aws bedrock-agentcore stop-runtime-session \
  --agent-runtime-arn arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/acme_support_agent-a1b2c3d4e5 \
  --runtime-session-id <session-id> \
  --qualifier production
```

Get the active session ids from your application or from the traces (the
runtime session id is on every span).

## 4. Stop spend (if the agent pays)

```bash
aws bedrock-agentcore delete-payment-session \
  --payment-manager-arn <payment-manager-arn> \
  --payment-session-id <payment-session-id>
```

If the cause may be a compromised wallet grant, the user can also revoke the
agent in the wallet provider's hub.

## Broader switches (when the policy layer itself is suspect)

- An IAM deny on `bedrock-agentcore:InvokeAgentRuntime` for the runtime ARN,
  attached to every caller role. Broad, and subject to IAM propagation.
- Delete the runtime endpoint, then the workload identity and the OAuth
  credential providers only this agent uses. This is decommissioning
  (Chapter 11, Section 11.8), not an incident step; it is slow to undo.

## After

- Record the time from page to "calls denied". If it was over a minute,
  the runbook changes, not the person.
- Write the golden-set case before the fix.
