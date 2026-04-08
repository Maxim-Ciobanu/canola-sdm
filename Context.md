# Canola Supply & Demand Model — Project Reference Document

> **Purpose:** This document is a full context dump for any collaborator or AI assistant picking up this project. It covers the goal, background decisions, architecture, data sources, schema, and the granular Phase 1 plan.

---

## 1. Project Goal

Build a **supply and demand (S&D) model for Western Canadian canola** that outputs an actionable basis forecast for Southern Alberta cash pricing.

The goal is **not** to predict outright futures prices — futures markets already price in global expectations. The goal is to model **basis** (local cash price minus ICE canola futures), which reflects:
- Local supply tightness (stocks-to-use)
- Regional logistics and delivery pace
- Seasonal demand patterns

### What "actionable" means
By the end of the project, the model should be able to say:
- "Supply tightness moved from neutral to tight because yield expectations fell and deliveries slowed"
- "Here are 3 scenarios with probabilities and the implied basis range over the next 8 weeks"
- "When stocks-to-use was at this level historically, basis moved X over the next Y weeks"

---

## 2. Commodity and Scope

**Commodity:** Canola  
**Why canola:** Clean ICE futures anchor, liquid basis history, strong Western Canadian production, rich AAFC/CGC data coverage.

**Balance sheet scope:** Western Canada  
**Pricing scope:** Southern Alberta — specific PDQ delivery points (e.g. Lethbridge, Picture Butte). Start with 2–3 points, do not expand until Phase 3.

**Crop year convention:** August–July (not calendar year). Every table and column must reflect this. e.g. "2024/25" = Aug 2024 – Jul 2025.

**Units:** Kilotonnes (kt) throughout the balance sheet. Do not mix with bushels or tonnes without explicit conversion columns.

---

## 3. Why Basis, Not Price

Outright canola price prediction is extremely difficult — the futures market is already a consensus of global supply/demand expectations. Basis is more tractable because it reflects **local and regional** factors:

| Factor | How it affects basis |
|---|---|
| Tight local stocks | Basis strengthens (cash premium over futures) |
| Slow export deliveries | Basis weakens |
| Harvest pressure | Basis weakens seasonally |
| Crush demand surge | Basis strengthens |
| Logistics disruption (rail, port) | Basis weakens |

Basis also has a strong **seasonal pattern** which makes it modelable with regression + seasonality even before adding balance sheet inputs.

---

## 4. Data Sources

All sources are public and free.

| Source | What it provides | Frequency | URL |
|---|---|---|---|
| Statistics Canada | Historical production and yield by crop | Monthly/Annual | https://www150.statcan.gc.ca/n1/en/subjects/agriculture_and_food/crop_production |
| AAFC Field Crop Outlook | Monthly balance sheet (production, crush, exports, ending stocks) | Monthly | https://agriculture.canada.ca/en/sector/crops/reports-statistics/canada-outlook-principal-field-crops-2026-03-18 |
| Alberta Weekly Crop Reports | In-season yield risk, seeding/harvest progress, crop conditions | Weekly (in-season) | https://www.alberta.ca/alberta-crop-reports |
| Canadian Grain Commission | Weekly grain flows — receipts, exports, elevator stocks | Weekly | https://www.grainscanada.gc.ca/en/grain-research/statistics/grain-statistics-weekly/ |
| PDQ Alberta Grains | Regional spot and forward cash bids, historical basis charts | Daily | https://www.pdqinfo.ca/ |
| Yahoo Finance | ICE canola futures (ticker: RS=F) via yfinance | Daily | https://finance.yahoo.com |

### Important data source notes
- **AAFC** may publish some reports as PDF only — if so, decide upfront whether to manually enter data for Phase 2 and automate later, or build a parser immediately.
- **CGC** weekly data needs to be checked for machine-readable format (CSV/Excel vs PDF).
- **Alberta crop reports** can change format between years — historical parsing may be inconsistent.
- **ICE canola futures** require handling contract rolls (nearby contract changes monthly). Decide in Phase 1 whether to track nearby only or build a continuous back-adjusted series.
- **PDQ** provides forward curves (Nov, Jan, Mar contracts) — capture these from day one even if you don't model forward basis immediately.

---

## 5. Pipeline Architecture

### 5.1 Ingestion
- Pull each dataset on a schedule: monthly for StatsCan/AAFC, weekly for AB crop reports and CGC, daily for prices
- **Save every raw pull to `data/raw/` with a timestamp in the filename** — non-negotiable, required for backtesting and audit trail
- Raw files are never modified after saving

### 5.2 Normalize
Transform raw pulls into consistent schema tables:
- Balance sheet (production, imports, exports, crush, ending stocks)
- Prices (spot, futures, basis)
- Crop conditions (seeding/harvest progress, condition ratings)
- CGC flows (receipts, exports, cumulative crop-year totals)
- Metadata (source, pull timestamp, report vintage date)

### 5.3 Model Layer
- **Baseline forecast:** 12-month balance sheet projection
- **Tightness metric:** stocks-to-use ratio — the core signal
- **Basis model:** regression + seasonality, adjusted for balance sheet conditions
- **Scenario analysis:** yield shock, export shock, logistics disruption — implied basis range per scenario
- **Stochastic layer (Phase 4 only):** probabilistic model using distribution of historical outcomes rather than discrete scenarios

### 5.4 Outputs
- **Weekly one-pager:** 3 bullets (supply, demand, implication) + 1–2 charts (basis chart, balance table actual vs forecast)
- **Dashboard (later):** basis chart, balance table, what changed since last update, scenario panel
- **Backtest output:** when tightness was X historically, what happened to basis over the next Y weeks

---

## 6. Key Modelling Decisions

### Basis model approach
- Start with **regression + seasonality**: fit a seasonal basis curve to history, then adjust based on current stocks-to-use level
- Simple and interpretable — you can explain every output
- Validate against known events (e.g. CN rail strike 2019, port disruptions) before adding complexity

### Scenario analysis vs stochastic
- **Phase 3:** scenario analysis — discrete shocks (drought/yield drop, export embargo, logistics disruption) propagated through the monthly balance sheet
- **Phase 4:** stochastic layer — probability distribution over outcomes using historical variance, avoiding nested scenario loops
- The stochastic approach is more computationally efficient than exhaustive scenarios but much harder to explain — build scenarios first, add stochastic only once the base model is trusted

### Transportation and logistics
- Do not model the rail/freight network directly — this adds major complexity
- Use **CGC weekly flow data as a proxy**: if deliveries are running below the pace needed to meet export commitments, that is a logistics stress signal
- "What if transportation costs triple" type scenarios can be modelled later as a lever on the export row of the balance sheet

### Time dimensions in the model
The model has three time dimensions — all are already implicit in the design:
1. **Monthly balance sheet** — historical series + 12-month forward projection, each month's ending stocks feeds the next month's beginning stocks
2. **Weekly basis observations** — 4–8 week forward forecast, seasonal curve adjusted by balance sheet conditions
3. **Backtest layer** — historical lookback when conditions were similar to today

Multi-period propagation means a yield shock in August cascades forward month-by-month through the balance sheet. The data layer must support clean vintage tracking (what did the model think at any point in time) — this is why raw pulls with timestamps are mandatory.

---

## 7. Repo structure


**Tech stack recommendation:** Python (pandas, requests, yfinance) + SQLite for local dev, PostgreSQL when the team needs shared access.

**Repo structure:**
```
canola-sdm/
├── ingest/
│   ├── statscan.py
│   ├── aafc.py
│   ├── ab_crop_reports.py
│   ├── cgc.py
│   ├── pdq.py
│   └── futures.py
├── normalize/
│   └── transform.py
├── models/
│   ├── balance_sheet.py
│   ├── basis_model.py
│   └── scenarios.py
├── outputs/
│   └── one_pager.py
├── data/
│   ├── raw/
│   └── db/
├── docs/
│   └── project_reference.md
├── tests/
│   └── smoke_test.py
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 8. Database Schema

### Tables
 
1. [`balance_sheet`](#1-balance_sheet) — monthly S&D estimates by vintage
2. [`prices`](#2-prices) — daily spot and forward cash bids + futures
3. [`crop_conditions`](#3-crop_conditions) — weekly in-season crop condition ratings
4. [`cgc_flows`](#4-cgc_flows) — weekly grain movement and elevator stocks
5. [`scenario_runs`](#5-scenario_runs) — scenario metadata and assumptions log
6. [`scenario_outputs`](#6-scenario_outputs) — implied balance sheet and basis per scenario
7. [`sources`](#7-sources) — source registry and ingestion config

- For more details on the schema design and column definitions, see the [project reference document](docs/project_reference.md).

---

## 9. Overall Phased Plan

| Phase | Timeline | Deliverable | Gate |
|---|---|---|---|
| **Phase 1** | 1–2 weeks (flexible) | Scope agreed, schema signed off, env running | Schema review complete, smoke test passes |
| **Phase 2** | 2 weeks | Working balance sheet, tightness metric, historical basis series | Usable even if project stopped here |
| **Phase 3** | Weeks 3–6 | Basis model (regression + seasonality), scenario module (optional if data issues), one-pager | One-pager ships |
| **Phase 4** | After Phase 3 stable | Backtest framework, scenario module (if deferred), stochastic layer | Backtest validates on at least one real historical event |

**Critical sequencing rules:**
- Phase 1 is time-flexible — schema sign-off gates Phase 2, not a calendar date
- Output format (one-pager vs dashboard) must be decided in Phase 1 — it shapes all downstream work
- Scenario module is optional in Phase 3 — defer to Phase 4 if data quality issues appear
- Do not build the stochastic layer before the base model is trusted and backtested

---

## 10. Phase 1 — Granular Task Plan

**Phase 1 is done when the schema is agreed and data sources are confirmed pulling — not on a fixed calendar date.**

---

### Days 1–2: Scope decisions

> Agree on all of these before touching any data or writing any code.

- [ ] **Confirm canola as the commodity**
  - Note: crop year runs Aug–Jul, not calendar year — schema must reflect this from day one

- [ ] **Decide geographic scope**
  - Recommended: Western Canada balance sheet + Southern AB as primary pricing location
  - Pick 2–3 PDQ delivery points now (e.g. Lethbridge, Picture Butte) — do not expand until Phase 3

- [ ] **Decide primary output format**
  - Recommended: weekly one-pager first (faster to ship, forces opinionated choices)
  - This decision shapes every downstream phase — make it explicitly, not by default

- [ ] **Assign a data source owner for each source**
  - One person per source responsible for confirming it pulls and watching for format changes
  - Sources: StatsCan, AAFC, AB crop reports, CGC, PDQ, Yahoo Finance

---

### Days 2–4: Data source audit

> Confirm every source actually works before designing schema around it.

- [ ] **StatsCan crop production tables**
  - Pull table 32-10-0359-01 (field crop reporting)
  - Confirm canola rows present, note column names, check historical depth (target 10+ years)
  - Flag any gaps in the series
  - URL: https://www150.statcan.gc.ca/n1/en/subjects/agriculture_and_food/crop_production

- [ ] **AAFC field crop outlook — monthly balance sheet**
  - Download the most recent report
  - Record exact columns in the canola balance sheet table
  - Note the vintage/publication date — critical for metadata table
  - Check whether historical PDFs are archived on the site
  - URL: https://agriculture.canada.ca/en/sector/crops/reports-statistics/canada-outlook-principal-field-crops-2026-03-18

- [ ] **Alberta weekly crop reports**
  - Confirm URL and download schedule (usually Tuesdays in-season)
  - Note what is structured vs. free text
  - Check whether historical reports are archived and in consistent format — format changes between years are common
  - URL: https://www.alberta.ca/alberta-crop-reports

- [ ] **CGC grain statistics weekly**
  - Pull the latest weekly report
  - Confirm canola is broken out separately
  - Confirm whether data is machine-readable (CSV/Excel) or PDF only — this changes ingestion approach significantly
  - URL: https://www.grainscanada.gc.ca/en/grain-research/statistics/grain-statistics-weekly/

- [ ] **PDQ Alberta Grains — spot and forward bids**
  - Confirm access to historical spot bids for Southern AB delivery points
  - Note how far back history goes, whether bids are end-of-day or intraday
  - Check if forward curves (Nov, Jan, Mar contracts) are available
  - This is the primary basis input — depth of history matters a lot here
  - URL: https://www.pdqinfo.ca/

- [ ] **ICE canola futures via Yahoo Finance**
  - Test pulling RS=F (canola nearby) via yfinance
  - Confirm daily OHLCV is available
  - Decide: track nearby contract only, or build a continuous back-adjusted series (handle monthly contract rolls)

- [ ] **Document any source that is PDF-only or inconsistently formatted**
  - If AAFC or CGC is PDF-only, decide: manual entry for Phase 2 and automate later, or build a parser upfront
  - This decision affects Phase 2 timeline more than anything else

---

### Days 4–6: Schema design

> The deliverable that gates Phase 2. Every team member must review and agree.

- [ ] **Define and agree on the `balance_sheet` table** (see Section 7 above)
  - Key decisions: crop year convention, units (kilotonnes), how to handle AAFC vs StatsCan vintage differences

- [ ] **Define and agree on the `prices` table** (see Section 7 above)
  - Key decisions: how to handle contract rolls, whether to store forward curve bids

- [ ] **Define and agree on the `crop_conditions` table** (see Section 7 above)
  - Key decisions: numeric vs text condition ratings, which AB regions to track

- [ ] **Define and agree on the `cgc_flows` table** (see Section 7 above)

- [ ] **Schema review meeting — all team members**
  - Walk through every table together
  - Confirm: units consistent everywhere? Crop year convention documented? `report_date` vintage column exists on every table?
  - This is the gate — do not start ingestion code until this is signed off

---

### Days 5–7: Tech stack and environment

- [ ] **Agree on stack and set up the repo**
  - Recommended: Python + SQLite (local dev) → PostgreSQL (shared)
  - Libraries: pandas, requests, yfinance, sqlalchemy
  - Folder structure: `ingest/ normalize/ models/ outputs/ data/raw/ data/db/`
  - Every raw pull saved to `data/raw/` with timestamp in filename — mandatory

- [ ] **Write and run one end-to-end smoke test**
  - Pull one data point from each source
  - Write it into the schema tables
  - Confirm it round-trips correctly
  - Does not need to be clean code — just needs to work once end-to-end
  - If this fails, Phase 2 cannot start

---

### Phase 1 Exit Gate

All of the following must be true before Phase 2 begins:

- [ ] Commodity and geographic scope agreed and written down
- [ ] Primary output format decided (one-pager or dashboard)
- [ ] Every data source confirmed pulling — machine-readable or manual workaround agreed
- [ ] All 4 tables (balance sheet, prices, crop conditions, CGC flows) reviewed and signed off by team
- [ ] Crop year convention (Aug–Jul) and units (kilotonnes) documented and agreed
- [ ] Repo set up, folder structure in place
- [ ] One smoke test passes end-to-end
- [ ] Each data source has an assigned owner

---

## 11. Things Intentionally Deferred

These are explicitly out of scope until later phases. Do not add them early.

| Item | Why deferred | When to revisit |
|---|---|---|
| Transportation cost modelling | CGC flow data is a sufficient proxy for Phase 2–3 | Phase 4 if logistics proves to be a dominant basis driver |
| Consumption timing model | Domestic use is stable enough to model as a seasonal curve | Phase 4 |
| Production timing model | Crop reports provide seeding/harvest pace as a signal already | Phase 3 (in-season adjustment) |
| Stochastic/probabilistic layer | Needs trusted base model first; scenario analysis builds intuition before going probabilistic | Phase 4 only |
| Multiple commodities | Canola first — add feed barley or wheat only after canola model is validated | Post Phase 4 |
| Full dashboard | One-pager is faster to ship and more opinionated — forces good decisions | Phase 3–4 |
| Forward basis modelling | Nearby basis first — forward curve modelling requires more PDQ data validation | Phase 3 |

---

## 12. Glossary

| Term | Definition |
|---|---|
| **Basis** | Local cash price minus ICE canola futures price (in CAD/tonne). Negative basis is normal (cash below futures). Strengthening basis = cash rising relative to futures. |
| **Stocks-to-use** | Ending stocks divided by total use (crush + exports). Primary tightness metric. Lower = tighter = typically stronger basis. |
| **Crop year** | Aug 1 – Jul 31 for canola. "2024/25" means Aug 2024 through Jul 2025. |
| **Vintage / report_date** | The date a report was published by AAFC or StatsCan. Different from the date your system pulled it. Both must be stored. |
| **Nearby contract** | The closest-to-expiry ICE canola futures contract. Used as the futures benchmark in basis calculation. Rolls monthly. |
| **Basis shock scenario** | A discrete input change (e.g. yield drops 20%, exports fall 15%) propagated forward through the monthly balance sheet to produce an implied basis range. |
| **One-pager** | The primary output: 3 bullets (supply signal, demand signal, implication for basis) + 1–2 charts. Produced weekly. |
| **PDQ** | Procom Dealer Quotations — free web tool showing Alberta grain cash bids by delivery point and contract month. |
| **CGC** | Canadian Grain Commission — federal regulator that publishes weekly grain flow statistics. |
| **AAFC** | Agriculture and Agri-Food Canada — publishes the monthly principal field crops balance sheet outlook. |

