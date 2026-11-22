import asyncio
import time

import pytest
from hypothesis import given, settings as hyp_settings, strategies as st

from tally.crust.errors import EndpointForbidden, SourceTimeout
from tally.ledger import call_key
from tally.wallet import ApprovalRequest, BudgetNode, BudgetRefused, Wallet, _set_limit


def make_wallet(ledger, limit=100.0, deadline_in=600.0, **kwargs):
    root = BudgetNode("root", limit)
    return Wallet(root, ledger, "run1", deadline=time.time() + deadline_in, **kwargs)


def charge(credits, delay=0.0):
    """A stand-in HTTP call that costs `credits`."""
    async def perform():
        if delay:
            await asyncio.sleep(delay)
        return {"ok": True}, credits, "req"
    return perform


async def test_a_purchase_within_budget_is_made_once(ledger):
    wallet = make_wallet(ledger)
    key = call_key("run1", "/person/search", {"q": 1})
    first = await wallet.buy(wallet.root, key, "/person/search", 3.0, "probe", charge(2.5))
    again = await wallet.buy(wallet.root, key, "/person/search", 3.0, "probe", charge(2.5))

    assert (first.credits, first.from_ledger) == (2.5, False)
    assert (again.credits, again.from_ledger) == (0.0, True)  # never bought twice
    assert wallet.spent() == 2.5 == ledger.credits_charged("run1")


async def test_a_purchase_over_the_limit_is_refused(ledger):
    wallet = make_wallet(ledger, limit=5.0)
    with pytest.raises(BudgetRefused) as refused:
        await wallet.buy(wallet.root, "k", "/company/enrich", 6.0, "too big", charge(6.0))
    assert refused.value.reason == "over_budget"
    assert ledger.states() == []  # nothing reserved, nothing spent


async def test_a_cut_stops_spending_within_one_call(ledger):
    events = []
    wallet = make_wallet(ledger, limit=100.0, on_change=lambda kind, data: events.append(kind))

    async def buyer(i):
        try:
            await wallet.buy(wallet.root, f"k{i}", "/x", 2.0, "probe", charge(2.0, delay=0.01))
        except BudgetRefused:
            pass

    tasks = [asyncio.create_task(buyer(i)) for i in range(30)]
    await asyncio.sleep(0.035)  # a few purchases in, one in flight
    spent_at_cut = wallet.spent()
    wallet.cut(spent_at_cut)  # no room left at all
    await asyncio.gather(*tasks)

    after_cut = events[events.index("cut"):]
    assert after_cut.count("settled") <= 1  # only the call already in flight
    assert after_cut.count("reserved") == 0  # nothing new was started
    assert wallet.spent() <= spent_at_cut + 2.0


async def test_the_deadline_stops_spending(ledger):
    wallet = make_wallet(ledger, deadline_in=-1)
    with pytest.raises(BudgetRefused) as refused:
        await wallet.buy(wallet.root, "k", "/x", 1.0, "late", charge(1.0))
    assert refused.value.reason == "deadline"


async def test_kill_stops_spending(ledger):
    wallet = make_wallet(ledger)
    wallet.kill()
    with pytest.raises(BudgetRefused) as refused:
        await wallet.buy(wallet.root, "k", "/x", 1.0, "after kill", charge(1.0))
    assert refused.value.reason == "killed"


class RecordingApprover:
    def __init__(self, answer):
        self.answer, self.seen = answer, []

    async def decide(self, request: ApprovalRequest) -> bool:
        self.seen.append(request)
        return self.answer


async def test_a_big_purchase_waits_for_a_person(ledger):
    no = RecordingApprover(False)
    wallet = make_wallet(ledger, approve_over=20.0, approver=no)
    with pytest.raises(BudgetRefused) as refused:
        await wallet.buy(wallet.root, "big", "/vendor/list", 50.0, "premium list", charge(50.0))
    assert refused.value.reason == "denied" and no.seen[0].credits == 50.0

    yes = RecordingApprover(True)
    wallet = make_wallet(ledger, approve_over=20.0, approver=yes)
    bought = await wallet.buy(wallet.root, "big2", "/vendor/list", 50.0, "premium list", charge(50.0))
    assert bought.credits == 50.0
    assert yes.seen[0].binding != ApprovalRequest("big2", "/vendor/list", 49.0, "x").binding  # bound to the amount


async def test_a_timeout_stays_in_doubt_and_counts(ledger):
    wallet = make_wallet(ledger)

    async def times_out():
        raise SourceTimeout("no answer")

    with pytest.raises(SourceTimeout):
        await wallet.buy(wallet.root, "k", "/person/enrich", 9.0, "confirm", times_out)
    assert ledger.state("k").event == "in_doubt"
    assert wallet.spent() == 9.0 == ledger.credits_charged()


async def test_an_error_response_is_free(ledger):
    wallet = make_wallet(ledger)

    async def forbidden():
        raise EndpointForbidden("403", status=403, credits=0.0)

    with pytest.raises(EndpointForbidden):
        await wallet.buy(wallet.root, "k", "/web/search/live", 1.0, "news", forbidden)
    assert ledger.state("k").event == "failed"
    assert wallet.spent() == 0.0


# ----- properties of the budget tree --------------------------------------------

operations = st.lists(
    st.one_of(
        st.tuples(st.just("carve"), st.integers(0, 3), st.floats(0, 60)),
        st.tuples(st.just("grant"), st.integers(0, 3), st.floats(0, 60)),
        st.tuples(st.just("spend"), st.integers(0, 3), st.floats(0, 30)),
        st.tuples(st.just("cut"), st.integers(0, 0), st.floats(0, 200)),
    ),
    max_size=40,
)


@hyp_settings(max_examples=300, deadline=None)
@given(operations)
def test_the_budget_tree_never_lets_a_node_overspend(ops):
    root = BudgetNode("root", 100.0)
    nodes = [root]
    for op, index, amount in ops:
        node = nodes[index % len(nodes)]
        limit_before = {n.id: n.limit for n in nodes}
        if op == "carve" and amount <= node.available():
            nodes.append(node.carve(f"n{len(nodes)}", amount))
        elif op == "grant" and node.parent and amount <= node.parent.available():
            node.parent.grant(node, amount)
        elif op == "spend" and amount <= node.available():
            node.spent += amount  # what a settled purchase does
        elif op == "cut":
            _set_limit(root, amount)  # the client may lower or raise their own ceiling

        for n in nodes:
            # Below the root, a limit only rises through the parent's grant.
            raised_legitimately = (op == "grant" and n is node) or (op == "cut" and n is root)
            if not raised_legitimately:
                assert n.limit <= limit_before.get(n.id, n.limit) + 1e-9
            # Spending never goes past a node's own committed budget.
            assert n.spent + n.reserved <= n.limit - sum(c.limit for c in n.children) + 1e-6 \
                or n.limit < n.committed() + 1e-6
        # Everything spent fits inside the root, unless the root was cut below what was already spent.
        assert root.total_spent() <= max(root.limit, root.committed()) + 1e-6
