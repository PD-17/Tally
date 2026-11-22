# Tally · Diligence Desk

**Every claim in a pitch deck is checked against outside records before the second meeting. Each verdict comes with a receipt, and the whole check stays inside a budget the partner sets.**

ContextCon 05, BITS Pilani · Theme 2, Investing · Statement 2.2, The Diligence Desk.

```
deck.pdf ─► read (Claude Opus + rules) ─► identify company ─► buy 1 record ─► QUOTE
        ─► partner accepts ─► ranked checks inside the ceiling ─► memo with receipts
        ─► questions for founders (held until the partner approves that exact set)
```

> **In one paragraph, for reviewers.** The model has exactly one job: turning an untrusted PDF into a typed list of claims (`DeckReading`). It has no tools, no budget and no say over verdicts. Everything with consequences runs in deterministic Python: worst-case pricing, admission control against a hierarchical budget, idempotent purchases, write-ahead accounting in an append-only hash-chained SQLite ledger, crash reconciliation against the vendor's usage records, and one pure judge function per claim kind. A purchase can't exceed the ceiling, can't be made twice and can't go unrecorded, and a verdict can't come from a model.

---

## Contents

1. [The problem](#the-problem)
2. [What the partner gets](#what-the-partner-gets)
3. [Why the memo can be trusted](#why-the-memo-can-be-trusted)
4. [Live results](#live-results-22-nov-2026)
5. [Run it](#run-it)
6. [Architecture](#architecture)
   - [System view](#system-view) · [Module dependency graph](#module-dependency-graph) · [Layering](#layering) · [Source map](#source-map)
7. [The pipeline, step by step](#the-pipeline-step-by-step)
   - [End-to-end sequence](#end-to-end-sequence) · [Run lifecycle](#run-lifecycle)
8. [The spending engine](#the-spending-engine)
   - [Budget tree](#budget-tree) · [Purchase protocol](#the-purchase-protocol-walletbuy) · [Ledger](#the-ledger) · [Crash recovery](#crash-recovery) · [Crustdata client](#crustdata-client)
9. [The verdict engine](#the-verdict-engine)
10. [Deck security](#deck-security)
11. [System invariants](#system-invariants)
12. [Concurrency and consistency model](#concurrency-and-consistency-model)
13. [Failure modes](#failure-modes)
14. [HTTP API and event stream](#http-api)
15. [Configuration and modes](#configuration-and-modes)
16. [Testing and evaluation](#testing-and-evaluation)
17. [Business case](#business-case)
18. [Design decisions](#design-decisions)
19. [Known limits](#known-limits)

---

## The problem

After a first meeting, a partner forwards the deck and the founders' names to the diligence team. An analyst then spends a day or two checking the deck against what's on record: the team size, the rounds, the investors, the founders' past jobs and the customers. The job is slow, it doesn't happen for every deck, and the output is usually a page of notes with no source for each line.

Automating it with "give an LLM the deck and some tools" fails on three counts:

| What goes wrong | Why it matters |
|---|---|
| Spend has no ceiling | A prompt can't enforce a budget. Paid data APIs bill per call. |
| The deck is adversarial | It was written by the company being checked, and it can carry instructions aimed at the AI reviewer. |
| "The model thinks it's wrong" isn't evidence | A partner can't take a hunch back to a founder. They need the field, the record and its date. |

Tally keeps the model for the one job it is good at, reading a messy PDF. Everything else runs in deterministic code: pricing, buying, judging and spending control.

## What the partner gets

**Before any money is spent beyond the first record**, a quote (the numbers here are illustrative):

> 14 claims in the deck. The company record already settles 9. For 30 credits, 13 of 14 get checked; a full check costs about 17.4 credits (₹1,400 at our price).

The quote includes a **coverage curve** (credits → claims checked), so the partner can see what each extra credit buys.

**After the run**, a memo containing:

- **One verdict per claim**, sorted by severity and then by materiality. Contradictions come first.
- **A receipt for each verdict**: the exact field or passage, the record or URL, the record's date, and the purchase (call key, request id, credits) that bought it.
- **Five comparable companies**, each with dated headcount, total funding and last round.
- **Questions for the founders**, generated from fixed templates and limited to 8. They are bound to a hash and written to an outbox only after approval.
- **An audit panel**: ledger hash chain OK, ledger credits vs. the account's balance drop, and model cost in ₹.

| Verdict | Meaning |
|---|---|
| **Confirmed** | A record says so, within the rounding tolerance. |
| **Contradicted** | A record that exists says otherwise. It is shown with its date. |
| **Records disagree** | Two records disagree with each other, e.g. a 20-day-old headcount vs. yesterday's job post. Both are shown with dates. |
| **Can't verify** | No record settles it. **A missing record is never a contradiction.** |
| **Held** | A source timed out three times, so the claim is held for a person. |
| **Not checked** | The budget or time ran out first. "Top up 20 credits" continues from there. |

**Controls while it runs**, from the desk or a phone on the same Wi-Fi: halve the budget, set a new limit, kill the run, approve or deny a large purchase. Each one takes effect before the next purchase.

## Why the memo can be trusted

| Guarantee | Mechanism | Where |
|---|---|---|
| A model never sets a verdict | Verdicts come from one pure function per claim kind, with fixed tolerances. The model's only output is a list of claims. | `diligence/claims.py`, `llm.py` |
| A model can't hide a claim | A regex reader runs on the text layer independently. Its claims are merged in whenever the model missed them. | `diligence/deck.py` |
| A misquote is caught | `quote_in_text` checks that each claim's quoted wording really appears in the deck's text layer. | `diligence/deck.py` |
| Spend can't exceed the ceiling | Each purchase is reserved at its **worst-case** price, under a lock, before the request leaves. | `wallet.py`, `prices.py` |
| Nothing is bought twice | The idempotency key is `sha256(run_id │ endpoint │ canonical_json(body) │ attempt)`. A settled key is served from the ledger for 0 credits. | `ledger.py`, `probes.py` |
| A crash never loses a charge | Write-ahead `reserved` entries and an `in_doubt` state, reconciled against Crustdata's usage records. | `recovery.py` |
| The ledger can't be edited quietly | Append-only, with each row hashed as `sha256(prev_hash + row)`. `tally verify` names the first tampered row. | `ledger.py` |
| Nothing goes to founders without a yes | Questions are bound to `sha256(run_id + exact questions)`. Approving writes them to `data/outbox/`. Nothing is sent automatically. | `diligence/memo.py` |
| Live data, labelled | `fake` and `replay` modes show a banner on every screen. | `api.py`, `static/*` |

## Live results (22 Nov 2026)

Two small decks about a real company (Razorpay), one accurate and one deliberately altered, run against live Crustdata:

| Deck | Claims | Verdicts | Credits | Model |
|---|---|---|---|---|
| Accurate | 10 | 10 confirmed. "3,000+ people" matched a record of 4,038. "Sequoia" matched Peak XV Partners (renamed). | 4.55, incl. 5 comparables | ₹2.22 |
| Altered | 8 | Contradicted, each with a dated source: team of 12,000 (record: 4,038), founded 2009 (2014), a Series H (rounds on record go to Series F), Accel and SoftBank as investors (not on record, and a web search found no page naming them investing). Tiger Global confirmed (Series A, 2015). "ex-Google" came back *can't verify*, because the founder's record lists only one past job. | 12.15 | ₹1.84 |

The first live run surfaced four problems. All four are fixed and covered in code:

1. `/company/identify` takes **one kind of identifier per call**, so Tally asks by domain, then by name.
2. Web search costs **2 credits** on this key, not the 1 in the docs. `PriceBook` now only ever *raises* a price from `/account/endpoints`, never lowers it.
3. Competitors come back as **domains with stray whitespace**, not ids. The schema layer normalises them.
4. Web evidence must **name both parties in the same sentence**. Otherwise an unrelated page can "confirm" an investor.

## Run it

Needs Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env          # CRUSTDATA_API_KEY, ANTHROPIC_API_KEY, TALLY_INR_PER_USD
uv run tally check            # first live calls (~2.2 credits): confirms response shapes. --full: ~5 more
uv run tally serve            # desk at http://localhost:8000, plain view at /simple, phone dial at /phone?run=<id>
```

Command line:

```bash
uv run tally diligence deck.pdf --founders "Asha Rao, Vik Mehta" --budget 150 --minutes 8 --out memo.md
uv run tally diligence deck.pdf --run-id <id>     # resume after a crash: nothing is bought twice
uv run tally diligence-eval sealed/answers.json   # run and score the sealed set
uv run tally verify                               # recompute the ledger's hash chain
uv run tally ledger <run-id>                      # print one run's ledger entries
uv run pytest                                     # 48 tests, all offline
```

**No keys?** Add `--mode fake` and use a deck from `tests/decks/`. These are made-up companies served by an in-process stand-in for Crustdata (`tally/crust/fake.py`). Without `ANTHROPIC_API_KEY`, the rules reader works alone.

`--out memo.md` also writes `memo.json` beside it, holding the full receipts.

## Architecture

### System view

Containers, the trust boundary around the deck, and the two external services. Solid arrows are calls; dotted arrows are callbacks and events.

```mermaid
flowchart TB
  subgraph Clients["Clients"]
    UI["Browser screens<br/>desk / · plain /simple · phone /phone"]
    CLI["CLI<br/>tally.cli"]
  end

  subgraph Control["Control plane · tally.api"]
    API["FastAPI handlers<br/>Desk registry of runs"]
    Hub["EventHub<br/>fan-out, 500-event replay buffer"]
    WA["WebApprover<br/>one asyncio.Future per binding"]
  end

  subgraph Orch["Orchestration · tally.diligence"]
    Run["DiligenceRun<br/>quote · deliver · top_up"]
    Reader["Deck readers<br/>model ∪ regex, instruction scanner"]
    Planner["DiligencePlanner<br/>greedy value per credit"]
    Judge["judge<br/>pure, one per ClaimKind"]
    Memo["Memo builder<br/>receipts, question binding"]
    DBuy["DeckBuyer<br/>typed probes"]
  end

  subgraph Spend["Spending engine · domain-agnostic"]
    Buyer["Buyer._buy<br/>idempotency key, 3 attempts"]
    Wallet["Wallet<br/>BudgetNode tree, purchase lane"]
    Prices["PriceBook<br/>worst case, raise-only"]
    Recovery["Recovery<br/>in_doubt reconciliation"]
  end

  subgraph Adapters["I/O adapters"]
    Client["CrustClient<br/>per-endpoint RPM, retries, record/replay"]
    Schema["crust/schema.py<br/>the only JSON parser"]
    Brain["Brain · llm.py<br/>the only model call"]
  end

  subgraph Store["Persistence · data/"]
    Ledger[("ledger.sqlite<br/>WAL, synchronous=FULL<br/>hash-chained entries")]
    Files[("decks/ · responses/ · outbox/")]
  end

  Crust[["Crustdata API"]]
  Claude[["Anthropic API<br/>claude-opus-5-5"]]

  UI -->|"REST"| API
  Hub -.->|"SSE"| UI
  CLI --> Run
  API --> Run
  API --> WA
  WA -.->|"approve / deny"| Wallet
  Run -.->|"emit"| Hub
  Run --> Reader --> Brain --> Claude
  Run --> Planner
  Run --> Judge
  Run --> Memo --> Files
  Run --> DBuy --> Buyer --> Wallet
  DBuy --> Schema
  Buyer --> Prices
  Wallet --> Ledger
  Wallet --> Client --> Crust
  Run --> Recovery --> Ledger
  Recovery --> Client
  Brain -->|"tokens, ₹"| Ledger
```

### Module dependency graph

Intra-package imports, generated from the `from tally.… import` lines. Arrows point from importer to imported. The graph is acyclic, and **no module in the spending engine imports `tally.diligence`**. The one place the two halves touch is `llm.py`, which imports the `DeckReading` schema lazily, inside `read_deck`.

```mermaid
flowchart TD
  subgraph Entry["Entry points"]
    cli["cli"]
    api["api"]
  end
  subgraph Dil["tally.diligence"]
    run["run"]
    memo["memo"]
    planner["planner"]
    dprobes["probes"]
    claims["claims"]
    deck["deck"]
    evidence["evidence"]
  end
  subgraph Eng["Spending engine"]
    session["session"]
    probes["probes · Buyer"]
    wallet["wallet"]
    recovery["recovery"]
    prices["prices"]
    ledger["ledger"]
  end
  subgraph Ad["Adapters"]
    llm["llm"]
    client["crust.client"]
    schema["crust.schema"]
    errors["crust.errors"]
    config["config"]
  end

  cli --> api & run
  api --> run & session & ledger
  run --> session & dprobes & planner & claims & memo & recovery
  memo --> claims & planner & ledger
  planner --> claims & prices
  claims --> deck & evidence
  dprobes --> probes & schema & wallet
  schema --> evidence
  session --> wallet & recovery & prices & llm & client
  probes --> wallet & client & prices
  wallet --> ledger & errors
  recovery --> client & ledger
  client --> ledger & errors & config
  llm --> ledger & config
  llm -.->|"lazy: DeckReading"| deck
```

Check the boundary yourself:

```bash
grep -nE "from tally\.diligence" tally/{wallet,ledger,probes,recovery,session,prices}.py tally/crust/client.py
# no output
```

### Layering

Each layer knows only the one below it:

| Layer | Knows about | Doesn't know about |
|---|---|---|
| `CrustClient` | HTTP, headers, rate limits, retries, `X-Credits-Used` | budgets, claims |
| `Wallet` + `Ledger` | credits, reservations, deadlines, approvals | what's being bought or why |
| `Buyer` / `DeckBuyer` | request shapes, worst-case prices | verdicts |
| `schema.py` | Crustdata's JSON (the *only* place it is parsed) | everything else |
| `claims.py` | evidence dataclasses → verdicts | HTTP, money |
| `DiligenceRun` | orchestration | JSON shapes |

The spending engine (`session.py`, `wallet.py`, `ledger.py`, `recovery.py`, `probes.py`) has no diligence code in it. It was built first for a different problem statement and reused as-is when the project switched to 2.2 (see [`docs/PREPARED_BEFORE_START.md`](docs/PREPARED_BEFORE_START.md)).

### Source map

| File | Lines | Responsibility |
|---|---:|---|
| `tally/diligence/run.py` | 383 | `DiligenceRun`: the two-phase state machine (quote → deliver), comparables, check loop, re-judging, memo assembly |
| `tally/diligence/claims.py` | 643 | One judge per `ClaimKind`; tolerances; name, school and place normalisation; alias tables |
| `tally/diligence/deck.py` | 391 | PDF loading (pypdf), regex claim reader, instruction scanner, model/rules merge, quote verification |
| `tally/diligence/planner.py` | 123 | Materiality and settle-chance priors; `next_action` greedy by value per credit; quote plan |
| `tally/diligence/probes.py` | 184 | Identify, enrich, jobs, founder, web, comparables; lookalike-domain detection |
| `tally/diligence/memo.py` | 152 | Memo rendering, receipts, question templates, approval binding, outbox |
| `tally/diligence/evaluate.py` | 79 | Sealed-set scorer |
| `tally/diligence/evidence.py` | 122 | Typed evidence: `CompanyProfile`, `PersonRecord`, `JobPosts`, `WebFinding`, `Comparable` |
| `tally/wallet.py` | 271 | `BudgetNode` tree, `Wallet.buy`, purchase lane, cut/extend/kill, approval requests |
| `tally/ledger.py` | 346 | Append-only SQLite ledger, hash chain, state folding, saved responses |
| `tally/recovery.py` | 84 | Crash reconciliation and balance check |
| `tally/session.py` | 109 | Shared run lifecycle: resume, limits, approval threshold, key prices |
| `tally/probes.py` | 59 | `Buyer._buy`: the single place a purchase happens |
| `tally/prices.py` | 79 | Worst-case `PriceBook`, raised from `/account/endpoints` |
| `tally/crust/client.py` | 192 | Async httpx client, sliding-window rate limiter, retry policy, record/replay |
| `tally/crust/schema.py` | 256 | Crustdata JSON → evidence dataclasses |
| `tally/crust/fake.py` | 461 | Offline Crustdata stand-in for tests and `--mode fake` |
| `tally/llm.py` | 74 | The only model call |
| `tally/api.py`, `events.py` | 333 | FastAPI app, SSE event hub, web approver |
| `tally/static/*.html` | | Desk (`/`), plain view (`/simple`), phone dial (`/phone`) |

## The pipeline, step by step

A run has two phases, so a partner can see the price before committing to it.

### End-to-end sequence

One run through the web desk, happy path. Every paid call follows the same reserve → perform → settle protocol, shown in detail under [The purchase protocol](#the-purchase-protocol-walletbuy).

```mermaid
sequenceDiagram
  autonumber
  actor P as Partner
  participant UI as Desk (browser)
  participant API as FastAPI
  participant R as DiligenceRun
  participant B as Brain (Claude Opus)
  participant W as Wallet
  participant L as Ledger
  participant C as Crustdata

  P->>UI: deck, founders, budget, minutes
  UI->>API: POST /api/memos
  API->>R: make_memo, then quote() as a background task
  API-->>UI: run_id
  UI->>API: GET /api/events?run_id (SSE, replays history)

  rect rgba(120,120,120,0.08)
  Note over R,C: Phase 1 · quote()
  R->>C: GET /account/endpoints, /account/credits (free)
  R->>L: update_run(starting_balance)
  R->>B: messages.parse(PDF document + text layer)
  B-->>R: DeckReading (claims, reader_instructions)
  B->>L: model entry (tokens, ₹)
  R->>R: regex reader, merge ∪, checkable(), find_instructions()
  R->>L: update_run(deck_reading)
  R->>W: identify by domain, then by name (0.2 cr each)
  W->>C: POST /company/identify
  R->>R: domain_check (lookalike?)
  R->>W: company enrich (2 cr)
  W->>C: POST /company/enrich
  R->>R: judge every claim, carve comparables, coverage curve
  R-->>UI: SSE quote
  end

  P->>UI: Accept
  UI->>API: POST /api/jobs/{id}/accept
  API->>R: deliver() as a background task

  rect rgba(120,120,120,0.08)
  Note over R,C: Phase 2 · deliver()
  R->>W: comparables (spends from the carved node)
  loop while planner.next_action() returns a check
    R->>W: buy(check) at worst-case price
    W->>L: reserved
    W->>C: POST
    C-->>W: body, X-Credits-Used, request id
    W->>L: settled + response body (one transaction)
    R->>R: re-judge every claim
    R-->>UI: SSE claim (only verdicts that changed)
  end
  R->>C: GET /account/credits
  R->>L: update_run(done, questions, questions_binding)
  R-->>UI: SSE done (ledger credits vs balance drop)
  end

  P->>UI: approve questions
  UI->>API: POST /api/memos/{id}/questions (binding, approve)
  API->>L: message entry (decision, bound to the exact set)
  API->>API: write data/outbox/run_id.md
```

### Run lifecycle

`DiligenceRun` as a state machine. Every exit path goes through **Memo**, so a stopped run still produces an honest verdict for every claim, *not checked at this budget* included.

```mermaid
stateDiagram-v2
  [*] --> Accounting: new or resumed run_id
  Accounting --> Reading: prices and RPM loaded, in_doubt reconciled
  Reading --> Identifying: at least one checkable claim
  Reading --> Memo: no checkable claims
  Identifying --> Quoted: record bought, claims judged, comparables carved
  Identifying --> Memo: no record, or BudgetRefused
  Quoted --> Comparables: partner accepts
  Comparables --> CheckLoop
  CheckLoop --> CheckLoop: buy, then re-judge all
  CheckLoop --> Memo: nothing worth buying, killed, deadline, 402
  Memo --> CheckLoop: top_up raises limit and deadline
  Memo --> QuestionsPending
  QuestionsPending --> Outbox: binding matches, approved
  QuestionsPending --> Closed: denied
  Outbox --> [*]
  Closed --> [*]
```

### Phase 1: `quote()`

1. **Account for the past.** Load this key's prices and rate limits from `/account/endpoints` (free). A new run records the starting credit balance. A resumed run reconciles anything a crash left in doubt (see [Crash recovery](#crash-recovery)).
2. **Read the deck.** If the reading is already saved on the run, it is reused and nothing is paid twice. Otherwise:
   - **Model reader:** Claude Opus receives the PDF as a `document` block plus the text layer. It returns a `DeckReading` through structured output (`messages.parse`, `output_format=DeckReading`, effort `medium`, server-side fallbacks on). Tokens and ₹ cost go on the ledger. An API error, a refusal or `max_tokens` returns `None`, and the rules reader stands alone.
   - **Rules reader:** regexes over the text layer for headcount, hiring, money (₹/$/crore/lakh/mn/bn), rounds, investors, founding year, founders' employers and schools, customers and the domain.
   - **Merge:** model claims ∪ rules claims the model missed (deduplicated by `claim_key`).
   - **Filter:** `checkable()` drops forward-looking statements ("we will…") and names that aren't names. Each dropped claim is logged.
   - **Scan:** `find_instructions()` flags reader-directed text. It is logged and otherwise ignored.
3. **Identify the company**, by domain and by name, at 0.2 credits each. `domain_check` compares the two results (see [Lookalike domains](#lookalike-domains)).
4. **Buy the company record.** One Company Enrich call (2 credits) returns headcount with history, funding rounds with investors and dates, founders and leaders with work history and schools, competitors, locations, revenue band and open roles. Most claims settle here.
5. **Judge every claim** against that record. Some verdicts are marked `provisional`: one more check could still change them.
6. **Carve the comparables sub-budget**: `min(8, 25% of budget, available)`. It is a child node in the budget tree, so comparables can't spend into claim checks.
7. **Quote.** The planner's ordered plan is walked cumulatively to build the coverage curve `[(credits, claims settled)]`. The quote reports claims settled at this budget, the full-check cost, and the cost and price in ₹.

### Phase 2: `deliver()`

8. **Comparables.** The company's own competitors on record come first, matched exactly by id or domain, and tiny SEO competitors (under a tenth of the company's size) are dropped. If fewer than 3 remain, companies in the same industry, country and size band, each with a funding record, fill the rest, up to 5.
9. **Check loop.** Repeat until nothing is worth buying. Each outcome is handled explicitly:

```mermaid
flowchart TD
  NA["planner.next_action(claims, evidence, root.available())"]
  NA -->|"None"| Done["stop: nothing left worth checking / budget used"]
  NA -->|"Check"| Do["_do(check) through DeckBuyer"]
  Do -->|"ok"| Mark["planner.done += key"]
  Do -->|"BudgetRefused: killed, deadline"| Stop["stop with that reason"]
  Do -->|"BudgetRefused: denied"| Block["planner.blocked += key<br/>never asked again"]
  Do -->|"BudgetRefused: over_budget after a cut"| Re
  Do -->|"SourceTimeout after 3 attempts"| Held["claims marked HELD<br/>planner.done += key"]
  Do -->|"OutOfCredits 402"| Out["stop: Crustdata credits ran out"]
  Do -->|"403 / 400 / other CrustError"| Block
  Mark --> Re["re-judge every claim<br/>emit SSE for changed verdicts"]
  Block --> Re
  Held --> Re
  Re --> NA
  Done & Stop & Out --> Memo["_finish_memo(stop_reason)"]
```

   One job-post purchase can change both a headcount and a hiring verdict, which is why **every** claim is re-judged after **every** purchase.

10. **Memo.** Results are sorted by verdict severity, then materiality, then deck order. When several claims share a purchase, its cost is split evenly between them. Questions are generated and bound. Ledger credits are compared with the account's balance drop.

**Top-up** after the memo raises the root limit and extends the deadline, then calls `deliver()` again. The planner's `done` set and the ledger's saved responses mean only new checks are bought.

## The spending engine

### Budget tree

```mermaid
flowchart TB
  root["client · limit 150<br/>spent + reserved by claim checks"]
  comps["comparables · limit ≤ 8<br/>carved at quote time"]
  root -->|"carve / grant / reclaim<br/>(parent only)"| comps
```

```python
available(node) = limit − spent − reserved − Σ child.limit
committed(node) = spent + reserved + Σ child.committed()
```

- `carve(id, amount)` creates a child out of the parent's `available()`. A child can't raise its own limit. Only `grant` from its parent can.
- `cut(new_limit)` resizes the tree top-down. Each child keeps at least what it has committed. The remaining room is shared out in proportion to each child's unused headroom. A limit below what's already committed is allowed: nothing new can be bought until it rises.
- A **kill** flag and a **deadline** (epoch seconds, persisted) are checked on every reservation.

Worked example of a cut. The root has spent 12; the comparables node has limit 8 and has committed 3.

| Operation | Root room `limit − spent − reserved` | Comparables limit | Root `available()` |
|---|---:|---:|---:|
| start (limit 150) | 138 | 8 | 130 |
| `cut(30)` | 18 | 8 (fits, unchanged) | 10 |
| `cut(15)` | 3 | 3 (floored at its committed) | 0, so no new purchase |

### The purchase protocol (`Wallet.buy`)

```mermaid
sequenceDiagram
  autonumber
  participant Bu as Buyer._buy
  participant W as Wallet.buy
  participant A as Approver
  participant Lane as Purchase lane (asyncio.Lock)
  participant L as Ledger (SQLite)
  participant C as CrustClient

  Bu->>Bu: key = sha256(run, endpoint, canonical body, attempt)
  Bu->>W: buy(node, key, endpoint, est_max, reason)
  W->>L: saved_response(key)
  alt already settled
    L-->>W: stored body
    W->>L: cached entry, 0 credits
    W-->>Bu: Purchase(from_ledger=True)
  else not bought yet
    opt est_max above approve_over
      W->>A: decide(ApprovalRequest), binding = sha256(key, credits)
      A-->>W: yes, or no and BudgetRefused(denied)
    end
    W->>Lane: acquire
    W->>W: check killed, deadline, est_max within node.available()
    W->>L: reserved entry at est_max, committed before the request
    W->>C: perform()
    alt 2xx
      C-->>W: body, X-Credits-Used, request id
      W->>L: BEGIN IMMEDIATE, response body + settled, COMMIT
    else HTTP error (CrustError)
      W->>L: failed entry, credits from header (normally 0)
    else timeout or no reply
      W->>L: in_doubt entry, counted at est_max
    end
    W->>Lane: release
  end
  Note over Bu: on SourceTimeout, retry with attempt + 1 (a fresh key), at most 3
```

- **Worst-case pricing.** `PriceBook` holds an upper bound for every endpoint, including premium field groups, which are billed twice (once to filter, once to return). Prices loaded from the key's endpoint list can only raise these bounds.
- **Approval threshold.** A single purchase above `max(25% of budget, 20 credits)` needs a person's yes. Setting the budget approves everything below that. With no one watching, the default is `DenyAll`. Approval waits happen **outside** the lane, so a pending approval never holds the lock.
- **One lane.** Paid calls are serialised. When a cut, kill or deadline fires, at most the one call already in flight settles.
- **Retries.** `Buyer._buy` tries up to 3 times. Each attempt is a *new* call key, so an in-doubt attempt stays on the ledger for reconciliation and is never overwritten.

### The ledger

SQLite in WAL mode with `synchronous=FULL`, so a committed entry survives a hard kill. Three tables:

```mermaid
erDiagram
  runs ||--o{ entries : "run_id"
  entries }o--o| responses : "call_key, written with settled"
  runs {
    TEXT id PK "run-YYYYmmdd-HHMMSS-xxxxxx"
    TEXT client_id
    TEXT job_text
    REAL credit_limit "persisted on every cut or top-up"
    REAL deadline_at "epoch seconds"
    TEXT mode "live, record, replay, fake"
    TEXT status "running, done"
    REAL created_at "start of the usage-event window"
    TEXT details "JSON: quote, deck_reading, questions, binding, starting_balance"
  }
  entries {
    INTEGER seq PK "AUTOINCREMENT, chain order"
    TEXT run_id
    TEXT call_key "idx (call_key, seq)"
    TEXT kind "crustdata, model, payment, message"
    TEXT event "reserved, settled, failed, in_doubt, resolved, cached"
    TEXT budget_id
    TEXT endpoint
    TEXT reason
    REAL credits
    TEXT model
    INTEGER tokens_in
    INTEGER tokens_out
    REAL cost_inr
    TEXT request_id "vendor event id, used by reconciliation"
    TEXT note
    REAL at
    TEXT prev_hash
    TEXT row_hash "sha256(prev_hash + canonical 15 columns)"
  }
  responses {
    TEXT call_key PK
    TEXT body "canonical JSON"
  }
```

A call's state is a **fold** over its entries, oldest first. Each call moves through this state machine, and the `charged` column is what the call counts against the run:

```mermaid
stateDiagram-v2
  direction LR
  [*] --> reserved: reserve(est_max), before the request
  reserved --> settled: 2xx
  reserved --> failed: HTTP error
  reserved --> in_doubt: timeout, or found open on restart
  in_doubt --> resolved: usage event matched
  in_doubt --> resolved: no usage event, 0 credits
  settled --> [*]
  failed --> [*]
  resolved --> [*]
```

| Latest event | `charged` | Notes |
|---|---|---|
| `reserved` | 0 in the ledger | still in flight; the wallet holds `est_max` as `node.reserved` |
| `settled` | `X-Credits-Used` | response body stored in the same transaction |
| `failed` | header credits, normally 0 | error responses are free |
| `in_doubt` | `est_max` | rule 3: when unsure, count it |
| `resolved` | the real amount, or 0 | from Crustdata's usage records |
| `cached` | 0 | its own key (`<key>:cache:<ns>`), an audit trail of reuse |

Rule 3, "when unsure, count it", means the ledger may overcount for a moment but never misses a charge.

**Hash chain.** Every entry stores `prev_hash` and `row_hash = sha256(prev_hash + canonical_json(15 hashed columns))`. Model calls and the partner's decision on the questions are entries in the same chain, so one audit covers data spend, model spend and the human decision.

```mermaid
flowchart LR
  G["GENESIS<br/>0 × 64"] --> E1["seq 1 · crustdata<br/>reserved"]
  E1 --> E2["seq 2 · crustdata<br/>settled"]
  E2 --> E3["seq 3 · model<br/>settled, tokens, ₹"]
  E3 --> En["…"]
  En --> Eq["seq n · message<br/>questions approved"]
```

Each arrow is `row_hash(k) = sha256(row_hash(k−1) ‖ canonical_json(row k))`.

`verify_chain()` recomputes the chain in one O(n) pass and returns the first bad `seq`. An edited, deleted or inserted row breaks every hash after it, so the first mismatch locates the tampering. (The chain proves integrity, not authorship: someone with write access could rebuild the whole chain. Pinning the head hash outside the database, for example in the memo, closes that gap.)

### Crash recovery

On restart with the same `run_id`:

```mermaid
sequenceDiagram
  autonumber
  participant P as New process (same run_id)
  participant S as Session
  participant L as Ledger
  participant C as Crustdata

  Note over P,L: the old process died mid-call, leaving an entry at "reserved"
  P->>S: DiligenceRun(run_id)
  S->>L: run(run_id): credit_limit, deadline_at, created_at
  S->>L: open_reservations(), mark_in_doubt on each
  S->>C: GET /account/endpoints (prices, RPM)
  S->>C: GET /account/usage/events since created_at (free)
  C-->>S: charged events with event_id
  S->>S: unclaimed = events not on the ledger, with credits > 0
  loop each in_doubt call
    alt an unclaimed event on the same endpoint
      S->>L: resolve(real credits, request_id = event_id)
    else none
      S->>L: resolve(0, never charged)
    end
  end
  S->>L: details.deck_reading, so the model is not called again
  Note over S,L: deterministic requests: every settled key is a cache hit at 0 credits
  S->>C: GET /account/credits at the end
  S->>S: ledger total vs (starting balance minus now), within 0.005
```

1. The original limits and deadline are reloaded from `runs`.
2. Every open `reserved` entry for the run becomes `in_doubt`.
3. `GET /account/usage/events` (free) since the run started is fetched. Charges whose `event_id` isn't already on the ledger are matched to in-doubt calls by endpoint and resolved at the real amount. An unmatched call resolves at 0.
4. The deck bytes (`data/decks/<run_id>`) and the saved `deck_reading` are reused, so the model isn't called again.
5. Every request is deterministic, so every purchase that already settled is a ledger hit at 0 credits.
6. At the end, `credits_charged(run)` is compared with `starting_balance − current balance`. They must match within 0.005 credits. This assumes no one else spends on the same key during the run.

### Crustdata client

- Sliding-window rate limiter **per endpoint**, with requests-per-minute loaded from `/account/endpoints` (defaults: enrich 15, search/identify 30, web 10; `/account/*` 60 shared).
- `429` is retried up to 5 times and `5xx` up to 2 times. Neither is charged. Waits are capped at 60 s.
- A timeout or network failure raises `SourceTimeout`, which is deliberately *not* a `CrustError`. With no response there's no way to know whether the call was charged, so the wallet keeps it in doubt.
- Errors map to typed exceptions, each telling the caller what to do: `BadKey` (401), `OutOfCredits` (402), `EndpointForbidden` (403), `BadRequest` (400), `RateLimited`.
- `record` mode saves each response. `replay` serves the saved ones for free and shows a REPLAY banner. Replay is insurance for the demo, not a data source.

## The verdict engine

`judge(claim, index, evidence) → Judgement(verdict, reason, sources, provisional)`. There is one function per `ClaimKind`, and each is pure: it takes the evidence bought so far and returns a verdict. Because judging is pure and cheap, the run re-judges every claim after every purchase instead of tracking dependencies between claims and evidence.

The judges differ in their fields and thresholds, but they share this shape:

```mermaid
flowchart TD
  In["claim + evidence bought so far"] --> Q1{"has the source that<br/>could settle it been bought?"}
  Q1 -->|"no"| NC["NOT_CHECKED · provisional"]
  Q1 -->|"yes"| Q2{"does the record<br/>carry the field?"}
  Q2 -->|"no"| CV["CANT_VERIFY<br/>absence is never a contradiction"]
  Q2 -->|"yes"| Q3{"do two dated<br/>records disagree?"}
  Q3 -->|"yes"| RD["RECORDS_DISAGREE<br/>both shown with dates"]
  Q3 -->|"no"| Q4{"inside the<br/>confirm tolerance?"}
  Q4 -->|"yes"| OK["CONFIRMED"]
  Q4 -->|"no"| Q5{"past the<br/>contradict threshold?"}
  Q5 -->|"no, grey zone"| CV
  Q5 -->|"yes"| CT["CONTRADICTED"]
  CT --> Q6{"could an unbought<br/>check overturn it?"}
  Q6 -->|"yes"| PV["provisional<br/>planner doubles the web check's value"]
  Q6 -->|"no"| Final["final"]
```

### Claim kinds, with planner priors

| Kind | Materiality | Settled by |
|---|---:|---|
| investor | 1.0 | company record (company + per-round investors); web search |
| headcount | 1.0 | company record; job posts (newer team size) |
| funding_total | 0.9 | company record; web |
| last_round | 0.8 | company record rounds; web |
| founder_past_employer · founder_role | 0.8 | company record people; founder's own record |
| revenue · customer | 0.6 | web only (customer needs a named subject) |
| founder_education · hiring · headcount_growth | 0.5 | record / founder record / job posts / headcount series |
| partner_or_award · founded | 0.4 | web / record |
| location | 0.3 | HQ, addresses, headcount-by-region |
| other_metric | 0.2 | web, only when the claim has a specific number |

### Tolerances

Decks round up more often than they lie, so each threshold is a named constant in `claims.py`:

| Claim | Confirmed | Contradicted | In between |
|---|---|---|---|
| Headcount (exact) | `0.6·rec ≤ n ≤ 1.25·rec + 5` | `n ≥ 1.5·rec` **and** `n − rec ≥ 10` | can't verify |
| Headcount ("60+") | `rec ≥ n/1.25 − 5` | as above | can't verify |
| Funding total | `\|n − rec\| ≤ 20%` | `n ≥ 1.6·rec` | can't verify; a deck claiming *less* is never contradicted (it may count one round) |
| Growth (12 mo) | within 35% of the claimed growth (+3 pts) | actual < half the claimed growth | can't verify |
| Founded | ±1 year | beyond | |
| Hiring | open posts ≥ 50% of the claimed hires | | |
| Past employer | listed | missing **and** the person's record lists ≥ 2 past jobs | can't verify (thin history proves nothing) |

The asymmetry is deliberate: the contradict thresholds sit well past the confirm band, and the gap between them is *can't verify*. A false contradiction costs the fund a founder relationship. A false *can't verify* costs one question.

### Rules that prevent false accusations

- **A missing record is never a contradiction.** Only a record that exists and says otherwise can contradict a claim.
- **Provisional contradictions get one more look.** A contradiction that a not-yet-bought check could overturn is marked `provisional`. The planner **doubles** the value of a web search that could overturn it, because calling a founder wrong deserves the extra credit.
- **Records disagreeing is its own verdict.** A headcount record from weeks ago vs. a newer job post that states a team size gives *records disagree*, with both dates shown. The deck isn't called wrong.
- **Web evidence must co-locate.** For funding claims, the investor's name, the company's name and a funding word (*invest, led, round, raise, backed, seed, series…*) must appear in the same sentence of a result. For a figure, the number must be stated there too.
- **Name matching** normalises Unicode, strips suffixes such as "Ventures", "Capital" and "Partners", and uses alias tables: Sequoia India ↔ Peak XV, Google ↔ Alphabet/DeepMind, Facebook ↔ Meta, Bangalore ↔ Bengaluru.
- **People are matched by company id, never by name alone.** Founders with common names don't collide.

### Planner

```
value(check) = Σ over claims it could change of
               materiality(kind) × P(settles | check) × (1 − settled(claim))
pick           argmax value / worst_case_cost,  subject to cost ≤ available,  value/cost ≥ 0.02
```

`settled(claim)` is 1.0 for confirmed or contradicted, 0.9 for records disagree, 0.6 for can't verify, 0.5 for held and 0 for not checked, halved when the verdict is provisional. The candidate checks are job posts (20 results), one founder's own record (enrich by profile URL, else a search scoped to the company), and one web search per claim that a search could settle. At a small budget this greedy order is the triage: investors and headcount get checked before logos, and the rest honestly say *not checked at this budget*.

Each loop iteration costs O(claims) to enumerate options and O(claims) to re-judge, with fewer than 20 candidate checks per deck. All of that is negligible next to one HTTP round trip.

## Deck security

### Prompt injection

The deck is treated as hostile input. The diagram shows the trust boundary: the only thing that crosses from the model to the rest of the system is a typed, validated data structure.

```mermaid
flowchart LR
  subgraph U["UNTRUSTED · written by the company under review"]
    Deck["deck.pdf<br/>text layer + images"]
  end

  subgraph SB["Model sandbox · no tools, no budget, no verdicts"]
    LLM["Claude Opus<br/>structured output only"]
  end

  Regex["Rules reader<br/>deterministic regex"]
  Scan["find_instructions()<br/>INSTRUCTION_PATTERNS"]
  Merge["merge ∪<br/>checkable() · quote_in_text()"]

  subgraph T["TRUSTED · deterministic code"]
    Planner["Planner"]
    Wallet["Wallet"]
    Judge["judge()"]
    Tmpl["Question templates"]
  end

  Flags["memo flags + run log"]

  Deck --> LLM -->|"DeckReading (pydantic)"| Merge
  Deck --> Regex --> Merge
  Deck --> Scan -->|"reported, never executed"| Flags
  LLM -->|"reader_instructions"| Flags
  Merge -->|"list of DeckClaim"| Planner & Judge
  Planner --> Wallet
  Judge --> Tmpl
```

1. **The prompt** tells the model the deck is untrusted. Text addressed to reviewers gets copied into `reader_instructions`, never followed.
2. **A regex scanner** (`INSTRUCTION_PATTERNS`) runs on the text layer whatever the model does. It catches "ignore previous instructions", "AI reviewers … rate/score/approve", "do not verify", "mark all claims as confirmed", "system prompt" and similar.
3. **Architecture.** Even if both layers miss an injection, it has nothing to act on. The model has no tools, no budget access and no say over verdicts. Question text comes from fixed templates, so nothing the deck says can write a question.

The worst a successful injection can do is shape the *claim list*: drop a claim (the regex reader still finds the common kinds) or invent one (it is then checked against records like any other, and `quote_in_text` reports that its wording isn't in the deck).

Instructions that are found show up on the memo and in the run log. `tests/decks/ledgerline_injected.txt` covers this.

### Lookalike domains

The company is identified **both** by the deck's domain and by its name. `domain_check`:

- If the two resolve to **different companies**, the check runs against the company found by name, and the memo flags it.
- If the name resolves but the domain doesn't, `looks_alike()` tests for a homoglyph fold (`0→o`, `1→l`, `rn→m`, …), an edit distance ≤ 2 after removing hyphens, or the same label with a different TLD (`paysetu.co` vs `paysetu.in`).

Either case adds a question for the founder ("Which domain is yours?"). The test decks are `kredbloom_lookalike.txt` and `kredbloom_altered.txt`.

## System invariants

The properties the design rests on, where each is enforced and what tests it.

| # | Invariant | Enforced in | Tested by |
|---|---|---|---|
| I1 | For every budget node, `spent + reserved + Σ child.limit ≤ limit` at reservation time (a later cut may push `limit` below `committed`; then nothing new is admitted) | `Wallet._check_can_spend`, `BudgetNode.carve/grant` | Hypothesis property test `test_the_budget_tree_never_lets_a_node_overspend` |
| I2 | No paid request leaves the process without a durable `reserved` entry committed first | `Wallet.buy` (reserve, then `perform()`) | `test_wallet`, `test_recovery` |
| I3 | A given `(run, endpoint, body, attempt)` is paid for at most once | `call_key` + `saved_response` | `test_ledger` idempotency, `test_recovery` "restart after a finished memo buys nothing" |
| I4 | A charge is never under-counted: an unknown outcome counts at its worst-case price until reconciled | `_fold` (`in_doubt → reserved`), `_record_failure` | `test_wallet` "timeout stays in doubt and counts" |
| I5 | After reconciliation, ledger credits equal the account's balance drop within 0.005 | `recovery.check_balance` | `test_recovery` (exact per-endpoint recovery) |
| I6 | Any edit, insert or delete in `entries` is detected | `verify_chain` | `test_ledger` "hash chain catches an edited entry" |
| I7 | Every verdict is a pure function of `(claim, evidence)`; no model output reaches `judge` except the claim itself | `claims.judge`, `llm.py` returns `DeckReading` only | `test_diligence` "injection inert", tolerance tests |
| I8 | A missing record never yields `CONTRADICTED` | each judge | `test_diligence` "missing record ≠ contradiction" |
| I9 | Founder questions reach the outbox only with a decision bound to their exact text | `questions_binding`, `approve_questions` (409 on mismatch) | `test_diligence` binding test, `test_api` |
| I10 | After a cut, kill or deadline, at most one already-in-flight call settles | single purchase lane per wallet | `test_wallet` "a cut stops spending within one call" |

## Concurrency and consistency model

| Concern | Mechanism |
|---|---|
| Execution model | One asyncio event loop per process. Each run executes as a background task (`Desk.in_background`); failures are surfaced as SSE `error` events. |
| Paid calls within a run | Serialised through `Wallet._lane` (`asyncio.Lock`). The admission check, the reservation and the ledger write happen under the lock, so check-then-reserve can't race. |
| Ledger writes | `threading.Lock` around every append, plus `BEGIN IMMEDIATE` so the chain head is read and extended in one write transaction. `settle` writes the response body and the `settled` entry atomically. |
| Durability | WAL + `synchronous=FULL`: a returned `COMMIT` is on disk. A `reserved` entry is therefore durable before the request is sent. |
| Controls (cut, kill, deadline) | Plain state on the wallet, read under the lane on every reservation. They act between purchases, never mid-request. Cuts and deadline extensions are persisted to `runs` so a restart keeps them. |
| Approvals | `WebApprover` parks an `asyncio.Future` keyed by `sha256(call_key, credits)`. The wait happens before the lane is taken. An answer for a different binding is rejected (404). |
| Live events | `EventHub.publish` is synchronous and non-blocking (`put_nowait` into unbounded per-subscriber queues), so the wallet can call it from inside the lane. A 500-event buffer lets late subscribers catch up. |
| Consistency with the vendor | The ledger is the source of truth for intent; Crustdata's usage records are the source of truth for charges. They are reconciled at resume (`settle_in_doubt`) and compared at the end (`check_balance`). |

```mermaid
flowchart LR
  W["Wallet.on_change<br/>reserved, settled, failed, cut, kill, approval_needed"] --> Pub
  R["DiligenceRun.emit<br/>deck, quote, claim, flag, note, done"] --> Pub
  WA["WebApprover<br/>approval"] --> Pub
  APIq["questions handler<br/>questions"] --> Pub
  Pub["EventHub.publish<br/>sync, put_nowait"] --> Ring[("replay buffer<br/>last 500")]
  Pub --> Q1["subscriber queue"] --> S1["SSE · desk"]
  Pub --> Q2["subscriber queue"] --> S2["SSE · phone"]
  Ring -.->|"on connect"| S1 & S2
```

## Failure modes

| Failure | Detected by | Behaviour | Effect on money |
|---|---|---|---|
| Process killed mid-request | `reserved` with no later entry | restart marks it `in_doubt`, reconciles against usage records | exact after reconciliation |
| Request timeout / connection drop | `SourceTimeout` | `in_doubt`; retried with a fresh key, up to 3 attempts; then claims → **Held** | overcounts until reconciled, never under |
| `429` rate limited | client | backoff, up to 5 retries, waits ≤ 60 s | free |
| `5xx` | client | up to 2 retries | free |
| `402` out of credits | `OutOfCredits` | check loop stops; memo with remaining claims *not checked* | none beyond what settled |
| `403` / `400` | `EndpointForbidden` / `BadRequest` | that check is blocked for the rest of the run | free |
| Model API error, refusal or `max_tokens` | `Brain.read_deck` returns `None` | rules reader stands alone | model cost still recorded if tokens were used |
| `/account/endpoints` unavailable | exception in `_load_key_limits` | default (documented) prices and RPM kept; run continues | worst-case bounds still apply |
| Deck carries an injection | prompt + `find_instructions` | flagged on the memo; no effect on verdicts or spend | none |
| Deck domain is a lookalike | `domain_check`, `looks_alike` | checked against the company found by name; flagged; founder question added | none |
| Partner cuts below spend | `_set_limit` | allowed; no new purchases until raised | at most the in-flight call settles |
| Ledger edited on disk | `verify_chain` | audit panel shows the first bad `seq` | n/a (detection) |
| Server restarted mid-run | `POST /api/jobs/{id}/resume` | rebuilds the run from the ledger and saved deck, re-quotes for free, continues | 0 for everything already settled |

## HTTP API

`uv run tally serve` → FastAPI on `:8000`. All live progress arrives over Server-Sent Events.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/`, `/simple`, `/phone` | Desk, plain view, phone dial |
| `GET` | `/api/info` | Mode (for the banner) and the LAN URL for phones |
| `POST` | `/api/memos` | `{deck_b64, filename, founders[], domain?, budget=150, minutes=8}` → `{run_id}`. Starts phase 1. |
| `POST` | `/api/jobs/{id}/accept` | Accept the quote and start phase 2 |
| `GET` | `/api/memos/{id}` | The finished memo with receipts and audit (409 until done) |
| `POST` | `/api/memos/{id}/questions` | `{binding, approve}`. 409 if the binding doesn't match the waiting set. |
| `POST` | `/api/jobs/{id}/cut` | `{limit}` or `{factor}` (e.g. 0.5 halves what's *left*) |
| `POST` | `/api/jobs/{id}/topup` | `{credits}` after the memo; continues the checks |
| `POST` | `/api/jobs/{id}/kill` | No new purchases |
| `POST` | `/api/jobs/{id}/approvals/{binding}` | Answer a large-purchase approval |
| `POST` | `/api/jobs/{id}/resume` | Continue a run after the server was killed |
| `GET` | `/api/jobs/{id}/state` | Limit, spent, seconds left, killed, budget tree |
| `GET` | `/api/jobs/{id}/audit` | Chain check, ledger vs. balance, quote, last 200 entries |
| `GET` | `/api/events?run_id=` | SSE: `deck · quote · claim · reserved · settled · failed · cut · kill · approval_needed · flag · note · done · questions · error` |

The event hub keeps the last 500 events, so a page opened mid-run catches up.

## Configuration and modes

`.env` (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `CRUSTDATA_API_KEY` | | Required for `live` and `record` |
| `ANTHROPIC_API_KEY` | | Optional. Without it, the rules reader works alone. |
| `TALLY_INR_PER_USD` | | Needed for ₹ quotes and to compare INR amounts in decks |
| `TALLY_MODE` | `live` | `live · record · replay · fake` |
| `TALLY_DATA_DIR` | `data` | Ledger, saved responses, decks, outbox |
| `TALLY_USD_PER_CREDIT` | `0.30` | The event values 15,000 credits at $4,500 |

Constants in code: `PRICE_MULTIPLE = 3.0` and the model `claude-opus-5-5` (`config.py`), `APPROVAL_SHARE = 0.25` and `MIN_APPROVAL_CREDITS = 20` (`session.py`), `COMPARABLES_SHARE = 0.25` and `COMPARABLES_MAX = 8` (`diligence/run.py`), `MAX_ATTEMPTS = 3` (`probes.py`), `MAX_QUESTIONS = 8` (`diligence/memo.py`).

| Mode | Crustdata | Banner | Use |
|---|---|---|---|
| `live` | real API | none | real memos |
| `record` | real API, every response saved | none | live runs you may want to replay |
| `replay` | saved responses, 0 credits | **REPLAY** | demo insurance |
| `fake` | in-process stand-in, made-up companies | **FAKE** | tests, trying it without keys |

On-disk layout under `data/`: `ledger.sqlite` (+WAL), `responses/` (record/replay), `decks/<run_id>`, `outbox/<run_id>.md`.

## Testing and evaluation

```bash
uv run pytest      # 48 tests, offline, ~30 s
```

The whole suite runs offline. `FakeCrustdata` is mounted as an `httpx` transport, so tests exercise the real `CrustClient`, rate limiter, retry policy, wallet and ledger, with only the network replaced.

| Suite | Covers |
|---|---|
| `test_wallet.py` | buy-once, over-limit refusal, a cut stops spending within one call, deadline, kill, large purchase waits for a person, timeout stays in doubt and counts, error responses are free; a Hypothesis property test that no budget-tree node ever overspends |
| `test_ledger.py` | settle and save, idempotent keys (and fresh keys for retries), unfinished calls counted at reserved amount, model calls on the ledger, hash chain catches an edited entry |
| `test_recovery.py` | a crash after Crustdata charged is recovered exactly (per endpoint); a restart after a finished memo buys nothing |
| `test_client.py` | cost and request id on every call, 429/500 retried and not charged, 403 and timeouts raised, record → replay for free, live mode needs a key |
| `test_diligence.py` | rules reader, instruction detection, headcount tolerances (parametrised), missing record ≠ contradiction, name/school matching, altered and accurate decks end to end, injection inert, lookalike flagged, records disagree, small budget + top-up, crash mid-memo, question approval binding, sealed-set scorer, forward-looking claims dropped, web figures, growth, locations |
| `test_api.py` | the HTTP flow end to end: memo → accept → questions |

Test decks (`tests/decks/`, all fictional): `ledgerline_accurate`, `ledgerline_injected`, `kredbloom_altered`, `kredbloom_lookalike`, `tinyledger_disagree`.

**Sealed set.** A teammate, not the builder, wrote a set of decks with an answer file. Only `sealed.sha256` is committed. `tally diligence-eval sealed/answers.json` runs each deck and scores the result against the 2.2 criteria: every altered claim comes back contradicted with a source, no other claim is contradicted, lookalike domains are flagged, and hidden instructions are flagged without changing any verdict.

## Business case

| | |
|---|---|
| **Buyer** | Diligence teams at VC funds and advisory firms. Today an analyst spends 1–2 days per deck. |
| **Unit of sale** | One memo per deck, delivered before the second meeting |
| **Cost to serve** | ~5–15 Crustdata credits (~$1.50–$4.50 at $0.30/credit) + ~₹2 of model tokens |
| **Price** | 3× cost (`PRICE_MULTIPLE`): ~₹400–1,200 per memo, shown on the quote before any spend |
| **Margin control** | The budget is a hard ceiling. The quote's coverage curve lets the buyer pick their own price/coverage point. |
| **Why it scales** | One record settles most claims, so the marginal cost goes to the few claims that matter. Resume and replay mean no work is paid for twice. |

## Design decisions

| Option | Verdict |
|---|---|
| An agentic LLM with tools writes the memo | **Rejected.** It has no enforceable ceiling, the deck can steer it, and its verdicts have no sources. |
| Enrich everything about the company and every founder | **Rejected.** It pays for records no claim needs, and it still can't tell a thin record from a contradiction. |
| **The model reads, code prices, buys and judges** | **Chosen.** The ceiling and every verdict live in code. The cheapest record that settles the most claims comes first. A contradiction that one search could overturn gets that search before the memo calls a founder wrong. |
| Greedy value-per-credit planner vs. an optimal knapsack | Greedy. Fewer than 20 checks, and verdicts change after each purchase, so re-planning greedily after every buy beats a single up-front optimum. |
| Reserve at worst case vs. at expected price | Worst case. Reserving at the expected price would let the last purchase overshoot the ceiling; the cost of pessimism is a little unused headroom, which the ledger returns on settle. |
| Event-sourced ledger (fold over entries) vs. mutable rows | Append-only. It gives the audit trail and the hash chain for free, and crash recovery is just "find calls whose latest event is `reserved`". |
| Re-judge everything vs. track claim ↔ evidence dependencies | Re-judge everything. Judges are pure and there are tens of claims, so a dependency graph would add bugs and save microseconds. |
| SQLite vs. Postgres for the ledger | SQLite with WAL and `synchronous=FULL`. One process, durable on hard kill, no infrastructure, and the hash chain is portable as a single file. |
| Learning planner priors per deck | Not done. One deck is too little data, so `learn()` is a stub and the priors are fixed. |

## Known limits

- **Young companies with thin records** mostly come back *can't verify*, never *contradicted*. The founder questions ask for evidence instead.
- **The balance check** assumes no one else spends on the same API key during the run. Two concurrent runs on one key will each see the other's spend.
- **Revenue, customers and awards** can only be checked on the web. A customer claim needs a named subject to be worth a search.
- **INR amounts** can't be compared until `TALLY_INR_PER_USD` is set. They come back *can't verify* with that reason.
- **Comparables** need a funding record. Companies without one are skipped instead of padding the list.
- **Single process.** The purchase lane is an in-process lock, and each run has its own `CrustClient`, so the per-endpoint rate limiter isn't shared across concurrent runs (429 retries absorb the overflow). Running several workers would need the reservation step and the limiter to move into shared storage, e.g. a conditional `UPDATE` on a budget row in Postgres.
- **Reconciliation matches by endpoint.** Two in-doubt calls on the same endpoint in one run are resolved in order against unclaimed charges on that endpoint. The total is exact; which of the two gets which amount isn't guaranteed.
- **The hash chain proves integrity, not origin.** Anyone who can write the SQLite file can rebuild a consistent chain. Publishing the head hash with each memo would anchor it.
