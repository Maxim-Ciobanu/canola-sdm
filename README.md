# Canola S&D Model

A supply and demand modelling pipeline for Western Canadian canola, with a Southern Alberta basis forecast as the primary output. Ingests public data from Statistics Canada, AAFC, the Canadian Grain Commission, Alberta crop reports, and PDQ cash bids to produce a weekly one-pager and actionable basis signal.

---

## What it does

- Maintains a live Western Canada canola balance sheet (production, crush, exports, ending stocks)
- Computes a stocks-to-use tightness metric updated monthly
- Tracks Southern Alberta spot and forward basis using PDQ delivery point bids vs ICE canola futures
- Models basis seasonality and adjusts for current balance sheet conditions
- Runs scenario analysis (yield shock, export disruption, logistics stress) to produce an implied basis range
- Outputs a weekly one-pager: 3 bullets + 1–2 charts

---

## Project status

> Currently in **Phase 1** — scoping, data source audit, and schema design.

| Phase | Status | Description |
|---|---|---|
| Phase 1 | 🔄 In progress | Scope, schema, environment setup |
| Phase 2 | ⏳ Not started | Working balance sheet, tightness metric, historical basis |
| Phase 3 | ⏳ Not started | Basis model, scenario module, one-pager |
| Phase 4 | ⏳ Not started | Backtest framework, stochastic layer |

---

## Data sources

All sources are public and free.

| Source | What it provides | Frequency |
|---|---|---|
| [Statistics Canada](https://www150.statcan.gc.ca/n1/en/subjects/agriculture_and_food/crop_production) | Historical production and yield | Monthly / Annual |
| [AAFC Field Crop Outlook](https://agriculture.canada.ca/en/sector/crops/reports-statistics/canada-outlook-principal-field-crops-2026-03-18) | Monthly balance sheet | Monthly |
| [Alberta Crop Reports](https://www.alberta.ca/alberta-crop-reports) | In-season yield risk, seeding and harvest progress | Weekly (in-season) |
| [Canadian Grain Commission](https://www.grainscanada.gc.ca/en/grain-research/statistics/grain-statistics-weekly/) | Weekly grain flows — receipts, exports, elevator stocks | Weekly |
| [PDQ Alberta Grains](https://www.pdqinfo.ca/) | Southern AB spot and forward cash bids | Daily |
| Yahoo Finance (`RS=F`) | ICE canola nearby futures via `yfinance` | Daily |

---

## Repo structure

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

## Getting started

### Prerequisites

- Python 3.10+
- SQLite (local dev) or PostgreSQL (shared/production)

### Installation

```bash
git clone git@github.com:Maxim-Ciobanu/canola-sdm.git
cd canola-sdm
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Requirements

```
pandas
requests
yfinance
sqlalchemy
matplotlib
python-dotenv
```

### Run the smoke test

Before doing anything else, confirm every data source is reachable and the schema round-trips correctly:

```bash
python tests/smoke_test.py
```

All six sources should return at least one row and write successfully to the local database.

---

## Key concepts

**Basis** — local Southern AB cash price minus ICE canola nearby futures (CAD/tonne). This is the primary output of the model, not outright price.

**Stocks-to-use** — ending stocks divided by total use (crush + exports). The core tightness metric. Lower = tighter market = typically stronger basis.

**Crop year** — August 1 through July 31. All balance sheet data uses this convention. "2024/25" means Aug 2024 – Jul 2025.

**Vintage date** — the date a report was published by AAFC or StatsCan, stored separately from the date your system pulled it. Both are required for backtesting.

---

## Schema overview

Four core tables. Full column definitions are in [`docs/project_reference.md`](docs/project_reference.md).

| Table | Grain | Primary key |
|---|---|---|
| `balance_sheet` | Monthly | `crop_year`, `month`, `source`, `report_date` |
| `prices` | Daily | `date`, `location`, `contract_month` |
| `crop_conditions` | Weekly (in-season) | `report_date`, `region`, `crop` |
| `cgc_flows` | Weekly | `week_ending` |

---
