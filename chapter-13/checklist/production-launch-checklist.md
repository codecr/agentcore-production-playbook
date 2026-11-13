# Production Launch Checklist for AgentCore Agents

Accompanies Chapter 13 of *AgentCore in Production: The Operator's Playbook*.

This is the checklist I would hand a team on day one. It is organized by
**phase**, not by service, because the order in which you do these things
matters more than the list itself. Several items are cheap now and expensive
in six months; those are marked **[hard to reverse]**.

Copy this into your repository. Delete what does not apply. Add what your
domain requires. A checklist nobody edits is a checklist nobody uses.

---

## Phase 0 — Before you write agent code

- [ ] **The use case survives the "why an agent" question.** Write one
      paragraph explaining why this needs an agent rather than a workflow, a
      form, or a search box. If the paragraph is hard to write, the answer is
      probably a workflow. See Chapter 13 Section 13.13.
- [ ] **The failure mode of a wrong answer is understood and written down.**
      Wrong answers are not hypothetical. Who is harmed, how badly, and how
      would anyone find out?
- [ ] **Someone owns this agent by name.** Not a team alias, a person, plus a
      rotation. Chapter 11 exists because of agents that outlived their owners.
- [ ] **The identity model is decided.** **[hard to reverse]** Who is the
      principal — the end user, a service, a tenant? What claims does the token
      carry? Every authorization decision in the system depends on this and
      changing it later means rewriting every policy.
- [ ] **The tenant isolation model is decided.** **[hard to reverse]** Whether
      tenancy is a token claim, a namespace, a separate Knowledge Base, or all
      three. Retrofitting isolation onto a system that assumed a single tenant
      is a rewrite.

## Phase 1 — Before the first staging deploy

**Runtime and Gateway**

- [ ] Every tool call goes through the Gateway. No agent code calls a tool
      target directly. **[hard to reverse]** This is the interception point
      for Policy; an agent with a side channel has no perimeter.
- [ ] The model id is configuration, not code. **[hard to reverse]**
- [ ] The container builds for ARM64.
- [ ] Session identifiers are deterministic from stable identity and at least
      33 characters.
- [ ] Runtime execution role is scoped to what this agent actually calls.

**Policy**

- [ ] Cedar policies exist for tenant isolation, environment boundaries, and
      any destructive or expensive tool.
- [ ] Policies are in version control, not authored in the console.
- [ ] Policies are deployed in `LOG_ONLY` first and the decision logs are read
      by a human before `ENFORCE`.
- [ ] There is at least one explicit `forbid` for the failure you would find
      unacceptable, independent of any `permit`.

**Guardrails**

- [ ] Content filters configured per use case, not blanket MEDIUM.
- [ ] PII detection on **output** as well as input, with BLOCK (not ANONYMIZE)
      for the identifiers you cannot afford to leak.
- [ ] Dynamic input tagging on every user-derived field entering a prompt.
- [ ] Denied topics defined for anything out of scope.
- [ ] Guardrail version is pinned; the agent does not point at DRAFT.

**Structured outputs**

- [ ] Every output another system consumes has a schema, versioned in a
      registry, with `additionalProperties: false`.
- [ ] Tool definitions use `strict: true`.
- [ ] Schema tests run in CI with no AWS credentials required.
- [ ] There is no silent fallback path. If a fallback exists, it emits a
      metric and marks its own output as degraded.

**State**

- [ ] The three kinds of state are separated: conversation state in the
      framework, working state in Session Storage, learned state in Memory.
- [ ] Memory namespaces are scoped by tenant and user. **[hard to reverse]**
- [ ] Knowledge Base corpus has been audited for file size against the
      chunking strategy's limits.
- [ ] CloudWatch alarms exist on ingestion job failures and per-document
      failures.

**Observability**

- [ ] OpenTelemetry instrumentation is emitting traces. **[hard to reverse
      in the sense that you cannot debug the past]**
- [ ] A business-level correlation id is propagated through payloads across
      every agent boundary, and stamped on every root span.
- [ ] Agent name, tenant id, and hop count are span attributes.
- [ ] Span queries cover the per-agent log groups (agents created from
      2026-07-20) and `aws/spans` (older agents). See Chapter 11.

**Containment**

- [ ] A kill switch exists and one on-call person may pull it without
      asking: entry flag, a ready stateless Cedar `forbid`, `StopRuntimeSession`,
      payment sessions. See `runbook/kill-switch.md`. Pulled once in staging,
      with the time to "calls denied" recorded.

## Phase 2 — Before the first production deploy

- [ ] **Golden set exists** — twenty to fifty cases with agreed expected
      behavior, including negative cases and at least one case per past
      incident.
- [ ] **Quality gate runs in CI** against a baseline, with per-metric floors
      and tolerances, category-level checks, blocking on regression. The
      **baseline moves only with an explicit flag**, and the flag puts a name
      in the deploy log; a baseline that updates itself after every green
      build ratchets downward.
- [ ] **Online evaluation is configured** with the three starter evaluators,
      Helpfulness (response), ToolSelectionAccuracy (trajectory), and
      GoalSuccessRate (outcome), and a sampling rate derived from traffic.
- [ ] **Alarms exist for**: sustained low quality, anomalous quality drops,
      and — most importantly — evaluation results no longer arriving.
- [ ] **A rollback path is written down and rehearsed.** Not "we would roll
      back", but the specific command (`update-agent-runtime-endpoint
      --agent-runtime-version N` on the named endpoint clients invoke), the
      specific version, and who runs it. Guardrail versions, Cedar policies,
      and Gateway tool descriptions have their own rollback paths.
- [ ] **Quotas checked against peak traffic**: Bedrock model RPM/TPM in the
      Region (or a cross-Region inference profile), Runtime session creation
      and active-session limits, Memory extraction, Gateway rate limits.
      Batch runners bound concurrency; retries happen at one layer only.
- [ ] **Retention and deletion path written for every store**: long-term
      memory (namespace by user; record expiry), short-term events, Session
      Storage, Knowledge Base sources, log groups (retention period, what
      goes into spans), evaluation outputs, payment instruments.
- [ ] **Cost per session projected**: model tokens, Runtime vCPU and GB
      seconds, Gateway and Policy calls, Memory, evaluation judge tokens, and
      payment provider fees. Resources tagged by agent for cost allocation.
      Show it to whoever owns that budget before launch. Price tables:
      Appendix C.
- [ ] **The agent is registered in the catalog** with owner, contact, tool
      schema, and a description written in the words a consumer would search.
- [ ] **An incident runbook exists** for this agent specifically. See
      `runbook/agent-incident-runbook.md`.
- [ ] **Offboarding covers agents.** When a person leaves, their agents and
      registry records get a new owner.

## Phase 3 — First week in production

- [ ] Read fifty real transcripts by hand. Not sampled by score — sampled at
      random. This is the highest-value hour of the launch.
- [ ] Read the lowest-scoring twenty. Confirm the judge is measuring what you
      think it is.
- [ ] Check the Policy decision logs for denials. Every denial is either a
      correct block or a policy that is too tight; both need a decision.
- [ ] Verify cost against projection. Investigate any variance over 25%.
- [ ] Confirm traces reconstruct end to end for a request that crossed an
      agent boundary.

## Phase 4 — First quarter

- [ ] Baselines re-measured and alarm thresholds set from real variance rather
      than round numbers.
- [ ] Golden set extended with every incident since launch.
- [ ] Registry reconciliation run: deployed agents versus catalogued agents.
- [ ] Memory audited for stale facts.
- [ ] Knowledge Base re-synced and ingestion failures reviewed.
- [ ] Policy set reviewed for rules that never fired (dead) and rules that
      fire constantly (probably wrong).
- [ ] Cost per successful session calculated and compared to the value of a
      successful session.

---

## If the agent handles money

Everything above, plus:

- [ ] End-user wallet authorization is explicit and revocable.
- [ ] Session spending limits are set to the smallest number that works.
- [ ] Paid calls go through Gateway tools, so Cedar and Dogwood can see them.
- [ ] A Dogwood daily fence (`payments/daily-spend.dogwood`) with the policy
      session id derived by the backend from agent, tenant, and UTC date.
      Remember it resets whenever a temporal policy is deployed.
- [ ] **Aggregate daily and monthly budgets exist outside the session**, with
      atomic counters. Session limits alone do not bound total spend.
- [ ] Single-transaction cap is at most a tenth of the daily cap.
- [ ] Payee allowlist enforced at the Gateway, not only in application code.
- [ ] Anything with a duration (subscriptions, licenses, recurring access)
      requires a procurement role on the principal, set by the identity
      provider. Never a token the model can type (`payment-policies.cedar` P05).
- [ ] A purchase ledger is consulted before buying anything durable, so the
      agent does not re-buy what it already has.
- [ ] Payment receipts flow into the same telemetry as everything else, with
      the correlation id attached.
- [ ] Denials are logged with enough context to distinguish a budget stop from
      a signing failure from a merchant verification failure.
- [ ] Coinbase wallets: the AWS Marketplace subscription is in place.
      Provider per-operation fees are modeled per ticket size.
- [ ] Nightly reconciliation against the wallet provider's transaction export.
- [ ] Finance sees agent spend in the same review cadence as any other vendor
      spend, and knows who to call.

---

## Sign-off

| Item | Owner | Date |
|---|---|---|
| Phase 0 complete | | |
| Phase 1 complete | | |
| Phase 2 complete — approved for production | | |
| Phase 3 review complete | | |
| Phase 4 review complete | | |

Re-run Phase 2 for every material change: a new model, a new tool, a new
Knowledge Base, a new tenant class, or a new payment capability.
