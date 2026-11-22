"""The wallet: every paid call goes through here, and code (never a model) decides.

Budgets form a tree. The root is the client's budget; each sub-agent's budget
is carved out of its parent, and only a parent can grant more. A purchase is
allowed only if the buyer's own node can cover its worst-case cost.

Three controls stop spending:

* `cut(limit)`: the client lowers the ceiling (from the desk or the phone).
* the deadline: every run has a time limit as well as a credit limit.
* `kill()`: no new purchases at all.

Paid calls go one at a time through a single "purchase lane". After any of
the three controls fires, at most the one call already in flight settles.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from tally.crust.errors import CrustError, SourceTimeout
from tally.ledger import Ledger, sha256

EPSILON = 1e-9


class BudgetRefused(Exception):
    """A purchase was not made. `reason` is over_budget, deadline, killed or denied."""

    def __init__(self, reason: str, budget_id: str, needed: float, available: float):
        super().__init__(f"{reason}: budget {budget_id} needs {needed:.2f}, has {available:.2f}")
        self.reason = reason
        self.budget_id = budget_id
        self.needed = needed
        self.available = available


# ----- the budget tree -------------------------------------------------------

@dataclass(eq=False)
class BudgetNode:
    """One budget in the tree. Amounts are in credits."""

    id: str
    limit: float
    parent: BudgetNode | None = None
    spent: float = 0.0  # settled by this node itself
    reserved: float = 0.0  # in flight for this node itself
    children: list[BudgetNode] = field(default_factory=list)

    def available(self) -> float:
        """What this node may still commit itself."""
        return self.limit - self.spent - self.reserved - sum(c.limit for c in self.children)

    def committed(self) -> float:
        """The least this subtree can be cut to: what it has spent or has in flight."""
        return self.spent + self.reserved + sum(c.committed() for c in self.children)

    def total_spent(self) -> float:
        return self.spent + sum(c.total_spent() for c in self.children)

    def carve(self, child_id: str, amount: float) -> BudgetNode:
        """Give a sub-agent its own budget, taken out of this one."""
        if amount < 0 or amount > self.available() + EPSILON:
            raise ValueError(f"can't carve {amount:.2f} from {self.id}, which has {self.available():.2f}")
        child = BudgetNode(child_id, amount, parent=self)
        self.children.append(child)
        return child

    def grant(self, child: BudgetNode, amount: float) -> None:
        """A parent gives a child more. A child has no way to raise its own limit."""
        if child.parent is not self:
            raise ValueError(f"{self.id} is not the parent of {child.id}")
        if amount < 0 or amount > self.available() + EPSILON:
            raise ValueError(f"can't grant {amount:.2f} from {self.id}, which has {self.available():.2f}")
        child.limit += amount

    def reclaim(self, child: BudgetNode) -> float:
        """Take back what a child hasn't committed. Returns the amount reclaimed."""
        if child.parent is not self:
            raise ValueError(f"{self.id} is not the parent of {child.id}")
        unused = max(0.0, child.limit - child.committed())
        _set_limit(child, child.limit - unused)
        return unused

    def find(self, budget_id: str) -> BudgetNode | None:
        if self.id == budget_id:
            return self
        for child in self.children:
            found = child.find(budget_id)
            if found:
                return found
        return None

    def as_dict(self) -> dict:
        """For the UI's budget-tree view."""
        return {
            "id": self.id, "limit": round(self.limit, 4), "spent": round(self.spent, 4),
            "reserved": round(self.reserved, 4), "children": [c.as_dict() for c in self.children],
        }


def _set_limit(node: BudgetNode, new_limit: float) -> None:
    """Set a node's limit, shrinking its children so they still fit inside it.

    Children keep at least what they have committed. Above that, the room left
    is shared out in proportion to each child's unused headroom. A limit below
    what is already committed is allowed: nothing new can be bought until the
    limit rises again.
    """
    node.limit = new_limit
    room = node.limit - node.spent - node.reserved
    kids = node.children
    if not kids or sum(k.limit for k in kids) <= room + EPSILON:
        return
    floors = [k.committed() for k in kids]
    spare = room - sum(floors)
    if spare <= 0:
        for kid, floor in zip(kids, floors):
            _set_limit(kid, floor)
        return
    headroom = [k.limit - f for k, f in zip(kids, floors)]
    total_headroom = sum(headroom)
    for kid, floor, head in zip(kids, floors, headroom):
        _set_limit(kid, floor + spare * head / total_headroom)


# ----- approvals ----------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalRequest:
    """A purchase that needs a person's yes. The binding ties the yes to this exact purchase."""

    call_key: str
    endpoint: str
    credits: float
    reason: str

    @property
    def binding(self) -> str:
        return sha256(f"{self.call_key}|{self.credits:.6f}")


class Approver(Protocol):
    async def decide(self, request: ApprovalRequest) -> bool: ...


class DenyAll:
    """The safe default when nobody is watching: anything over the threshold is refused."""

    async def decide(self, request: ApprovalRequest) -> bool:
        return False


# ----- the wallet ---------------------------------------------------------------

@dataclass(frozen=True)
class Purchase:
    """What a paid call returned."""

    body: Any
    credits: float
    request_id: str | None
    from_ledger: bool = False  # True when it was bought earlier and served again for free


# The function that actually makes the HTTP call: () -> (body, credits charged, request id).
PerformCall = Callable[[], Awaitable[tuple[Any, float, str | None]]]


class Wallet:
    def __init__(self, root: BudgetNode, ledger: Ledger, run_id: str, deadline: float,
                 approve_over: float = 20.0, approver: Approver | None = None,
                 on_change: Callable[[str, dict], None] | None = None):
        self.root = root
        self.ledger = ledger
        self.run_id = run_id
        self.deadline = deadline  # epoch seconds
        self.approve_over = approve_over
        self.approver = approver or DenyAll()
        self.on_change = on_change or (lambda kind, data: None)
        self.killed = False
        self._lane = asyncio.Lock()

    # ----- controls -----

    def cut(self, new_limit: float, actor: str = "client") -> None:
        """Lower (or raise) the client's ceiling. Takes effect before the next reservation,
        and is written to the ledger so a restart keeps it."""
        _set_limit(self.root, new_limit)
        self.ledger.set_limits(self.run_id, self.root.limit, self.deadline)
        self.on_change("cut", {"limit": self.root.limit, "actor": actor})

    def extend(self, minutes: float) -> None:
        """Give the run more time (a top-up comes with some)."""
        self.deadline = max(self.deadline, time.time()) + minutes * 60
        self.ledger.set_limits(self.run_id, self.root.limit, self.deadline)

    def kill(self, actor: str = "client") -> None:
        self.killed = True
        self.on_change("kill", {"actor": actor})

    def seconds_left(self) -> float:
        return max(0.0, self.deadline - time.time())

    def spent(self) -> float:
        return self.root.total_spent()

    # ----- buying -----

    async def buy(self, node: BudgetNode, key: str, endpoint: str, est_max: float,
                  reason: str, perform: PerformCall) -> Purchase:
        """Buy one call, at most once, within `node`'s budget.

        `est_max` must be the worst-case cost; the purchase is refused if the
        node can't cover it. The real cost (from the response) is what's spent.
        """
        saved = self.ledger.saved_response(key)
        if saved is not None:
            self.ledger.record_cache_hit(self.run_id, key, endpoint, reason)
            return Purchase(body=saved, credits=0.0, request_id=None, from_ledger=True)

        if est_max > self.approve_over:
            request = ApprovalRequest(key, endpoint, est_max, reason)
            self.on_change("approval_needed", {"binding": request.binding, "endpoint": endpoint,
                                               "credits": est_max, "reason": reason})
            if not await self.approver.decide(request):
                raise BudgetRefused("denied", node.id, est_max, node.available())

        async with self._lane:
            self._check_can_spend(node, est_max)
            node.reserved += est_max
            self.ledger.reserve(self.run_id, key, node.id, endpoint, est_max, reason)
            self.on_change("reserved", {"budget": node.id, "endpoint": endpoint, "credits": est_max,
                                        "reason": reason})
            try:
                body, credits, request_id = await perform()
            except (Exception, asyncio.CancelledError) as error:
                # (A hard stop such as KeyboardInterrupt is not caught: like a killed
                # process, it leaves the reservation open for recovery to find.)
                node.reserved -= est_max
                self._record_failure(node, key, error)
                raise
            node.reserved -= est_max
            node.spent += credits
            self.ledger.settle(key, credits, request_id, body)
            self.on_change("settled", {"budget": node.id, "endpoint": endpoint, "credits": credits,
                                       "spent": self.spent(), "limit": self.root.limit})
            return Purchase(body=body, credits=credits, request_id=request_id)

    def _check_can_spend(self, node: BudgetNode, needed: float) -> None:
        if self.killed:
            raise BudgetRefused("killed", node.id, needed, node.available())
        if time.time() >= self.deadline:
            raise BudgetRefused("deadline", node.id, needed, node.available())
        if needed > node.available() + EPSILON:
            raise BudgetRefused("over_budget", node.id, needed, node.available())

    def _record_failure(self, node: BudgetNode, key: str, error: BaseException) -> None:
        """An error response is free; a lost response might not be, so it stays in doubt."""
        if isinstance(error, SourceTimeout) or not isinstance(error, CrustError):
            # No response, so we can't know whether Crustdata charged us. Count it (ledger rule 3).
            self.ledger.mark_in_doubt(key, note=repr(error)[:200])
            node.spent += self.ledger.state(key).charged
        else:
            self.ledger.fail(key, note=str(error)[:200], credits=error.credits, request_id=error.request_id)
            node.spent += error.credits
        self.on_change("failed", {"budget": node.id, "error": str(error)[:200]})
