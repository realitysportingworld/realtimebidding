# RTB Auction Engine

A **real-time bidding (RTB) engine** for programmatic advertising, with ML that
decides what each ad impression is worth. Every bidding claim below is tested
on **12 million real auctions** from the iPinYou RTB logs, with time-based
splits, held-out test days and bootstrap confidence intervals.

**Live:** [dashboard](LIVE_DEMO_URL) · [API docs](https://rtb-engine.onrender.com/docs)

## Results at a glance

| | Result | How it was measured |
|---|---|---|
| **Expected-value bidding** | **+45% clicks** at the same budget (95% CI +40% to +54%), cost per click −35% | Bid `pCTR × value` against a flat bid, replayed on 3.4M held-out real auctions at half budget |
| **CTR model** | **AUC 0.803**, calibration error 49% → 11.5% | Logistic regression with user tags, isotonic calibration; beat gradient boosting on the tune day |
| **First-price bid shading** | **+16% clicks** at the same budget (95% CI +13% to +20%), overpayment 69% → 44% of spend | Win-price curve learned from won/lost outcomes only, against the best fixed-fraction bid |
| **Bidder throughput** | **125 bids/s, zero lock waits** (from 75 bids/s, 29 of 44 backends waiting) | Budget leasing removed the hot campaign row from the bid path; 4 workers, 10 bids in flight |
| **Latency** | **27ms p50** server-side per auction, live | Render Frankfurt + Supabase eu-central-1 |
| **Train/serve parity** | Served models match the notebooks to **~10⁻¹⁴** | 22,745 real requests rescored by the serving code in CI |

**What's in it**

- **OpenRTB 2.5 bidder:** standard bid requests; win, loss and click notices settled exactly once; budgets paced across the day; every bid's features logged for retraining.
- **ML, offline and served:** CTR models and a bid landscape trained in [notebooks](notebooks/), exported as plain JSON and scored in pure Python at 10–20 µs per request. No scikit-learn on the server.
- **Honest evaluation:** time-based splits, paired replays, block-bootstrap CIs, a best-in-hindsight robustness check, and a pipeline check that must reproduce earlier published numbers exactly.
- **Correct money:** single-statement budget writes, reservations that can't overspend across processes, and idempotent notices. 170 tests, run in CI against a real Postgres.

## Architecture

```
Publisher (SSP)  →  Bid Request  →  RTB Engine  →  Auction Result
                                        │
                              ┌─────────┼─────────┐
                           FastAPI   PostgreSQL   Redis Streams
                              │                       │
                          Auction Logic          Impression Events
                         (second-price)         (analytics pipeline)
```

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| API | FastAPI (async) | An auction is mostly waiting on Postgres, not computing |
| Database | PostgreSQL + SQLAlchemy | ACID transactions for billing accuracy |
| Cache/Streams | Redis + Redis Streams | Event pipeline for impressions/clicks |
| Frontend | React + Recharts | Live dashboard with auto-refresh |
| ML | scikit-learn (offline) + pure-Python serving | CTR model drives expected-value bidding |
| Containerisation | Docker Compose | Reproducible local dev environment |

## Key Features

- **Second-price (Vickrey) auction** — winners pay 2nd highest bid + $0.01, incentivising truthful bidding
- **OpenRTB 2.5 bidder** — exchanges send standard bid requests; bids reserve budget, and win, loss and click notices settle them exactly once
- **Budget capping and pacing** — campaigns stop at their daily and lifetime budgets, and `even`-paced campaigns spread spend across the day
- **Targeting** — country, device type, and IAB category filtering
- **Redis Streams event pipeline** — impression events fire asynchronously, never blocking the auction response
- **CTR-driven expected-value bidding** — a click-through-rate model trained on real ad data lets campaigns bid `pCTR × value_per_click` instead of a flat CPM (see below)
- **First-price bid shading** — a win-price curve learned from won/lost outcomes alone picks each bid to maximise expected surplus; +16% clicks at equal budget over the best fixed-fraction bid on real iPinYou auctions (see below)
- **Live A/B test** — every auction is randomly assigned to flat-CPM (control) or expected-value bidding (treatment); the dashboard compares their cost per click side by side
- **Live dashboard** — 5-second auto-refresh with auction volume charts, campaign CTR, fill rate KPIs


## Accounting Correctness

Auction prices and campaign spend intentionally use different units:

- Bids and clearing prices are integer **CPM cents** (cents per 1,000 impressions).
- A filled auction is charged in integer **USD microdollars**. For example,
  a 150-cent CPM becomes a 1,500-micro ($0.0015) impression charge.
- Daily and lifetime spend are accumulated in microdollars, avoiding floats and
  avoiding the 1,000x overcharge caused by treating a CPM quote as one impression.
- Budget reservation is one conditional PostgreSQL update. Concurrent auctions
  cannot reserve spend beyond either the campaign's daily or lifetime limit.
- API and dashboard totals are converted back to cents only for presentation.

The auction response exposes both `clearing_price_cents` (the CPM quote) and
`charged_cost_micros` (the exact cost of that impression), making the unit
boundary visible in the demo and auditable in tests.

## Bidding on an Exchange (OpenRTB 2.5)

In the house auction the engine is the exchange: it runs the auction and sets
the price. On a real exchange the roles change. The exchange runs the auction;
the bidder only says what it would pay, then learns the outcome from a **win**
or **loss notice**, often seconds later and sometimes never.
`POST /api/v1/openrtb/2.5/bid` takes a standard OpenRTB 2.5 bid request and
answers with a bid response (`price`, `adm`, `nurl`, `lurl`, `crid`, `adomain`),
or `204 No Content` when it doesn't bid, as the spec has it.

- **Same decision code.** The exchange's impression (`imp.banner`,
  `imp.bidfloor`, `site`, `device`) is shaped like a house slot. The engine's
  own eligibility SQL, targeting, pacing, A/B arm and pCTR model decide the
  bid. Country arrives as ISO alpha-3 and category as IAB codes, and both are
  mapped.
- **Bids draw on budget leases.** The bidder doesn't know the price when it
  bids, so every bid's maximum cost (the bid itself) must already be held.
  Holding it per bid made one campaign row the bottleneck (see below).
  Instead, a process **leases** a slice of a campaign's budget: 1% of the
  daily budget, never more than half of what's left, and only as much as
  pacing allows. That's one conditional write to the campaign's row. The
  process then admits bids against the slice in memory, and each bid is one
  insert into its own table. Leases count against budgets everywhere, the
  house auction included, so bids in flight across any number of processes
  can't together overspend. Tested cases:
  - 30 concurrent bids at a campaign with room for two produce exactly 2 bids.
  - Two processes with separate lease books, bidding at once against room for
    four, produce exactly 4.
  - In every case, all of them winning at full price stays within budget.
- **Settled exactly once.** A win charges the exchange's clearing price,
  capped at the bid, out of its bid's lease. A loss, or a bid that hears
  nothing within `BID_TTL_SECONDS` (60), moves no money. A notice can land on
  a different process from the one that bid, so the slice is never refilled;
  its unused rest returns when the lease closes. A process stops placing bids
  on a lease after `LEASE_SECONDS` (10), and any process's sweeper closes it
  once its last bids are past the TTL. That includes the leases of a process
  that crashed (tested). Each settlement takes row locks in a fixed order
  (bid, then lease, then campaign), so 8 duplicate win notices fired at once
  charge once (tested). A win notice arriving after its lease closed is still
  billed, since the ad ran. It's the one path that can overspend, and it's
  logged as `late`.
- **Clicks.** The ad markup routes clicks through `/openrtb/click/{bid}`. It
  counts a click once per impression, even one that arrives before its win
  notice, and redirects to the creative's own landing page. Nothing in the URL
  chooses the destination, so it isn't an open redirect.
- **Pacing.** An `even` campaign may only have spent (including reservations)
  the share of the UTC day that has passed, plus 5% headroom. It applies in
  both the house auction and the bidder, and the simulator names it as an
  exclusion reason.
- **Declines.** The bidder declines non-USD currencies, IAB25/IAB26 content,
  connected TV, video-only impressions, and advertisers on the request's
  `badv` block list.
- **The feature log.** Every exchange bid stores the model's inputs, its pCTR,
  the model version, the A/B arm, the bid and its outcome, plus the request's
  user segments, which the served model doesn't read. The iPinYou replay found
  segments carry most of the click signal, so the next model can use them.
  `GET /api/v1/openrtb/log.csv` exports settled bids as that training set.
- **Kept separate.** Exchange impressions are stored with `source = openrtb`,
  and the house A/B panel ignores them: their clicks are real, the house's are
  simulated, and mixing the two dilutes an arm.

[`scripts/simulate_exchange.py`](scripts/simulate_exchange.py) plays the
exchange. It sends bid requests, runs a second-price auction against simulated
competitors, calls `nurl` on a win and `lurl` on only half the losses, and
clicks some winning ads. After a 600-request run, an audit of the database
found everything consistent:
- **Spend:** campaign spend equals the sum of impression charges, and the
  exchange's $0.1243 matches what the leases charged.
- **Leases:** every one of the 60 balances (leased = charged + returned), and
  none are left open.
- **Reservations and budgets:** nothing is still reserved, and no campaign is
  over budget.

**Why leasing.** Before leasing, each bid reserved its cost on the campaign's
row. Ten bids at a time took ~150ms p50, and adding worker processes barely
moved throughput. Sampled during the load, 29 of 44 active Postgres backends
were waiting on row locks: one run-of-network campaign was the top bidder on
1,196 requests, so nearly every bid queued behind the last one's commit on
that row. With leasing, the same load (4 workers, 10 bids in flight) measured:

|  | Per-bid reservation | Budget leases |
|---|---|---|
| p50 | 104ms | **67ms** |
| p95 | 337ms | **164ms** |
| Throughput | 75 bids/s | **125 bids/s** |
| Backends waiting on row locks | 29 of 44 | **0** |
| Campaign-row writes | one per bid | one per ~26 bids (43 for 1,131) |

The database is now mostly idle under that load, and the remaining limit is
the workers' own CPU, which scales with more of them. The price of leasing is
that budget in an open lease is unavailable to everyone else for up to
`LEASE_SECONDS` + `BID_TTL_SECONDS`, which is why a lease is never more than
half of what a campaign has left. The house auction still charges the
campaign row directly, so it keeps the hot-row limit; leasing it the same way
is the next step.

## Machine Learning: CTR-Driven Bidding

Real demand-side platforms don't bid a flat price — they bid what an impression is
worth, which means predicting how likely it is to be clicked.

- **Model.** A click-through-rate (CTR) model trained offline on the real
  [Avazu](https://www.kaggle.com/competitions/avazu-ctr-prediction) ad dataset,
  restricted to features the auction engine can actually supply at bid time
  (device, publisher category, ad position, hour of day). Logistic regression,
  evaluated with a time-based train/test split and a calibration check.
- **Serving.** The trained coefficients are exported to plain JSON and scored with a
  few lines of pure Python (a weighted sum + sigmoid) — no scikit-learn or numpy in
  the running API, which keeps it light. If the model file is missing or a value is
  unknown, it falls back to the baseline rate and the auction never breaks.
- **Bidding.** In the treatment arm, a campaign bids `pCTR × value_per_click`, capped
  by its max CPM. Low-CTR impressions get lower bids; high-CTR ones get more.
- **Honest framing.** The model and its training data are real; the traffic it scores
  in the demo is simulated. That mirrors production, where a model trained on
  historical logs scores live traffic it has never seen. Simulated clicks are drawn
  from held-out empirical CTRs the model never trained on, so the A/B result isn't
  circular.
- **Outcomes for every auction.** Nobody looks at the demo's ads, so the engine
  draws each filled auction's click from the same held-out table, written in the
  same statement as the impression. An impression without an outcome would dilute
  the arm it lands in. Set `SIMULATE_CLICKS=0` once real click tracking calls
  `POST /auction/impression/{id}/click`.
- **Significance, not just a difference.** Cost per click is a ratio of
  per-impression means, and the impression is the randomised unit, so the dashboard
  puts a 95% delta-method interval on the treatment's change in CPC and only calls a
  winner when that interval excludes zero. A Monte Carlo test checks that equal arms
  are called different about 5% of the time.
- **A ground truth with structure.** The held-out CTR table holds every level of a
  hierarchy (category, device × category, … × position, … × hour), each shrunk
  toward its parent by empirical Bayes, and the simulator backs off level by level.
  The first version only stored full-detail cells and sent everything else to one
  global rate. 38 of the demo's 45 ad contexts had no row, the truth was flat, and
  the A/B measured +1.3% (95% CI −13% to +18%): with nothing to find, no model can
  find cheaper clicks. The notebook now checks the serving contexts directly. True
  CTR spans 7–28% (p10–p90) across them, and pCTR correlates 0.55 with it.

**Result.** Expected-value bidding lowers cost per click by **14.6%** (95% CI
−18.6% to −10.3%), pooled over 10 independent seeds of 6,000 auctions (~480 clicks
per arm each). Every seed came out cheaper, and 7 of 10 were significant on their
own. The seeds matter: each one draws a new market of campaigns, and a single
seed's interval only covers click noise within that market. On these contexts the
model also over-predicts on average (mean pCTR 0.222 vs a true 0.158), so
recalibrating it on the serving distribution is the next lever.

### On real auction logs (iPinYou)

The demo's traffic is simulated. To test the bidding rule on real auctions,
[`notebooks/ipinyou-replay.ipynb`](notebooks/ipinyou-replay.ipynb) replays the
[iPinYou](https://arxiv.org/abs/1407.7073) 2013 RTB logs. Season 2 covers 5
advertisers and 12.2M real impressions, each with its real market price (the
highest competing bid) and real click. The parsed counts match Table 3 of the
benchmark paper exactly. Results are in
[`notebooks/results/ipinyou_replay_results.json`](notebooks/results/ipinyou_replay_results.json).

- **Protocol** (as in the paper): requests replay in time order. A bid wins when
  it beats the logged market price, pays that price and gets the logged click.
  Bidding stops when the budget is spent.
- **Splits, all by time:** models train on 6–9 June. The model choice, its
  calibration, the guardrail and each strategy's bid level are all fixed on
  10 June. Results come from 11–12 June (3.4M auctions, 2,761 clicks), which
  nothing above ever sees. Every strategy replays identical traffic, so the
  comparisons are paired.
- **Uncertainty:** a block bootstrap over test hours (300 resamples).

**Model.** Four candidates, chosen by tune-day log loss:

| Model | Test AUC | Test log loss |
|---|---|---|
| Logistic regression, request features | 0.565 | 0.006462 |
| **Logistic regression + user tags** (chosen) | **0.803** | **0.006006** |
| Gradient boosting, request features | 0.711 | 0.006244 |
| Gradient boosting + user tags | 0.735 | 0.008476 |

User tags are the interest segments logged in each bid request. They're known
at bid time and carry most of the signal. Gradient boosting with its default
settings came out overconfident: with tags, its log loss is worse than without
them. It wasn't tuned, and the simpler model won.

**Calibration.** The raw model under-predicts (mean pCTR 0.70× the true CTR).
Isotonic regression fitted on the tune day brings that to 0.93× and cuts the
relative calibration error over pCTR deciles from 49% to 11.5%, with AUC
unchanged (0.805). Platt scaling barely helped (52%). This matters for the
engine, whose bid is pCTR × value: a biased pCTR is a biased bid.

**Guardrail.** An advertiser on whose tune-day traffic the model ranks below
AUC 0.55 bids flat instead. The threshold was fixed before any result. It didn't
fire: with user tags, the advertiser the first model couldn't rank (3476, AUC
0.474) reached 0.884.

**Result.** Clicks won at the same budget, EV bidding against a flat bid:

| Budget (share of real spend) | Request features only | **+ user tags, calibrated** (95% CI) | Cost per click (95% CI) | Best levels in hindsight |
|---|---|---|---|---|
| 1/2  | +10.5% | **+45%** (+40% to +54%)    | −35% (−38% to −32%) | +40%  |
| 1/8  | +71%   | **+296%** (+259% to +354%) | −75% (−78% to −73%) | +287% |
| 1/32 | +233%  | **+574%** (+449% to +739%) | −85% (−88% to −82%) | +473% |

The last column gives every strategy its best possible bid level in hindsight,
on the test days themselves. This rules out a gap that comes from a level badly
chosen on one noisy tuning day. The request-features column is the first
version of this notebook. It's kept in the replay and reproduces its original
numbers exactly, which checks that the pipeline didn't change underneath it.

The gain grows as the budget tightens: a small budget only buys the best few
percent of impressions, and finding those is exactly what the model does.

**Limitations.**
- **Logged auctions only:** the logs only contain auctions iPinYou won, so a bid
  above its original one is scored only on auctions that were logged. Every
  offline evaluation on this data shares this limitation.
- **Small budgets are noisy per advertiser:** at 1/32, two of the five
  advertisers won slightly fewer clicks than with the request-features model.
  The pooled gain holds, but the 1/2-budget figure is the conservative one.
- **Two models, by design:** this model needs an iPinYou bid request's features
  (its exchanges, regions, domains and user segments), which a visitor typing a
  URL doesn't have. So it serves the replayed real requests below, and the URL
  simulator keeps the Avazu model, whose features any request can supply.

### Serving it: the model in the engine

The notebook exports the chosen model as plain JSON (version-hashed): per-feature
weights, user-tag weights, and the isotonic calibration's breakpoints.
[`app/ml/ipinyou.py`](backend/app/ml/ipinyou.py) scores it in pure Python, in
about 10 µs per request, with no scikit-learn on the server.

- **Train/serve parity.** The notebook also exports 22,745 real test requests,
  each with the pCTR it gave them. [`test_ipinyou_parity.py`](backend/tests/test_ipinyou_parity.py)
  rescores every one in the serving code. The worst disagreement is 5.5 × 10⁻¹⁵
  before calibration and 6.7 × 10⁻¹⁴ after: floating-point rounding. A feature
  string formatted differently, or an unseen value sent to the wrong bucket,
  would fail it.
- **Replay endpoint.** `GET /api/v1/replay/auction` takes a real request, prices
  it with the served model, and bids with both strategies at the levels tuned on
  the notebook's tune day. Each strategy wins when its bid beats the price that
  really won, and the response says whether the ad was really clicked. The
  dashboard's **Real auction replay** panel shows it, and its headline numbers
  come from the model artifact, not from text in the UI.
- **The sample.** It holds 20,000 test requests drawn uniformly, plus every
  clicked test request so a visitor can find some. A random replay only draws
  from the uniform part, so clicks show up at their real rate (about 1 in 1,200
  requests). The "clicked" button draws from the clicked part, and the panel
  says so. In the sample, clicked requests sit at the 79th percentile of
  predicted CTR on average, against the 49th for random ones.
- **Data credit.** The sample comes from the iPinYou RTB dataset released for
  research (Zhang et al., [*Real-Time Bidding Benchmarking with iPinYou
  Dataset*](https://arxiv.org/abs/1407.7073), 2014). Bid IDs, user IDs, IPs and
  URLs are removed. Domains were already anonymised in the release.

### Bid shading for first-price auctions

Most programmatic buying moved to first-price auctions around 2019, where the
winner pays its own bid. Bidding an impression's value then wins it at no
profit, so a first-price bidder shades: it bids
`b* = argmax_b (value − b) · P(win | request, b)`.
[`notebooks/bid-shading.ipynb`](notebooks/bid-shading.ipynb) tests this on the
same iPinYou auctions. Results are in
[`notebooks/results/bid_shading_results.json`](notebooks/results/bid_shading_results.json).

- **Simulated first price:** a bid wins when it beats the logged market price
  (the highest competing bid) and pays itself. This holds the other bidders'
  behaviour fixed, the standard assumption in offline shading studies.
- **The bid landscape is learned from won/lost only.** A first-price bidder
  never sees the price it needed, only whether it won. So each fit-day request
  gets one exploration bid, log-uniform from 1 to 400 fen and independent of
  the market price, and one won/lost label. The model is logistic in log bid:
  `P(win) = σ(a(x) + β(x)·ln b)`, with `a(x)` from the request's features and
  user tags and the slope `β` per advertiser and exchange. On the tune day it
  reaches AUC 0.976, and its predicted win rates are off by 0.008 on average
  (expected calibration error), against 0.956 and 0.022 for a per-advertiser
  curve that ignores the request.
- **The optimum is exact.** `ln((v − b)·F(b))` is concave in `b`, so the
  surplus-maximising bid is unique and a bisection finds it. A brute-force grid
  check confirms it.
- **The baseline is tuned, not naive.** The unshaded EV bid gets its own level
  tuned for first price, which makes it the best *fixed fraction of value*: it
  ends up bidding about a quarter of the shaded bidder's value scale on every
  request. Shading has to beat that, by choosing the fraction per request.
- **Same protocol as the CTR replay:** levels tuned on 10 June, results on
  11–12 June, a paired block bootstrap over test hours. The notebook also
  replays the second-price strategies and reproduces the CTR replay's numbers
  exactly (1,562 and 2,268 clicks at half budget).

**Result.** Clicks at the same budget, contextual shading against the tuned
fixed-fraction EV bid:

| Budget | Clicks: fixed fraction → shaded | **Click lift** (95% CI) | Cost per click (95% CI) | Overpaid, share of spend | Best levels in hindsight |
|---|---|---|---|---|---|
| 1/2  | 1,593 → 1,846 | **+15.9%** (+12.7% to +20.4%) | −11.0% (−14.4% to −8.4%) | 69% → 44% | +15.0% |
| 1/8  | 1,120 → 1,217 | **+8.7%** (+5.6% to +12.8%)   | −5.0% (−8.5% to −2.4%)   | 56% → 41% | +9.1%  |
| 1/32 | 558 → 570     | +2.2% (−7.9% to +11.5%)        | −0.9% (−8.0% to +11.2%)  | 49% → 33% | +3.9%  |

"Overpaid" is what wins cost above the price that would have won them. A
per-advertiser landscape gets about half the gain at half budget (+8.5%), so
most of it comes from conditioning on the request. At 1/32 the gain isn't
significant: a budget that small only buys the highest-value impressions, and
for those a fixed fraction is already close to right. Against a flat first-price
bid, shaded EV wins +93% clicks at half budget.

**Limitations.** The other bidders are held fixed; in a real market they shade
too, and prices would move. The logs only contain auctions iPinYou won, as for
the CTR replay. And the landscape here is trained on iPinYou's features, so like
the CTR model it serves the replayed requests, not live exchange traffic.

**Serving it.** [`app/ml/shading.py`](backend/app/ml/shading.py) scores the
exported landscape in the CTR model's layout (reusing its scorer) and runs the
same bisection, in about 20 µs per request. [`test_bid_shading.py`](backend/tests/test_bid_shading.py)
reproduces the notebook's curve and shaded bid on 500 real test requests: the
worst gap in `a(x)` is 7 × 10⁻¹⁵ and the bids match exactly. Each replay in the
dashboard also shows the request in a first-price auction: both bids, each
one's win probability, and what a win overpaid.

## Running Locally

### Prerequisites
- Docker + Docker Compose
- Node.js 18+

### 1. Start backend services

```bash
# Clone and enter project
cd rtb-engine

# Copy env file
cp backend/.env.example backend/.env

# Start PostgreSQL + Redis + FastAPI
docker-compose up --build
```

Backend runs at `http://localhost:8000`
API docs at `http://localhost:8000/docs`

### 2. Start frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard runs at `http://localhost:3000`

### 3. Seed demo data

Click **"Seed Demo Data"** in the dashboard header, or:

```bash
curl -X POST http://localhost:8000/api/v1/seed/
```

This creates 8 advertisers, ~20 campaigns, 25 ad slots, and runs 400 simulated auctions
(with simulated clicks) so the A/B comparison has data to show.

### 4. Run a manual auction

```bash
# Get a slot ID first
curl http://localhost:8000/api/v1/publishers/slots

# Submit a bid request
curl -X POST http://localhost:8000/api/v1/auction/bid \
  -H "Content-Type: application/json" \
  -d '{
    "ad_slot_id": "<slot-id-here>",
    "country": "US",
    "device_type": "desktop",
    "page_url": "https://example.com"
  }'
```

### Targeting

A campaign bids only when every targeting rule it sets matches the request. An
empty list means no restriction, so a campaign with no rules bids on
everything — that is how the run-of-network campaigns in the seed backstop the
auction.

| Field | Matches against | Semantics |
|-------|-----------------|-----------|
| `target_countries` | `country` | allow list |
| `target_devices` | `device_type` | allow list |
| `target_categories` | the page's content category | allow list — see below |
| `target_domains` | domain of `page_url` | allow list — contextual buying |
| `blocked_domains` | domain of `page_url` | block list — brand safety |

`page_url` is the page the ad would appear on, OpenRTB's `site.domain`. Two
things follow from how buyers actually think about it:

- **A domain covers its subdomains.** Buying `example.com` also buys
  `sports.example.com` — a buyer means the publisher, not one hostname. The
  suffix test is anchored on a dot, so `example.com` never matches
  `notexample.com`.
- **A block beats an allow.** An advertiser that will not appear beside a
  domain does not appear there for any other reason, so `blocked_domains` is
  checked first and vetoes.

Contextual targeting that cannot be verified is not treated as a match: if the
URL has no readable domain, a campaign with a `target_domains` list does not
bid, while one that only blocks is unaffected.

### Contextual classification

An allow list can only match domains somebody enumerated in advance, so on its
own it makes every unlisted domain behave identically. Real RTB solves this
with categories: the advertiser buys a category and the exchange says what the
page is (OpenRTB's `site.cat` alongside `site.page`).

So `page_url` is classified into one of the six content categories campaigns
already target, and `target_categories` matches **that** rather than the
publisher's own category — a page is what an advertiser is buying, and
publishers have sections. Four signals, most confident first:

| Source | Example | Result |
|--------|---------|--------|
| `section` | `bbc.com/sport/football` | `sports` — a section beats the masthead |
| `known_domain` | `espn.com`, `vogue.com` | `sports`, `fashion` |
| `keyword` | `nigeriasportsdaily.com` | `sports` — inferred from the name |
| `publisher` | `randomblog.xyz` | the publisher's own category, unchanged |

Both the category and the signal that produced it come back on the response,
so an inference is never invisible:

```json
"page_category": "sports",
"page_category_source": "section"
```

Only the first path segment counts as a section, or
`techcrunch.com/2024/01/money-raised` would read as finance. Leading words beat
buried ones, which is what separates `newsportal.co` (news) from
`sportsdailynews.com` (sports) — a plain substring scan gets that pair
backwards.

This is a heuristic over the URL, not a crawler. Fetching an arbitrary page
mid-auction would cost more than the entire latency budget and would have the
auction issuing requests to caller-supplied addresses. A URL that classifies
to nothing — `google.com` is a search engine, not a content category — returns
`unknown` and falls back to the publisher, rather than inventing an answer.

### Why a campaign did not bid

`num_bidders` alone cannot distinguish a rule that fired from a field that is
ignored, so every bid response carries a count of the rules that rejected the
campaigns which did not bid:

```json
"num_bidders": 2,
"excluded": { "domain": 4, "country": 6, "creative": 1, "floor": 2 }
```

Keys are the targeting field names above, plus `creative` (no creative matching
the slot's dimensions) and `floor` (wanted the impression but could not clear
the floor price, from its own cap or what is left of its budget). Campaigns
filtered in SQL for budget, status or flight dates never reach the auction and
are not counted.

Ad slots report `publisher_domain`, since a bid request's `page_url` should be
a page on the site that owns the inventory.

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/auction/bid` | Submit a bid request, run auction |
| `GET` | `/api/v1/auction/recent` | Recent auction results |
| `GET` | `/api/v1/analytics/overview` | KPI aggregates |
| `GET` | `/api/v1/analytics/campaigns` | Per-campaign performance |
| `GET` | `/api/v1/analytics/timeseries` | Auction volume by minute |
| `GET` | `/api/v1/analytics/ab-comparison` | Flat-CPM vs expected-value bidding, per arm, with a 95% CI on the CPC change |
| `GET` | `/api/v1/replay/auction` | Replay a real iPinYou request through the served model, under second and first price (`?clicked=true` for a clicked one) |
| `GET` | `/api/v1/replay/summary` | The served model's version, metrics and offline evaluation |
| `POST` | `/api/v1/openrtb/2.5/bid` | OpenRTB 2.5 bid request; a bid response, or 204 for no bid |
| `GET` | `/api/v1/openrtb/win/{bid_id}?price=` | Win notice (`nurl`): charge the clearing price once |
| `GET` | `/api/v1/openrtb/loss/{bid_id}?reason=` | Loss notice (`lurl`): release the reservation |
| `GET` | `/api/v1/openrtb/click/{bid_id}` | Count a click once and redirect to the creative's landing page |
| `GET` | `/api/v1/openrtb/stats` | Bids by outcome, reserved budget, win rate, eCPC |
| `GET` | `/api/v1/openrtb/log.csv` | Settled exchange bids with model inputs and outcomes: the next training set |
| `POST` | `/api/v1/advertisers/` | Create advertiser |
| `POST` | `/api/v1/campaigns/` | Create campaign |
| `POST` | `/api/v1/publishers/slots` | Create ad slot |
| `POST` | `/api/v1/seed/` | Seed demo data |

## Auction Flow

1. Publisher fires `POST /auction/bid` when a user loads a page
2. Engine fetches all **active campaigns** with remaining daily budget
3. **Eligibility filter** enforces campaign dates, advertiser status, country,
   device, publisher category, budgets, and matching creative dimensions
4. Each eligible campaign bids its `max_cpm_cents`
5. Run the winner's configured first- or second-price auction
6. Convert the clearing CPM to an exact per-impression microdollar charge
7. In **one SQL statement**, reserve that charge against daily and lifetime
   budgets and record the auction result, every bid and the impression — all
   of them land or none do
8. Impression event fires to **Redis Stream** once that write is durable,
   without the response waiting on it
9. Response returns the CPM quote, exact charge, and auction result

### Latency

A warm auction is **two database round trips**: the eligibility read, then the
single charge-and-record statement. Slots are cached in process for 30s
(`SLOT_CACHE_TTL_SECONDS`), and runs in autocommit, so there is no BEGIN or
COMMIT to send. It used to be ten, plus three more for SQLAlchemy's
`pool_pre_ping`, which is replaced by a one-round-trip ping of connections
that have sat idle (`DB_PING_IF_IDLE_SECONDS`).

Measured with backend, client and Postgres 15 all in Docker on one machine,
300 sequential auctions and 600 more at 10 in flight. "Remote" puts a proxy
in front of Postgres that makes each round trip 7.6ms, standing in for a
hosted database:

| | Before | After |
|---|---|---|
| Remote DB, p50 / p95 | 123 / 168 ms | **31 / 40 ms** |
| Remote DB, throughput at 10 in flight | 19 req/s | **51 req/s** |
| Local DB, p50 / p95 | 26 / 42 ms | **15 / 23 ms** |
| Local DB, throughput at 10 in flight | 43 req/s | **87 req/s** |

Every run was also audited row by row: spend equals the sum of impression
charges, no budget is exceeded, each filled auction has one impression and
one bid record per bidder, clearing prices follow the second-price rule, and
each filled auction produced exactly one Redis event. On the deployed stack
it measures 27ms p50 server-side; see Deployment below.

## Data Model

```
Advertiser (1) → (N) Campaign (1) → (N) AdCreative
Publisher  (1) → (N) AdSlot
AdSlot     (1) → (N) AuctionResult
Campaign   (1) → (N) BidRecord
Campaign   (1) → (N) Impression (1) → (N) Click
```

## Deployment

The backend runs on **Render** (a long-lived container) and the frontend on
**Vercel**, both from this one repository.

### Backend — Render

`render.yaml` provisions a web service in the **Frankfurt** region so it sits
next to the Supabase database (`eu-central-1`). Colocation matters more than
anything in the code: an auction makes two sequential database round trips,
and through Supabase's pooler each statement is a prepare plus an execute.
With the backend in Frankfurt and the database in Oregon, the same code
measured 1.3s per auction.

Measured on the deployed stack (Render Frankfurt, Supabase `eu-central-1`),
100 warm auctions: **27ms p50 / 54ms p95 server-side**, split evenly between
the eligibility read and the charge-and-record write. Before the round-trip
work above it was ~150ms. The connection pool
is warmed at boot so connection setup is paid once rather than inside the first
visitor's auction. Free-tier containers still spin down when idle, and a cold
start adds several seconds on top of that.

Set `AUCTION_PROFILE=1` to get a per-phase millisecond breakdown on every bid
response; it is always written to the logs regardless.

| Setting | Value |
|---------|-------|
| Root Directory | `backend` |
| Build | `pip install -r requirements.txt` |
| Start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Region | `frankfurt` — must match your database's region |

Environment variables:

| Variable | Notes |
|----------|-------|
| `DATABASE_URL` | Postgres connection string (Supabase). Port `5432` is fine on a long-lived host — the pool stays open, so serverless pooler concerns don't apply. |
| `REDIS_URL` | Upstash Redis. Use the `rediss://` (TLS) URL. Optional — the auction degrades gracefully without it. |
| `ENVIRONMENT` | `production` |

Verify with `curl https://<backend>/health` — it reports database connectivity,
so a bad `DATABASE_URL` shows up immediately instead of as an empty dashboard.

> **Free-tier note:** Render spins the service down after ~15 minutes idle, and the
> next request pays a ~50s cold start. Use a paid instance or a keep-warm ping if
> you need the demo to answer instantly.

### Frontend — Vercel

| Setting | Value |
|---------|-------|
| Root Directory | `frontend` |
| Build Command | `npm run build` (auto-detected) |

Environment variables:

| Variable | Notes |
|----------|-------|
| `VITE_API_URL` | **Required.** `https://<render-backend>/api/v1` — including the `/api/v1` suffix. Vite inlines this at build time, so redeploy after changing it. |

After both are up, run `POST /api/v1/seed/` once to populate the dashboard (the A/B
panel is empty until there's data).

`backend/vercel.json` and `backend/api/index.py` remain for an optional serverless
deployment but are unused on Render.

### Demo behaviour

- **Seed** wipes and rebuilds the dataset, so repeated clicks by visitors can't
  pile up duplicates.
- **Budgets roll over automatically.** Daily spend resets when the UTC date
  changes; lifetime spend never resets, so neither limit can be bypassed by
  repeatedly running auctions or reactivating a campaign.
