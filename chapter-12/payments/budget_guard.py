"""
budget_guard.py — Aggregate spend control across sessions.

Accompanies Chapter 12 of "AgentCore in Production: The Operator's Playbook".

WHY THIS EXISTS

AgentCore Payments enforces spending limits **per session**, deterministically,
at the infrastructure layer. That control is real and it works. It is also
scoped to exactly one session, and a batch workload that opens two thousand
sessions a month has two thousand independent budgets that sum to a number
nobody chose.

That is the incident in Chapter 12 Section 12.1: every session stayed inside
its limit, every payment was authorized, and the monthly total was four
thousand dollars for data the organization had already bought.

This module is the layer AgentCore does not provide: a budget that spans
sessions, with three enforcement tiers and a circuit breaker.

    Tier 1  session   — enforced by AgentCore Payments (not this module)
    Tier 2  daily     — enforced here, before the agent is allowed to spend
    Tier 3  monthly   — enforced here, with a hard stop
    Tier 4  anomaly   — circuit breaker on unusual spend shape

STORAGE
    Counters live in DynamoDB with atomic updates, because two agent sessions
    running concurrently must not both read "spent so far" and both decide
    they have room. Conditional writes are the whole mechanism; anything
    read-then-write races under load and races are how budgets leak.

Requires:
    pip install boto3
    A DynamoDB table with partition key `budget_key` (string).
"""

import logging
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("budget_guard")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BUDGET_TABLE = os.environ.get("BUDGET_TABLE", "agent-budgets")

# Tier 4 (anomaly / circuit breaker) tuning. Deliberately simple: a single
# transaction more than ANOMALY_MULTIPLIER times the trailing average trips
# it. ANOMALY_MIN_SAMPLE_TX guards against judging "unusual" against a thin
# history — a brand-new agent's third transaction should not be flagged
# just because there is no baseline yet.
ANOMALY_MULTIPLIER = 3
ANOMALY_WINDOW_DAYS = 7
ANOMALY_MIN_SAMPLE_TX = 5


# =========================================================================
# Decisions
# =========================================================================

class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY_DAILY_CAP = "DENY_DAILY_CAP"
    DENY_MONTHLY_CAP = "DENY_MONTHLY_CAP"
    DENY_PAYEE_NOT_ALLOWED = "DENY_PAYEE_NOT_ALLOWED"
    DENY_SINGLE_TX_CAP = "DENY_SINGLE_TX_CAP"
    DENY_CIRCUIT_OPEN = "DENY_CIRCUIT_OPEN"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"


@dataclass
class SpendRequest:
    """A proposed payment, before it happens."""
    agent_name: str
    session_id: str
    tenant_id: str
    payee: str                  # domain or endpoint identifier
    amount_usd: Decimal
    purpose: str                # what the agent believes it is buying
    correlation_id: str = ""    # from Chapter 11's cross-agent tracing


@dataclass
class SpendVerdict:
    decision: Decision
    reason: str
    daily_spent: Decimal
    monthly_spent: Decimal
    remaining_daily: Decimal
    remaining_monthly: Decimal

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


# =========================================================================
# Budget policy
# =========================================================================

@dataclass(frozen=True)
class BudgetPolicy:
    """Limits for one agent. Set these from a finance-approved number, not
    from what feels safe to an engineer at 17:00 on a Thursday.

    The relationship between the tiers is the important part:

        single_tx_cap  ≤ daily_cap / 10   — no single payment should be able
                                            to consume a meaningful share of
                                            the day. A payment that large is
                                            a decision, not a transaction.
        daily_cap × 31 ≥ monthly_cap      — otherwise the monthly cap never
                                            binds and is decoration.
    """
    agent_name: str
    single_tx_cap_usd: Decimal
    daily_cap_usd: Decimal
    monthly_cap_usd: Decimal
    allowed_payees: frozenset[str]
    escalate_above_usd: Optional[Decimal] = None   # human approval threshold

    def validate(self) -> list[str]:
        problems = []
        if self.single_tx_cap_usd > self.daily_cap_usd / 10:
            problems.append(
                "single_tx_cap should be at most a tenth of daily_cap; a payment "
                "larger than that is a decision that deserves a human"
            )
        if self.daily_cap_usd * 31 < self.monthly_cap_usd:
            problems.append(
                "monthly_cap can never be reached given daily_cap — it is decoration"
            )
        if not self.allowed_payees:
            problems.append(
                "allowed_payees is empty; an agent that may pay anyone is an "
                "agent whose spend you cannot predict"
            )
        return problems


# The Acme Lending credit-analysis agent from Section 12.1, with the controls
# it should have had.
ACME_CREDIT_AGENT_BUDGET = BudgetPolicy(
    agent_name="acme-lending-credit-analysis",
    single_tx_cap_usd=Decimal("2.00"),
    daily_cap_usd=Decimal("40.00"),
    monthly_cap_usd=Decimal("900.00"),
    allowed_payees=frozenset({
        "bureau.example.com",
        "marketdata.example.com",
        "news.example.com",
    }),
    escalate_above_usd=Decimal("1.00"),
)


# =========================================================================
# The guard
# =========================================================================

class BudgetGuard:
    """Aggregate spend enforcement across sessions.

    Call `authorize` BEFORE the agent attempts a paid call, and `record`
    AFTER the payment settles. The two-phase shape matters: authorizing and
    recording in one step means a failed settlement still consumes budget,
    and skipping the authorize step means the first thing you learn about an
    overspend is the invoice.
    """

    def __init__(self, policy: BudgetPolicy, table_name: str = BUDGET_TABLE,
                 region: str = AWS_REGION):
        problems = policy.validate()
        if problems:
            raise ValueError(
                f"Budget policy for {policy.agent_name} is unsound:\n  "
                + "\n  ".join(problems)
            )
        self.policy = policy
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    def _daily_key(self, now: datetime) -> str:
        return f"{self.policy.agent_name}#day#{now.strftime('%Y-%m-%d')}"

    def _monthly_key(self, now: datetime) -> str:
        return f"{self.policy.agent_name}#month#{now.strftime('%Y-%m')}"

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def authorize(self, request: SpendRequest) -> SpendVerdict:
        now = datetime.now(timezone.utc)
        daily = self._current(self._daily_key(now))
        monthly = self._current(self._monthly_key(now))

        def verdict(decision: Decision, reason: str) -> SpendVerdict:
            v = SpendVerdict(
                decision=decision,
                reason=reason,
                daily_spent=daily,
                monthly_spent=monthly,
                remaining_daily=self.policy.daily_cap_usd - daily,
                remaining_monthly=self.policy.monthly_cap_usd - monthly,
            )
            # Every decision is logged, including the allows. A denial trail
            # that only records denials cannot answer "why was this allowed?"
            logger.info(
                "spend_decision agent=%s session=%s payee=%s amount=%s "
                "decision=%s daily=%s monthly=%s correlation=%s",
                request.agent_name, request.session_id, request.payee,
                request.amount_usd, decision.value, daily, monthly,
                request.correlation_id,
            )
            return v

        if request.payee not in self.policy.allowed_payees:
            return verdict(
                Decision.DENY_PAYEE_NOT_ALLOWED,
                f"payee '{request.payee}' is not on the allowlist",
            )

        if request.amount_usd > self.policy.single_tx_cap_usd:
            return verdict(
                Decision.DENY_SINGLE_TX_CAP,
                f"amount {request.amount_usd} exceeds single-transaction cap "
                f"{self.policy.single_tx_cap_usd}",
            )

        if daily + request.amount_usd > self.policy.daily_cap_usd:
            return verdict(
                Decision.DENY_DAILY_CAP,
                f"daily cap would be exceeded: {daily} + {request.amount_usd} "
                f"> {self.policy.daily_cap_usd}",
            )

        if monthly + request.amount_usd > self.policy.monthly_cap_usd:
            return verdict(
                Decision.DENY_MONTHLY_CAP,
                f"monthly cap would be exceeded: {monthly} + {request.amount_usd} "
                f"> {self.policy.monthly_cap_usd}",
            )

        trailing_avg = self._trailing_average_tx_usd(now)
        if (trailing_avg is not None and trailing_avg > 0
                and request.amount_usd > ANOMALY_MULTIPLIER * trailing_avg):
            return verdict(
                Decision.DENY_CIRCUIT_OPEN,
                f"amount {request.amount_usd} is more than {ANOMALY_MULTIPLIER}x "
                f"the trailing {ANOMALY_WINDOW_DAYS}-day average transaction "
                f"size (${trailing_avg:.4f}); circuit breaker open",
            )

        if (self.policy.escalate_above_usd is not None
                and request.amount_usd > self.policy.escalate_above_usd):
            return verdict(
                Decision.ESCALATE_HUMAN,
                f"amount {request.amount_usd} exceeds the human-approval "
                f"threshold {self.policy.escalate_above_usd}",
            )

        return verdict(Decision.ALLOW, "within all budget tiers")

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, request: SpendRequest, settled_amount_usd: Decimal,
               receipt_id: str) -> Decision:
        """Record a settled payment atomically against both counters.

        Uses ADD, which is atomic in DynamoDB — but ADD alone only makes the
        *write* atomic, not the *check-then-write*. `authorize()` reads a
        snapshot of "spent so far" and returns ALLOW; nothing stops a second
        concurrent session from reading the same stale snapshot, also getting
        ALLOW, and both then calling `record()`. That is the race a batch
        workload guarantees will happen under load.

        The fix is a ConditionExpression on the same update_item call that
        performs the ADD, re-asserting the cap that was checked at read
        time: the write only commits if `spent_usd + settled_amount_usd`
        would still not exceed that counter's cap (or the counter doesn't
        exist yet — the first write for a key). This ties the check to the
        write atomically at the DynamoDB layer, which a Python-level
        read-then-write can never do no matter how careful the code between
        the read and the write looks.

        If a counter's condition is rejected, DynamoDB raises
        ConditionalCheckFailedException. That is not a system error — it
        means this request lost the race to a concurrent one — so it is
        caught and turned into the matching DENY_*_CAP decision rather than
        propagated. Checked in tier order (daily, then monthly); the first
        counter to reject stops the loop, so a rejected daily write never
        also touches the monthly counter for the same settlement.
        """
        now = datetime.now(timezone.utc)
        for key, cap, lost_decision in (
            (self._daily_key(now), self.policy.daily_cap_usd, Decision.DENY_DAILY_CAP),
            (self._monthly_key(now), self.policy.monthly_cap_usd, Decision.DENY_MONTHLY_CAP),
        ):
            try:
                self._table.update_item(
                    Key={"budget_key": key},
                    UpdateExpression="ADD spent_usd :amt, tx_count :one "
                                     "SET last_updated = :ts",
                    ConditionExpression=(
                        "attribute_not_exists(spent_usd) OR "
                        "spent_usd <= :cap_minus_amt"
                    ),
                    ExpressionAttributeValues={
                        ":amt": settled_amount_usd,
                        ":one": 1,
                        ":ts": now.isoformat(),
                        ":cap_minus_amt": cap - settled_amount_usd,
                    },
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    logger.warning(
                        "spend_race_lost agent=%s session=%s payee=%s amount=%s "
                        "key=%s correlation=%s",
                        request.agent_name, request.session_id, request.payee,
                        settled_amount_usd, key, request.correlation_id,
                    )
                    return lost_decision
                raise

        # The receipt is the artifact an auditor asks for. Log it in your own
        # store, not only in the payment rail's, and put the correlation id on
        # it so the payment joins the trace from Chapter 11.
        logger.info(
            "payment_receipt agent=%s session=%s payee=%s amount=%s receipt=%s "
            "purpose=%s correlation=%s",
            request.agent_name, request.session_id, request.payee,
            settled_amount_usd, receipt_id, request.purpose,
            request.correlation_id,
        )
        return Decision.ALLOW

    def _current(self, key: str) -> Decimal:
        try:
            response = self._table.get_item(Key={"budget_key": key})
        except ClientError as exc:
            # Fail closed. A budget system that cannot read its counters must
            # not assume the counters are zero.
            raise RuntimeError(f"budget counter unavailable for {key}: {exc}")
        item = response.get("Item")
        return Decimal(str(item["spent_usd"])) if item and "spent_usd" in item else Decimal("0")

    # ------------------------------------------------------------------
    # Tier 4: anomaly / circuit breaker
    # ------------------------------------------------------------------

    def _trailing_average_tx_usd(self, now: datetime,
                                  days: int = ANOMALY_WINDOW_DAYS) -> Optional[Decimal]:
        """Trailing daily-average transaction size, from data already tracked.

        No new storage: this walks back over the same per-day counters
        `_current` reads (`spent_usd`, `tx_count`), one `get_item` per day,
        for the `days` calendar days before today (today is still
        accumulating and is excluded so a burst of legitimate spend earlier
        in the day cannot inflate its own baseline).

        Returns None when there are fewer than ANOMALY_MIN_SAMPLE_TX
        transactions in the window — not enough history to call anything
        "unusual" against, which is the common case for a newly deployed
        agent. Callers must treat None as "skip the anomaly check," not as
        zero.
        """
        total_spent = Decimal("0")
        total_tx = 0
        for i in range(1, days + 1):
            day = now - timedelta(days=i)
            key = self._daily_key(day)
            try:
                response = self._table.get_item(Key={"budget_key": key})
            except ClientError as exc:
                raise RuntimeError(f"budget counter unavailable for {key}: {exc}")
            item = response.get("Item")
            if item:
                total_spent += Decimal(str(item.get("spent_usd", 0)))
                total_tx += int(item.get("tx_count", 0))
        if total_tx < ANOMALY_MIN_SAMPLE_TX:
            return None
        return total_spent / total_tx


# =========================================================================
# Duplicate-purchase detection
# =========================================================================
# The specific failure from Section 12.1: the agent re-bought access it had
# already paid for, once per session, because nothing carried the purchase
# across sessions. The fix is a purchase ledger the agent consults first —
# and the natural home for it is the same Memory the agent already has
# (Chapter 7), keyed by tenant, not by session.

def already_purchased(
    memory_client,
    tenant_id: str,
    payee: str,
    resource: str,
) -> Optional[dict[str, Any]]:
    """Check whether this tenant already bought this resource.

    Returns the prior purchase record if one exists. Call this before any
    payment for something with a duration — a subscription, a licence, a
    time-boxed feed. Micro-payments for single queries do not need it.

    No recency filter: matching is plain substring containment over
    whatever the memory client returns, with no time bound. An earlier
    version of this function took a `within_hours` argument and computed a
    cutoff timestamp that it never applied — chapter-07's
    `MemoryClient.retrieve()` (utils/memory_helpers.py) does not return a
    created-at timestamp in the record dict it hands back (it discards
    `createdAt` when building the `records` list), so there was nothing to
    compare the cutoff against. That parameter was removed rather than
    wired up: adding real recency filtering means changing chapter-07's
    return shape, which is out of scope here. If you need "recently," add
    a timestamp field to `MemoryClient.retrieve()`'s output first, then
    filter on it here.
    """
    namespace = f"tenants/{tenant_id}/purchases"
    results = memory_client.retrieve(
        namespace=namespace,
        query=f"purchase of {resource} from {payee}",
        max_results=5,
    )
    for record in results.get("records", []):
        content = record.get("content", "")
        if payee in content and resource in content:
            return {"match": content, "score": record.get("score")}
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("Validating the Acme Lending budget policy...\n")
    problems = ACME_CREDIT_AGENT_BUDGET.validate()
    print("  sound" if not problems else "\n".join(f"  {p}" for p in problems))

    print("\nTier relationships:")
    p = ACME_CREDIT_AGENT_BUDGET
    print(f"  single tx cap : ${p.single_tx_cap_usd}")
    print(f"  daily cap     : ${p.daily_cap_usd}   ({p.daily_cap_usd / p.single_tx_cap_usd:.0f} max transactions/day)")
    binding_day = math.ceil(p.monthly_cap_usd / p.daily_cap_usd)
    print(f"  monthly cap   : ${p.monthly_cap_usd}  (binds at day {binding_day} of sustained max spend)")
    print(f"  escalate above: ${p.escalate_above_usd}")
    print(f"  allowed payees: {len(p.allowed_payees)}")

    print("\nAn unsound policy for comparison:")
    bad = BudgetPolicy(
        agent_name="bad-example",
        single_tx_cap_usd=Decimal("100.00"),
        daily_cap_usd=Decimal("200.00"),
        monthly_cap_usd=Decimal("50000.00"),
        allowed_payees=frozenset(),
    )
    for problem in bad.validate():
        print(f"  - {problem}")
