"""The bluffing seller: a simulated data vendor, and Tally's audit of its offer.

The vendor is our own simulated service (agents stay "in the room"), and the
screen says so. It offers a premium list of engineering heads for the same
companies and claims it is fresh. It isn't: it lists people who have left,
and it has mixed up who works where.

Tally doesn't take the claim on trust. It checks a sample against facts it
already holds receipts for, so the check costs nothing, and then:

* recommends buying only if the sample is at least 90% right (buying would be
  a payment, so it would wait for a person and run in a test mode);
* counters at the price times the accuracy it measured, if half or more is right;
* declines otherwise. Every verdict carries its evidence.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from tally.models import Answer, Status

BUY_IF_ACCURACY_AT_LEAST = 0.9
COUNTER_IF_ACCURACY_AT_LEAST = 0.5


@dataclass(frozen=True)
class VendorRecord:
    company: str
    person: str
    title: str


@dataclass
class Offer:
    vendor: str
    pitch: str
    price_credits: float
    records: list[VendorRecord]
    simulated: bool = True


@dataclass(frozen=True)
class SampleCheck:
    record: VendorRecord
    verdict: str  # matches | left | wrong person | nothing supports it
    evidence: str


@dataclass
class Audit:
    offer: Offer
    checks: list[SampleCheck] = field(default_factory=list)
    accuracy: float = 0.0
    decision: str = ""
    counter_price: float | None = None

    def as_dict(self) -> dict:
        return {
            "vendor": self.offer.vendor, "pitch": self.offer.pitch, "price": self.offer.price_credits,
            "simulated": self.offer.simulated, "accuracy": round(self.accuracy, 2), "decision": self.decision,
            "counter_price": self.counter_price,
            "checks": [{"company": c.record.company, "vendor_says": f"{c.record.person}, {c.record.title}",
                        "verdict": c.verdict, "evidence": c.evidence} for c in self.checks],
        }


def make_offer(answers: list[Answer], price_per_record: float = 6.0) -> Offer:
    """The vendor's list for the same companies, with the mistakes a stale list really has."""
    named = [a for a in answers if a.person is not None]
    records = [VendorRecord(a.company.name, a.person.name, a.person.title) for a in named]
    # Who works where got mixed up between two companies.
    if len(records) >= 4:
        first, second = records[1], records[3]
        records[1] = VendorRecord(first.company, second.person, first.title)
        records[3] = VendorRecord(second.company, first.person, second.title)
    return Offer(
        vendor="FreshLeads Data (simulated)",
        pitch=f"Verified heads of engineering at all {len(records)} companies, refreshed last week. "
              f"{price_per_record * len(records):.0f} credits for the lot.",
        price_credits=price_per_record * len(records),
        records=records,
    )


def audit_offer(offer: Offer, answers: list[Answer], sample_size: int = 5, seed: int = 7) -> Audit:
    """Check a sample of the vendor's records against Tally's own receipts."""
    ours = {a.company.name: a for a in answers}
    sample = random.Random(seed).sample(offer.records, min(sample_size, len(offer.records)))
    audit = Audit(offer, [_check(record, ours.get(record.company)) for record in sample])
    audit.accuracy = sum(c.verdict == "matches" for c in audit.checks) / max(len(audit.checks), 1)

    if audit.accuracy >= BUY_IF_ACCURACY_AT_LEAST:
        audit.decision = "recommend buying: it waits for a person's approval, paid in a test mode"
    elif audit.accuracy >= COUNTER_IF_ACCURACY_AT_LEAST:
        audit.counter_price = round(offer.price_credits * audit.accuracy, 2)
        audit.decision = f"counter at {audit.counter_price:g} credits: the price times the accuracy we measured"
    else:
        audit.decision = "decline: most of the sample is wrong"
    return audit


def _check(record: VendorRecord, ours: Answer | None) -> SampleCheck:
    if ours is None or ours.person is None:
        return SampleCheck(record, "nothing supports it", "no record we bought names anyone in this role")
    source = next((s for s in ours.sources if s.kind == "crustdata_record"), None)
    dated = f", record dated {(source.record_date or 'unknown')[:10]}" if source else ""
    if ours.status is Status.CONFLICT and record.person == ours.person.name:
        news = next((s for s in ours.sources if s.kind == "web_page"), None)
        return SampleCheck(record, "left", f"newer news: \"{news.value}\" ({news.ref})" if news else "newer news")
    if record.person != ours.person.name:
        return SampleCheck(record, "wrong person",
                           f"our record shows {ours.person.name}, {ours.person.title}{dated}")
    return SampleCheck(record, "matches", f"our record agrees{dated}")
