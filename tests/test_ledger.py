import sqlite3

from tally.ledger import Ledger, call_key


def test_a_settled_call_is_saved_and_charged(ledger):
    key = call_key("run1", "/person/search", {"limit": 5})
    ledger.reserve("run1", key, "root", "/person/search", 0.15, "find people")
    assert ledger.saved_response(key) is None  # reserved is not bought yet
    ledger.settle(key, 0.12, "req-1", {"profiles": []})

    assert ledger.saved_response(key) == {"profiles": []}
    assert ledger.credits_charged("run1") == 0.12
    state = ledger.state(key)
    assert (state.event, state.reserved, state.request_id) == ("settled", 0.15, "req-1")


def test_the_same_request_gets_the_same_key_and_a_retry_does_not():
    body = {"filters": {"field": "x", "type": "=", "value": 1}, "limit": 5}
    reordered = {"limit": 5, "filters": {"value": 1, "type": "=", "field": "x"}}
    assert call_key("r", "/p", body) == call_key("r", "/p", reordered)
    assert call_key("r", "/p", body) != call_key("r", "/p", body, attempt=1)


def test_an_unfinished_call_is_counted_at_its_reserved_amount(ledger):
    key = call_key("run1", "/company/enrich", {"crustdata_company_ids": [1]})
    ledger.reserve("run1", key, "root", "/company/enrich", 2.0, "people at company 1")
    assert [s.call_key for s in ledger.open_reservations()] == [key]

    ledger.mark_in_doubt(key, "process restarted mid-call")
    assert ledger.credits_charged("run1") == 2.0  # rule 3: count it until we know
    ledger.resolve(key, 0.0, "usage events show no charge")
    assert ledger.credits_charged("run1") == 0.0
    assert ledger.open_reservations() == []


def test_model_calls_are_in_the_ledger(ledger):
    ledger.record_model_call("run1", "read the job", "claude-opus-5-5", 1200, 300, cost_inr=0.95)
    assert ledger.model_cost_inr("run1") == 0.95
    assert ledger.credits_charged("run1") == 0.0


def test_the_hash_chain_catches_an_edited_entry(tmp_path):
    book = Ledger(tmp_path / "l.sqlite")
    for i in range(3):
        key = call_key("run1", "/person/search", {"i": i})
        book.reserve("run1", key, "root", "/person/search", 0.3, "probe")
        book.settle(key, 0.3, f"req-{i}", {})
    assert book.verify_chain().ok
    book.close()

    raw = sqlite3.connect(tmp_path / "l.sqlite")
    raw.execute("UPDATE entries SET credits = 0.01 WHERE seq = 4")
    raw.commit()
    raw.close()

    check = Ledger(tmp_path / "l.sqlite").verify_chain()
    assert not check.ok and check.first_bad_seq == 4
