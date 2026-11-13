"""
hop_budget.py — Carry a hop limit, a deadline, and a call budget across
agent-to-agent calls, and retry at one layer only.

Accompanies Chapter 12 (Section 12.8). Extends the RequestContext from
chapter-11/observability/cross_agent_trace.py with three fields:

    max_hops        how deep the delegation chain may go
    deadline_epoch  absolute time after which no agent should start work
    calls_left      remaining downstream calls for the whole user request

Why: three layers that each retry three times turn one transient failure
into up to 27 leaf attempts. The budget is cooperative (every agent's code
must honor it), which is why the chapter pairs it with Gateway rate limits
and a Dogwood `count` policy as backstops.

Run the local demo:
    python amplification/hop_budget.py
"""

import json
import time
from dataclasses import asdict, dataclass, field

HEADER = "x-acme-request-context"


class BudgetExhausted(Exception):
    """Raised instead of delegating. Callers treat it as final: no retry."""


@dataclass(frozen=True)
class BudgetedContext:
    correlation_id: str
    tenant_id: str
    origin: str
    hop: int = 0
    path: tuple = field(default_factory=tuple)
    max_hops: int = 3
    deadline_epoch: float = 0.0
    calls_left: int = 8

    @classmethod
    def start(cls, correlation_id, tenant_id, origin, seconds=60.0, max_hops=3, calls=8):
        return cls(correlation_id, tenant_id, origin,
                   max_hops=max_hops, deadline_epoch=time.time() + seconds, calls_left=calls)

    def delegate_to(self, agent_name: str) -> "BudgetedContext":
        """Derive the context for the next hop, or refuse."""
        if self.hop + 1 > self.max_hops:
            raise BudgetExhausted(f"hop limit {self.max_hops} reached at {list(self.path)}")
        if time.time() >= self.deadline_epoch:
            raise BudgetExhausted("deadline passed; not starting downstream work")
        if self.calls_left <= 0:
            raise BudgetExhausted("call budget for this request is spent")
        return BudgetedContext(
            self.correlation_id, self.tenant_id, self.origin,
            hop=self.hop + 1, path=(*self.path, agent_name),
            max_hops=self.max_hops, deadline_epoch=self.deadline_epoch,
            calls_left=self.calls_left - 1,
        )

    def remaining_seconds(self) -> float:
        """Pass this as the timeout of the downstream call: every participant
        bounds its wait, and no hop waits past the user's deadline."""
        return max(0.0, self.deadline_epoch - time.time())

    def as_header(self) -> dict:
        return {HEADER: json.dumps(asdict(self))}

    @classmethod
    def from_header(cls, headers: dict) -> "BudgetedContext":
        data = json.loads(headers[HEADER])
        data["path"] = tuple(data["path"])
        return cls(**data)


def call_with_single_layer_retry(ctx: BudgetedContext, agent_name: str, send, attempts=3):
    """Retry here, at the layer closest to the failure, and only here.

    `send(context, timeout)` performs the downstream call. A BudgetExhausted
    from below is final and propagates without retry.
    """
    last_error = None
    for _ in range(attempts):
        child = ctx.delegate_to(agent_name)
        ctx = BudgetedContext(**{**asdict(ctx), "calls_left": child.calls_left,
                                 "path": ctx.path})
        try:
            return send(child, child.remaining_seconds())
        except BudgetExhausted:
            raise
        except TimeoutError as error:
            last_error = error
    raise BudgetExhausted(f"{agent_name} failed {attempts} times: {last_error}")


def leaf_attempts(layers: int, retries_per_layer: int) -> int:
    """Worst-case attempts at the leaf when every layer retries independently."""
    return retries_per_layer ** layers


def _demo() -> None:
    print("independent retries, 3 layers x 3 attempts ->", leaf_attempts(3, 3), "leaf attempts")

    ctx = BudgetedContext.start("req-41c9e2", "globex-corp", "support-chat", calls=4)
    flaky_calls = {"n": 0}

    def flaky_send(child, timeout):
        flaky_calls["n"] += 1
        raise TimeoutError(f"refunds-agent timed out after {timeout:.1f}s")

    try:
        call_with_single_layer_retry(ctx, "refunds-agent", flaky_send)
    except BudgetExhausted as error:
        print(f"[stopped after {flaky_calls['n']} attempts] {error}")

    deep = BudgetedContext.start("req-9", "globex-corp", "support-chat", max_hops=2)
    try:
        deep.delegate_to("billing-specialist").delegate_to("refunds-agent").delegate_to("notifications-agent")
    except BudgetExhausted as error:
        print(f"[refused] {error}")


if __name__ == "__main__":
    _demo()
