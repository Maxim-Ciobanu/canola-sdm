"""
Create the SQLite database with the full schema for the AB canola model.

Three tables:
  - statscan_sd: triannual provincial snapshots from StatsCan 32-10-0015-01
  - cgc_weekly: weekly AB canola data from CGC GSW (Primary + Process tabs)
  - nowcast: computed weekly balance sheet (populated by a separate script)

Run this ONCE. To start over, delete data/db/canola.sqlite first.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "db" / "canola.sqlite"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(DB_PATH)
cur = con.cursor()

# Table 1: StatsCan triannual balance sheet snapshots.
cur.execute("""
CREATE TABLE IF NOT EXISTS statscan_sd (
    crop_year                TEXT NOT NULL,    -- e.g. "2023/24"
    snapshot_month           TEXT NOT NULL,    -- "March" | "July" | "December"
    snapshot_date            DATE NOT NULL,    -- 2024-03-31, 2024-07-31, 2024-12-31
    geography                TEXT NOT NULL,    -- "Alberta"
    crop                     TEXT NOT NULL,    -- "Canola"
    total_supplies_kt        REAL,
    beginning_stocks_kt      REAL,
    production_kt            REAL,
    total_disposition_kt     REAL,
    deliveries_kt            REAL,
    seed_requirements_kt     REAL,
    ending_stocks_kt         REAL,
    feed_waste_dockage_kt    REAL,
    source_table             TEXT NOT NULL,    -- "32-10-0015-01"
    report_date              DATE NOT NULL,    -- vintage (StatsCan release date)
    pulled_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (crop_year, snapshot_month, geography, crop, report_date)
);
""")

# Table 2: CGC weekly Alberta data, pulling from Primary AND Process tabs.
# total_* columns are computed at insert time (primary + process).
cur.execute("""
CREATE TABLE IF NOT EXISTS cgc_weekly (
    week_number                       INTEGER NOT NULL,   -- 1=first week of August
    week_ending                       DATE NOT NULL,
    crop_year                         TEXT NOT NULL,
    geography                         TEXT NOT NULL,      -- "Alberta"
    crop                              TEXT NOT NULL,      -- "Canola"

    -- Primary elevator channel
    producer_deliveries_weekly_kt     REAL,
    producer_deliveries_cytd_kt       REAL,
    primary_shipments_weekly_kt       REAL,
    primary_shipments_cytd_kt         REAL,
    primary_elevator_stocks_kt        REAL,
    condo_storage_kt                  REAL,

    -- Process elevator channel (canola crush plants etc.)
    process_deliveries_weekly_kt      REAL,
    process_deliveries_cytd_kt        REAL,
    process_elevator_stocks_kt        REAL,

    -- Computed totals (primary + process)
    total_deliveries_weekly_kt        REAL,
    total_deliveries_cytd_kt          REAL,
    total_visible_stocks_kt           REAL,                -- primary + process + condo

    source_file                       TEXT NOT NULL,
    pulled_at                         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (week_ending, geography, crop)
);
""")

# Table 3: Computed weekly nowcast. Empty until a separate script populates it.
cur.execute("""
CREATE TABLE IF NOT EXISTS nowcast (
    week_ending                  DATE NOT NULL,
    crop_year                    TEXT NOT NULL,
    geography                    TEXT NOT NULL,
    crop                         TEXT NOT NULL,
    total_supplies_kt            REAL,    -- from statscan_sd, fixed Aug 1
    deliveries_cytd_kt           REAL,    -- from cgc_weekly
    estimated_total_stocks_kt    REAL,    -- supplies - cumulative disposition
    visible_commercial_stocks_kt REAL,    -- primary + process + condo
    implied_on_farm_stocks_kt    REAL,    -- estimated_total - visible
    computed_at                  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (week_ending, geography, crop)
);
""")

con.commit()

# Confirm what got created
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
tables = [r[0] for r in cur.fetchall()]
print(f"Database created at: {DB_PATH}")
print(f"Tables: {tables}")

# Show column counts for sanity
for tbl in tables:
    cur.execute(f"SELECT COUNT(*) FROM pragma_table_info('{tbl}')")
    n_cols = cur.fetchone()[0]
    print(f"  {tbl}: {n_cols} columns")

con.close()
