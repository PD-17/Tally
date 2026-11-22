# Build plan: 7.1 The Research Buyer

ContextCon 05, BITS Pilani, Sunday 22 November 2026. Written 20 Nov 2026 from the problem statements, the event site and the Builder Handbook. Credit figures are planning estimates to measure on the day with `X-Credits-Used`. Crustdata field names and endpoint paths not quoted from the handbook should be checked in the docs.

**Updated 22 Nov for the handbook revised on 21 Nov:**
- **Building starts at 10:15 AM**, so you have 7 h 45 min, and the repo's commit history must start then.
- **Company Identify costs 0.2 credits per match** again; a non-match is free.
- **Every service must meet five rules:**
  1. decisions on live data
  2. a source for every claim, with "I can't verify" naming what's missing
  3. a person approves anything you can't take back, with payments in Razorpay or Stripe test mode
  4. a time limit and a credit limit on every run, with **model calls in the ledger**
  5. a crash or timeout neither repeats nor loses anything
- **The judges also want:**
  - a business case in rupees (your price, and what one job costs to run)
  - who the product could get wrong
  - only the personal data the job needs
  - an answer to "100 times the volume, or a second client?"
  - test cases written before coding and dated after 1 Sept 2026

The sections below are updated to match.

---

## 0. What you're building

A client types a research job and a budget, for example *"the head of engineering at 60 Indian fintech companies with 50–500 people, for 400 credits"*. Your service then:

- plans what data to buy and buys it
- returns a sourced answer for each company
- keeps a ledger that matches Crustdata's reported costs to the credit
- adds a note on what more budget would buy

The client watches spending live and can cut the budget at any moment.

**The one design rule:** code controls the money, never the AI model. The model reads the job, judges unclear matches and writes the report. Every purchase goes through a deterministic governor that the model can't get around.

**Pitch it as a service, not as infrastructure.** Say "a research analyst that delivers a sourced list within a fixed budget". The governor and ledger are the reason the client can leave it running unattended. The judges score "as the customer who would buy the service", so this framing protects the 20 points for customer value.

---

## 1. Architecture

```
            ┌────────── Dashboard (browser) ───────────┐
            │ job box · budget gauge · cut slider ·    │
            │ approve/deny · kill · company grid · feed│
            └──────────────┬──────────────▲────────────┘
                     REST (cut/approve)   SSE (events)
            ┌──────────────▼──────────────┴────────────┐
            │              FastAPI (api.py)            │
            └──────────────┬───────────────────────────┘
                           │
   job text ──► LLM: parse_job ──► JobSpec
                           │
                ┌──────────▼───────────┐     ┌──────────────────┐
                │ Root agent / planner │────►│ Sub-agents (×3)  │
                │ (value per credit)   │     │ child budgets    │
                └──────────┬───────────┘     └────────┬─────────┘
                           │  every purchase           │
                ┌──────────▼───────────────────────────▼─────────┐
                │ GOVERNOR: budget tree · reserve→settle ·       │
                │ cut · approval threshold · kill · purchase lane│
                └──────────┬─────────────────────────────────────┘
                ┌──────────▼───────────┐    ┌───────────────────┐
                │ LEDGER (SQLite WAL)  │◄──►│ Crustdata client  │──► api.crustdata.com
                │ idempotency, in_doubt│    │ cache · rate limit│
                └──────────────────────┘    │ X-Credits-Used    │
                                            └───────────────────┘
   Evidence store ──► Verifier (rules + LLM) ──► Report (cite-or-drop) + "more budget buys"
   Seller check ◄── teammate's "premium list" service
```

---

## 2. Stack

| Need | Choice | Why |
|---|---|---|
| Language | **Python 3.12** (3.10+ is the floor for the Anthropic SDK 1.x), **uv** for setup | Fast to set up |
| HTTP client | **httpx** (async) | You need the response headers (`X-Credits-Used`, `x-ratelimit-reset`) and full control over retries |
| Data models | **pydantic v2** | Typed JobSpec, Evidence and LedgerEntry; also feeds structured outputs |
| AI model | **Anthropic Python SDK**, `claude-opus-5-5` for reasoning and `claude-haiku-5-5` for bulk title checks | Structured outputs via `messages.parse(output_format=Model)` |
| State | **SQLite** in WAL mode (stdlib `sqlite3`, run through `asyncio.to_thread`) | Crash-safe, one file, easy to inspect in the demo |
| Server | **FastAPI + uvicorn + sse-starlette** | Live event stream, plus endpoints for cut, approve and kill |
| UI | **One HTML page**, vanilla JS (or htmx) with a server-sent event stream | No build step. Streamlit's rerun model fights live budget cuts |
| Tests | **pytest + hypothesis** | Property tests prove the budget rules hold |
| CLI | **rich** | Headless runs and a fallback demo |
| Video and diagram | Loom; Excalidraw or Mermaid | The submission form requires both |

**Don't use LangChain, LangGraph or CrewAI for the main loop.** They add retries and hidden calls you don't control, which is exactly how an agent ends up paying twice. This is a code-orchestrated workflow, so plain asyncio plus SDK calls is simpler and easier to audit.

Use the Crustdata MCP server (`https://install.crustdata.com/mcp`) and CLI only to explore data while you build. The product calls the REST API through your governed client.

---

## 3. Repo layout

```
research-buyer/
  README.md  NOTES.md  RESULTS.md  pyproject.toml  .env.example
  rb/
    config.py        # keys, thresholds, model ids
    models.py        # JobSpec, Unit, Candidate, Evidence, Probe, CallRecord
    pricebook.py     # GET /account/endpoints → prices, permissions, limits (free)
    crust.py         # Crustdata client: headers, rate limits, cache, retries
    ledger.py        # SQLite WAL: calls, budgets, approvals, evidence
    governor.py      # budget tree, reserve/settle, cut, approvals, kill
    probes.py        # find_companies, find_people, company_people, web_fact, confirm_person
    planner.py       # value-per-credit scheduler, stop rules, "more budget buys"
    agents.py        # root + sub-agents (asyncio tasks)
    verify.py        # rules first, then the AI model; assemble evidence
    llm.py           # parse_job, classify_titles, resolve, write_summary
    seller.py        # premium-list sample check
    forecast.py      # pilot + Monte Carlo cost forecast
    report.py        # answer table, sources, ledger summary
    api.py           # FastAPI + SSE
    static/index.html
  seller_sim/app.py  # the teammate's "premium list" seller
  baseline/enrich_all.py
  eval/run_eval.py  eval/chaos.py  eval/sealed.sha256
  tests/test_governor.py  tests/test_ledger.py
```

The repo must be new, with its commit history starting at 10:15 AM on 22 November.

---

## 4. Module design

### 4.1 Crustdata client (`crust.py`)

- **Headers:** `authorization: Bearer <key>` and `x-api-version: 2025-11-01`. Base URL `https://api.crustdata.com`.
- **Rate limits:**
  - One token bucket per endpoint: about 30 a minute for person search, company search, company identify and job search; 15 for company enrich; 10 for web search/fetch and watchers.
  - The 21 Nov handbook no longer mentions a 200-a-minute limit per key, so there's no global bucket.
  - Load the real limits from `GET /account/endpoints` at startup (the dashboard's Endpoints page shows the same).
- **Errors:**

  | Code | Meaning | What to do |
  |---|---|---|
  | 429 | Over the rate limit | Sleep until `x-ratelimit-reset`, then retry the same request with the same idempotency key |
  | 402 | Out of credits | Stop the run. Never retry |
  | 403 | This key can't call that endpoint | Mark the endpoint unavailable; the planner drops it |
  | 401 | Bad key | Fail loudly |
  | Timeout | No answer | Retry with backoff and the same idempotency key. After 3 tries, mark the company `held: source timed out`. Never treat silence as "no head of engineering" (handbook rule 5) |

- **Cache:** keyed by `sha256(endpoint + canonical JSON body)`. A cache hit costs 0 credits and is logged as `cached`.
- **Log everything:** record `X-Credits-Used` and `X-Request-Id` from every response, and save each raw response to disk.

### 4.2 Ledger (`ledger.py`)

Set `PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;` so a committed row survives a hard kill.

```sql
CREATE TABLE calls(
  idem_key TEXT PRIMARY KEY,      -- sha256(run_id|endpoint|canonical body)
  run_id TEXT, budget_id TEXT, unit_id TEXT,
  kind TEXT CHECK(kind IN ('crustdata','model','payment')),   -- model calls are required in the ledger
  endpoint TEXT, reason TEXT,
  reserved REAL, actual REAL,                                 -- credits, for crustdata rows
  model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_inr REAL,
  status TEXT CHECK(status IN ('reserved','settled','failed','in_doubt','cached','held')),
  x_request_id TEXT, started_at TEXT, settled_at TEXT, response_path TEXT);
CREATE TABLE runs(id TEXT PRIMARY KEY, client_id TEXT, job_text TEXT,
  credit_limit REAL, deadline_at TEXT, price_inr REAL, status TEXT);
CREATE TABLE budgets(id TEXT PRIMARY KEY, run_id TEXT, parent_id TEXT, lim REAL);
CREATE TABLE budget_events(id INTEGER PRIMARY KEY, budget_id TEXT,
  kind TEXT, delta REAL, actor TEXT, at TEXT);          -- create|cut|grant|reclaim
CREATE TABLE approvals(id INTEGER PRIMARY KEY, idem_key TEXT, amount REAL,
  content_hash TEXT, status TEXT, decided_at TEXT);
CREATE TABLE evidence(id INTEGER PRIMARY KEY, run_id TEXT, unit_id TEXT,
  person_ref TEXT, claim TEXT, source_kind TEXT, source_ref TEXT,
  record_updated_at TEXT, fetched_at TEXT);
```

**The purchase protocol** (beats test 5 and the "retries spend twice" trap):

1. If the idempotency key already exists and is `settled`, return the saved response (no charge).
2. Insert a `reserved` row and commit, **before** the HTTP call.
3. Make the call.
4. Save the response, then update the row to `settled` with the actual cost from `X-Credits-Used`, and commit.
5. **On restart,** any row still `reserved` becomes `in_doubt`. Reconcile it against Crustdata's free usage records under `/account/` (check the exact path in the docs on the day).
   - If you were charged, settle the row at the charged amount. If the response was lost, buying it again is logged as a **new** purchase, never a silent retry.
   - If the usage records can't settle it, count it as charged at the reserved amount. The ledger may then overcount but never misses a call.
6. **End-of-run check:** the ledger's Crustdata total must equal the drop in your `GET /account/credits` balance (free). Show this number in the demo.
7. **Model calls:** after each Anthropic call, write a `model` row with `usage.input_tokens`, `usage.output_tokens` and the cost at list price, with the rate in `config.py`. The report shows credits, model cost and the run's total cost in rupees.

### 4.3 Governor (`governor.py`), the core of the project

```python
class BudgetNode:
    def __init__(self, id, limit, parent=None):
        self.id, self.limit, self.parent = id, limit, parent
        self.spent = 0.0; self.reserved = 0.0; self.children = []
    def available(self):                      # what this node may still commit
        carved = sum(c.limit for c in self.children)
        return self.limit - self.spent - self.reserved - carved
    def carve(self, child_id, amount):        # child budget comes OUT of the parent
        assert amount <= self.available()
        c = BudgetNode(child_id, amount, self); self.children.append(c); return c
    # No public method lets a child raise its own limit; only parent.grant(child, x)

class Governor:
    def __init__(self, root, ledger, deadline, approve_over=20.0):
        self.root, self.ledger, self.approve_over = root, ledger, approve_over
        self.deadline = deadline              # epoch seconds: every run has a time limit too
        self.lane = asyncio.Lock()            # ONE paid call in flight → "stops within one call"
        self.killed = False
    async def buy(self, node, endpoint, body, est_max, reason):
        key = idem_key(node, endpoint, body)
        if (hit := self.ledger.settled(key)): return hit          # already bought
        async with self.lane:
            if self.killed or time.time() >= self.deadline or est_max > node.available():
                raise BudgetRefused(node.id, est_max)             # deadline = the run's time limit
            if est_max > self.approve_over:
                await self.wait_approval(key, est_max)              # bound to the content hash
            node.reserved += est_max; self.ledger.reserve(key, node, endpoint, est_max, reason)
            try:
                resp, cost = await crust.call(endpoint, body)
            except Exception:
                self.ledger.fail(key); node.reserved -= est_max; raise
            node.reserved -= est_max; node.spent += cost
            self.ledger.settle(key, cost, resp)
            return resp
    def cut(self, new_limit):                 # client lowers the ceiling
        self.root.limit = new_limit           # re-check before the next reservation
        shrink_children_proportionally(self.root)   # never below each child's spent+reserved
```

- **The purchase lane** (one paid call at a time) is what makes "stops within one call" true. Rate limits already cap you at roughly one call every 2 seconds, so you lose almost no speed.
- **After a cut:**
  - If spending is already over the new ceiling, stop and return the best answer so far.
  - Otherwise, re-plan within the new ceiling.
- **Approvals** are bound to `sha256(idem_key|amount)`. If the request changes, the approval no longer applies. While a purchase waits for approval, the agent works on other companies. Keep the threshold high enough that approvals are genuine exceptions: the handbook's End-to-end criterion rewards asking a person only when the answer changes the result.
- **Anything you can't take back waits for a person** (handbook rule 3): over-threshold buys, any purchase from the seller, and any top-up. Seller and top-up payments run in **Stripe or Razorpay test mode** with test keys.
- **Deadline:** when the time limit passes, the governor refuses new reservations, just as after a cut, and the run returns its best answer so far. Companies it didn't reach are marked "not reached: time limit".

**Property tests with hypothesis** (`tests/test_governor.py`). Generate random sequences of carve, grant, cut, buy and crash, then assert:

- a child's limit never goes up except through `parent.grant`
- the children's limits never add up to more than the parent's
- spent never exceeds the limit after a cut
- after a cut, at most one call settles

### 4.4 Probes and planner (`probes.py`, `planner.py`)

**Generic building blocks**, so a job the judges invent still breaks down into known steps:

| Probe | Endpoint | Cost (check on the day) | Settles |
|---|---|---|---|
| `find_companies(spec)` | company search, or company identify for a given list | 0.03 per result, plus premium filter groups; identify costs 0.2 per matched identifier, and a non-match is free (21 Nov handbook) | The list of companies to cover |
| `find_people(company_ids, role)` | person search, with several companies batched in one query using `in` filters | 0.03 per result; filtering on job history adds about 0.1 per result for filtering and 0.1 per result returned | Candidates with dated roles |
| `company_people(id)` | company enrich, people section | 2 | The CTO or VP Engineering at small firms |
| `web_fact(query)` | web search and fetch | 1 per page | Recent appointments and news |
| `confirm_person(id)` | person enrich | 1–9 | Current role and start date |

**How the planner picks** (anytime and greedy):

- For each unresolved company, score every possible next probe as `value = P(settles) × value_of_answer ÷ expected cost`.
- Buy the highest-value probe, update that company's candidates, and repeat.
- Stop when the best value falls below a floor, when the budget is spent, or when the run's time limit is reached (20 minutes by default, shown to the client up front).
- Start the P(settles) values from a pilot on 5 companies, then update them as you go.

**Batching:**

- Put 10–20 company IDs in one person-search query, and use 100 results per page.
- Use autocomplete (free) to get exact filter values.
- Filter on cheap fields where you can, and post-filter in code.

**Credit estimate for the example job** (planning figures; measure them with `X-Credits-Used`):

| Step | Estimate |
|---|---|
| Company list (60) | ≈20 by search, or ≈12 if the client gives the list and you identify each company (60 × 0.2) |
| People search (about 4 candidates per company) | ≈55 |
| Confirming about 25 unclear companies (web search or enrich) | ≈25–50 |
| **Total** | **≈90–130 against a budget of 400** |

You still finish after the budget is halved. Add the model tokens on top in rupees; they're small next to the credits but must appear in the ledger.

**The business case** (the judges now ask for rupees): the report's cost of one job (credits × your per-credit rate, plus model tokens) is your cost to serve. Your price is that cost plus a margin, compared with what a research analyst charges for the same list. For the per-credit rate, the event site values each builder's 15,000 credits at $4,500, about $0.30 a credit; at that rate a 110-credit job costs about $33 in data. Take the model's token price from Anthropic's pricing page.

**The "what more budget would buy" note:** price the planner's queue of probes it didn't run, and add up their expected extra right answers. For example: *"+120 credits ≈ 9 more companies resolved."*

### 4.5 Sub-agents (`agents.py`)

- Split the 60 companies across 3 asyncio sub-agents. The root carves each one a child budget, for example 30% of what's available, and keeps a reserve.
- A sub-agent can **ask** for more. Only the root decides, using `grant`. It can also take back unused budget.
- Every sub-agent buys through the same governor and lane, so cuts and kills reach all of them at once.
- On the dashboard, show the budget tree with each node's limit, spent and reserved amounts.

### 4.6 Verifier and AI model calls (`verify.py`, `llm.py`)

**Rules first.** Accept a candidate if:

- their current employer's ID equals the company's ID
- their title matches a list of synonyms: Head/VP/Director of Engineering, or CTO at companies under about 150 people
- the record's `updated_at` is recent

**Then the AI model, only for unclear cases.** Batch the title checks on Haiku with low effort (`output_config={"effort": "low"}`), and cache them by (title, company-size band). Send unclear choices between candidates to Opus with structured output.

```python
from pydantic import BaseModel
from typing import Literal
import anthropic

class JobSpec(BaseModel):
    entity: Literal["company"]
    company_filters: dict[str, str]     # e.g. {"country": "India", "industry": "fintech", "headcount": "50-500"}
    target_count: int
    role: str                           # "head of engineering"
    role_synonyms: list[str]
    output_fields: list[str]
    unsupported_reason: str | None      # set when the job is outside what the service can do

client = anthropic.AsyncAnthropic()

async def parse_job(text: str) -> JobSpec:
    resp = await client.messages.parse(
        model="claude-opus-5-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": f"Turn this research job into a JobSpec:\n{text}"}],
        output_format=JobSpec,
    )
    return resp.parsed_output
```

- **Cite or drop:** the report is built from the evidence table in code. Any name without a source is dropped, and its row says "can't verify" and names what's missing. The handbook now says a wrong fact counts against you more than an honest "I can't verify", so set a high bar for "confirmed".
- **Required:** log model tokens and cost in the ledger as `model` rows (handbook rule 4), so *every* cost is visible.
- **Only the personal data the job needs:** keep name, title, role start date, profile URL and the source passage. Strip emails, phone numbers and other fields from enrich responses before saving evidence.
- **Who it could get wrong**, with a guard for each (the judges ask this):
  - common names: match on the company ID, never the name alone
  - someone who left recently: the freshness check plus a "conflict" flag
  - inflated titles at tiny firms: mark them "probable"

### 4.7 Premium-list seller check (`seller.py`, `seller_sim/app.py`)

- A teammate runs a small FastAPI "seller". It offers a list of "fresh heads of engineering at 60 fintechs" for, say, 150 credits, with stale people deliberately mixed in.
- The agent:
  1. Asks for a free sample, or buys 5 records.
  2. Checks each against Crustdata: current employer, title and `updated_at`.
  3. Buys only if the sample is at least 90% accurate **and** the claimed freshness holds.
- Any purchase from the seller waits for a person's approval, and the payment runs in Stripe or Razorpay test mode. Record the purchase, or the refusal and its reason, in the ledger.
- The handbook says agents talk only to your own team and the organisers during the event; the seller is your teammate's service, so this is in bounds.

### 4.8 Forecast (`forecast.py`), the stretch goal

After the 5-company pilot, run a Monte Carlo simulation over the probe sequences using the observed settle rates and prices. Show a typical forecast (P50) and a high-end one (P90) before the full run. At the end, show forecast against actual; the target is within 15%.

### 4.9 API and dashboard (`api.py`, `static/index.html`)

| Endpoint | Does |
|---|---|
| `POST /jobs` | Starts a run with the job text and budget |
| `GET /events` | Live server-sent event stream |
| `POST /budget/cut` | Cuts the budget |
| `POST /approvals/{id}` | Approves or denies a purchase |
| `POST /kill` | Stops the run |
| `GET /report` | Returns the report |

The page shows:

- a budget gauge (limit, reserved, spent)
- spending by endpoint
- the live call feed, with the planner's reason for each call
- a 60-tile company grid (unknown, candidate, confirmed, can't verify)
- the budget tree
- a "halve budget" button and an approvals panel

---

## 5. Each test and what passes it

| Test | What passes it |
|---|---|
| 1. Test set: the right answer for 20 of 60 companies | Written **before coding**, as the 21 Nov handbook asks (declare it on the form). Include a duplicate company, a lookalike name and a case whose right answer is "I can't verify", and pick some companies whose head of engineering changed **after 1 Sept 2026**, past the models' training data |
| 2. More right answers per 100 credits than an enrich-everything baseline | The planner's value ranking plus batching and caching. `baseline/enrich_all.py` runs on the same job; the eval prints both |
| 3. Halve the budget at random; stop within one call | The one-at-a-time purchase lane plus re-checking the budget before every reservation. `eval/` fires the cut at a random second and asserts at most one call settles afterwards |
| 4. A sub-agent's budget only shrinks; it can't raise its own | The budget tree's API plus the hypothesis property tests |
| 5. Kill mid-run, restart, no double or missed count | Reserve before the call, plus `in_doubt` reconciliation. `eval/chaos.py` hard-kills the process 3–5 times at random (`Popen.kill()`), restarts it, then compares the ledger with Crustdata's usage records and the balance drop |
| 6. A premium-list seller | The sample check in `seller.py` |
| Going further | The forecast with P50/P90 against actual |

---

## 6. Evaluation

- `eval/run_eval.py --sealed` opens the test set once and writes `RESULTS.md`, with one table row per test: PASS/FAIL, right answers per 100 credits for yours and the baseline, coverage, precision, credits, model cost and seconds.
- Use a separate practice set of about 10 hand-checked companies for tuning. Never tune on the test set.
- Keep `NOTES.md` from 10:15: the approaches you tried (for example, enriching first versus searching first), what worked, and the hardest case. The judges score this, and it feeds the README and the form's "How does your product work?" answer.
- **README:** add a "Sources and checks" section (new in the handbook's suggestions). It covers every data source the service calls, where a person approves, and the ledger of what each call cost. Add the business case in rupees, too.

---

## 7. Day schedule

| Time | A · Agent | B · Spine | C · Product | D · Truth |
|---|---|---|---|---|
| Before 10:15 | | | | Test set written (no code): 20 answers, edge cases, some dated after 1 Sept 2026 |
| 10:15–11:15 | Repo, JobSpec, `parse_job` | Price book, client skeleton | FastAPI and SSE skeleton | Pick the 60 companies; finish checking the test set |
| 11:15–1:00 (lunch 12:30, eat in shifts) | `find_companies` and `find_people`, simple pick, report with sources | Ledger v1 (with model rows), governor v1 (reserve/settle, deadline) | Dashboard: gauge, feed, grid | Practice set; rupee rates in `config.py` |
| **≈1:00** | **One real request runs end to end** (the handbook's suggested pace) | | | |
| 1:00–2:30 | Planner value ranking, verifier with the AI model | Cut, kill, approvals, `in_doubt` | Cut slider, approvals panel | `enrich_all` baseline |
| **2:30** | **Checkpoint submission.** Show a mentor the chaos test | | | |
| 2:30–4:00 | Sub-agents, "more budget buys" | Property tests, `chaos.py` | Budget tree view, polish | Seller simulation, `run_eval.py` |
| 4:00–4:30 | Freeze code. Open the test set once and write `RESULTS.md` | | | |
| 4:30–5:30 | Help with the README | Diagram | **Video** | Form text |
| **5:30–5:50** | **Submit** | | | |

**Cut order if you're behind:** forecast first, then the seller check, then sub-agents (keep one child level plus the property tests). Never cut the ledger, the cut button or the crash test.

**Solo or two people:** use the `rich` command-line display plus a minimal page, with one sub-agent level and no forecast.

---

## 8. Two-minute demo

| Time | Show |
|---|---|
| 0:00 | One sentence: research teams pay analysts to build sourced lists; this one does it within a fixed credit budget |
| 0:10 | Type the job in customer words with 400 credits. The forecast appears, with the time limit and the price in rupees |
| 0:25 | The run: tiles fill in, the ledger ticks, each call shows its reason |
| 0:50 | **Halve the budget.** Buying stops within one call and the best answer so far comes back |
| 1:10 | **Kill the process and restart it.** The ledger matches Crustdata's usage records with nothing counted twice |
| 1:30 | The seller pitches a "fresh" list. The agent samples it, finds stale people and declines |
| 1:45 | Scoreboard against the baseline: right answers per 100 credits, ledger equal to the balance drop, forecast against actual |

**In the finals,** also show a company under two names merging into one record, a 429 or 403 being handled, a restart mid-run (now on the handbook's list), and a fresh job typed in by a judge. If anything breaks on stage, play the video and keep going.

**Be ready for the scale question** ("100 times the volume, or a second client?"):
- **Volume:** the per-endpoint rate limits are the bottleneck. Batching 10–20 companies per person search, the cache and the planner skipping low-value calls mean 100 times the companies costs far less than 100 times the calls. Past that, run several keys under one budget tree.
- **A second client:** each client is its own budget root with its own ledger partition, so one client can't spend another's credits. SQLite becomes Postgres, and the idempotency keys stay the same.

---

## 9. Risks and fallbacks

- **Blocked or plan-gated endpoints:** read `GET /account/endpoints` as soon as building starts at 10:15. The planner leaves out anything that returns 403.
- **How `X-Credits-Used` behaves:** check it on your first call against `/account/credits`, and whether it can be fractional.
- **The usage-records endpoint path:** confirm it in the docs. If there isn't one, reconcile against the balance drop alone.
- **Messy titles:** use the synonym list plus the AI model. "Can't verify" is an acceptable answer.
- **Rate limits:** batching keeps the run under about 150 calls, well inside 20 minutes.
- **Judges asking for a job outside this one:** the parser sets `unsupported_reason` and explains clearly. This includes the theme's sample request about buying tools for a team.

---

## 10. Before Sunday (no code)

1. Read Crustdata's docs: pricing, rate limits and best practices, person search, company search and identify, autocomplete, and the account and usage endpoints.
2. Read about idempotency keys; Stripe's documentation explains them well.
3. Sketch the architecture diagram from section 1.
4. Create the dashboard account with code PILANI05, claim credits at app.crustdata.com with your BITS Pilani email and promo code CONTEXTCON-BITS, and make one first call.
5. Have GitHub, Loom and your Anthropic key ready.
6. If you prepare anything in advance, declare it on the submission form ("Anything written before 10:15 AM?").
7. Write the test set before coding (section 5, test 1). The handbook now asks for this.
8. Check in on the check-in form on your phone (from 7:45 AM, with your BITS Pilani Google account). Doors open at 8:30.
