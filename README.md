# Alberta Canola S&D Model

Smallest-viable weekly supply-and-demand nowcast for Alberta canola, with Southern Alberta basis as the output.

---

## The whole thing in one paragraph

Statistics Canada publishes an Alberta canola balance sheet three times a year. The CGC publishes Alberta producer deliveries every week. The CGC weekly number is literally the same series as the StatsCan "deliveries" line, just at higher frequency. Subtract cumulative weekly deliveries from total supply and you have an estimated stocks number for every week of the crop year, recalibrated against StatsCan reality four times a year. Then compare to Southern AB cash bids from PDQ to get a basis signal.

---

## Three data sources. That's it.

| # | Source | What it gives us | Frequency |
|---|---|---|---|
| 1 | [StatsCan 32-10-0017](https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3210001701) (Alberta filter) | Full AB balance sheet: beginning stocks, production, deliveries, ending stocks, feed/waste | Triannual (Dec/Mar/Jul) |
| 2 | [CGC GSW](https://www.grainscanada.gc.ca/en/grain-research/statistics/grain-statistics-weekly/) (Alberta tab) | Weekly + CYTD producer deliveries, primary elevator stocks, condo storage | Weekly |
| 3 | [PDQ Alberta Grains](https://www.pdqinfo.ca/) | Southern AB spot cash bids | Daily |

That is the entire data layer. Other sources (AAFC, AB crop reports, ICE futures, etc.) are explicitly not in scope until the base model works.

---

## The core identity

```
Total Supplies (fixed Aug 1 from StatsCan)
    = Beginning Stocks (prior year Jul 31)
    + Production

Estimated AB Stocks at week N
    = Total Supplies
    − Cumulative Producer Deliveries at week N   (CGC weekly, AB tab)
    − Pro-rated feed / waste / dockage           (from StatsCan annual estimate)

Implied On-Farm Stocks at week N
    = Estimated AB Stocks
    − Primary Elevator Stocks                    (CGC weekly, AB tab)
    − Condo Storage                              (CGC weekly, AB tab)
```

When StatsCan releases the next triannual snapshot, compare estimate to actual and adjust the feed/waste pro-rating.

---

## Output

A single number that moves every week: **implied on-farm canola stocks in Alberta.** Combined with the Southern AB basis from PDQ, that's the actionable signal.

Bonus signal: weekly **producer deliveries vs primary elevator shipments**. When shipments lag deliveries, primaries are filling up — bearish for basis.

---

## Repo

```
canola-sdm/
├── ingest/
│   ├── statscan.py        # 32-10-0017 AB filter, triannual
│   ├── cgc.py             # GSW AB tab, weekly
│   └── pdq.py             # Southern AB spot bids, daily
├── nowcast.py             # The identity above
├── data/
│   ├── raw/               # Every pull saved with timestamp
│   └── db/                # SQLite
├── README.md
└── requirements.txt
```

Four scripts. One database file. No framework.

---

## Schema — four tables

```sql
-- 1. Triannual truth anchor
CREATE TABLE statscan_sd (
    crop_year               TEXT,        -- "2024/25"
    snapshot_date           DATE,        -- 2024-12-31, 2025-03-31, 2025-07-31
    beginning_stocks_kt     REAL,
    production_kt           REAL,
    total_supplies_kt       REAL,
    deliveries_kt           REAL,
    ending_stocks_kt        REAL,
    feed_waste_dockage_kt   REAL,
    report_date             DATE,        -- StatsCan publication date (vintage)
    PRIMARY KEY (crop_year, snapshot_date, report_date)
);

-- 2. Weekly heartbeat
CREATE TABLE cgc_weekly (
    week_ending                     DATE PRIMARY KEY,
    crop_year                       TEXT,
    producer_deliveries_weekly_kt   REAL,
    producer_deliveries_cytd_kt     REAL,
    primary_shipments_weekly_kt     REAL,
    primary_elevator_stocks_kt      REAL,
    condo_storage_kt                REAL
);

-- 3. Basis input
CREATE TABLE prices (
    observation_date  DATE,
    zone              TEXT,        -- "Southern AB"
    spot_cad          REAL,
    PRIMARY KEY (observation_date, zone)
);

-- 4. Computed weekly nowcast
CREATE TABLE nowcast (
    week_ending                   DATE PRIMARY KEY,
    crop_year                     TEXT,
    estimated_total_stocks_kt     REAL,
    implied_on_farm_stocks_kt     REAL,
    delivery_pace_vs_prior_year   REAL
);
```

---

## Getting started

```bash
git clone <repo>
cd canola-sdm
python -m venv venv && source venv/bin/activate
pip install pandas requests openpyxl beautifulsoup4 sqlalchemy
```

Pull the three sources, run `nowcast.py`, look at the latest row of the `nowcast` table.

---

## Phases

| Phase | Deliverable |
|---|---|
| **Phase 1** | The four tables above. Three ingest scripts pulling. `nowcast.py` computing the identity for the current crop year. |
| **Phase 2** | Backfill historical seasons. Plot implied on-farm stocks vs Southern AB basis. Eyeball the relationship. |
| **Phase 3** | Only after Phases 1–2 work: revisit whether to add AAFC, futures, crop reports, anything else. |

Do not add a fourth data source until the three-source version produces a chart you trust.

---

## Things explicitly not in scope

- AAFC national outlook — useful later, not needed for the AB nowcast
- StatsCan production table (32-10-0359) — production is already inside 32-10-0017
- CGC GDPP shipping-point detail — provincial AB total is enough to start
- AB crop reports — yield-signal layer, Phase 3 at earliest
- ICE canola futures — basis ≈ spot − futures, but spot alone is enough to see the pattern at first
- Scenario engine, stochastic layer, backtest framework, one-pager generator — all later
- Multi-province, multi-crop — Alberta canola only
