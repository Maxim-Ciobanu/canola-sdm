"""
Load PDQ canola cash bids into the prices table.

PDQ exports a forward-curve CSV: each (IMPORT DATE, ZONE) has multiple rows,
one per delivery month.  We extract the SPOT month (the row whose delivery
month matches the calendar month of IMPORT DATE) and store:

  - observation_date  = IMPORT DATE
  - zone              = mapped zone name  ("S ALTA" -> "Southern AB", etc.)
  - delivery_month    = the MONTH column  (e.g. "JAN '24")
  - cash_per_bu       = CASH  (CAD/bu)
  - basis_per_bu      = BASIS (CAD/bu, typically negative = under futures)
  - futures_contract  = FUTURES MONTH  (e.g. "RSH24")

Units are $/bushel as published by PDQ.  1 tonne canola ≈ 44.092 bu,
so cash_per_bu * 44.092 ≈ CAD/tonne — but we store the raw quote.

Usage:
    python3 ingest/load_pdq.py                       # loads all CSVs in data/raw/pdq/
    python3 ingest/load_pdq.py path/to/export.csv     # loads one specific file

Re-running is safe — rows are upserted by (observation_date, zone, delivery_month).
"""

import csv
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PDQ_DIR = Path(__file__).parent.parent / "data" / "raw" / "pdq"
DB_PATH = Path(__file__).parent.parent / "data" / "db" / "canola.sqlite"

ZONE_MAP = {
    "S ALTA":  "Southern AB",
    "N ALTA":  "Northern AB",
    "PEACE":   "Peace River",
}

MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_delivery_month(label):
    """Parse "JAN '24" -> (2024, 1). Returns None on failure."""
    m = re.match(r"([A-Z]{3})\s+'(\d{2})", label.strip())
    if not m:
        return None
    mon = MONTH_ABBR.get(m.group(1))
    yr = 2000 + int(m.group(2))
    if mon is None:
        return None
    return (yr, mon)


def is_spot_month(delivery_label, obs_date):
    """True if the delivery month matches the calendar month of observation."""
    parsed = parse_delivery_month(delivery_label)
    if parsed is None:
        return False
    return parsed == (obs_date.year, obs_date.month)


def safe_float(val):
    """Convert to float, returning None for '-' or empty."""
    if val is None:
        return None
    val = val.strip()
    if val in ("", "-"):
        return None
    try:
        return float(val.replace(",", ""))
    except ValueError:
        return None


def parse_pdq_csv(path):
    """Read one PDQ export CSV. Yields one dict per (date, zone) spot-month row."""
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_zone = row.get("ZONE", "").strip()
            zone = ZONE_MAP.get(raw_zone)
            if zone is None:
                continue

            raw_date = row.get("IMPORT DATE", "").strip()
            if not raw_date:
                continue
            try:
                obs_date = datetime.strptime(raw_date, "%m/%d/%Y").date()
            except ValueError:
                continue

            delivery_month = row.get("MONTH", "").strip()
            if not is_spot_month(delivery_month, obs_date):
                continue

            cash = safe_float(row.get("CASH"))
            if cash is None:
                continue  # no point storing a row with no price

            yield {
                "observation_date": obs_date.isoformat(),
                "zone":             zone,
                "delivery_month":   delivery_month,
                "cash_per_bu":      cash,
                "cash_change":      safe_float(row.get("CASH CHANGE")),
                "basis_per_bu":     safe_float(row.get("BASIS")),
                "basis_change":     safe_float(row.get("BASIS CHANGE")),
                "futures_contract": (row.get("FUTURES MONTH") or "").strip() or None,
            }


def ensure_prices_table(con):
    """Create the prices table if it doesn't exist."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            observation_date   DATE    NOT NULL,
            zone               TEXT    NOT NULL,   -- "Southern AB", "Northern AB", "Peace River"
            delivery_month     TEXT,               -- "JAN '24" (spot month label)
            cash_per_bu        REAL,               -- CAD/bu spot bid
            cash_change        REAL,               -- day-over-day change
            basis_per_bu       REAL,               -- CAD/bu basis (spot - futures)
            basis_change       REAL,               -- day-over-day basis change
            futures_contract   TEXT,               -- "RSH24" etc.
            source             TEXT    NOT NULL DEFAULT 'PDQ',
            pulled_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (observation_date, zone)
        );
    """)
    con.commit()


def main():
    # Determine input files
    if len(sys.argv) > 1:
        files = [Path(sys.argv[1])]
    else:
        if not PDQ_DIR.exists():
            print(f"ERROR: directory not found: {PDQ_DIR}")
            print("Either drop PDQ CSVs into data/raw/pdq/ or pass a file path as argument.")
            sys.exit(1)
        files = sorted(PDQ_DIR.glob("*.csv"))
        if not files:
            print(f"No .csv files in {PDQ_DIR}.")
            sys.exit(0)

    print(f"Found {len(files)} file(s) to process\n")

    con = sqlite3.connect(DB_PATH)
    ensure_prices_table(con)
    cur = con.cursor()

    total_inserted = 0
    for path in files:
        records = list(parse_pdq_csv(path))
        if not records:
            print(f"  SKIP {path.name}: no spot-month rows parsed")
            continue

        cur.executemany("""
            INSERT OR REPLACE INTO prices (
                observation_date, zone, delivery_month,
                cash_per_bu, cash_change, basis_per_bu, basis_change,
                futures_contract
            ) VALUES (
                :observation_date, :zone, :delivery_month,
                :cash_per_bu, :cash_change, :basis_per_bu, :basis_change,
                :futures_contract
            )
        """, records)
        total_inserted += len(records)
        date_range = f"{records[0]['observation_date']} to {records[-1]['observation_date']}"
        zones = sorted({r['zone'] for r in records})
        print(f"  OK   {path.name:<45} -> {len(records):>5} rows  ({date_range})")
        print(f"       zones: {', '.join(zones)}")

    con.commit()

    # Summary
    print(f"\nDone: {total_inserted} rows inserted/replaced\n")
    print("--- prices summary ---")
    cur.execute("""
        SELECT zone, COUNT(*) AS days,
               MIN(observation_date) AS first, MAX(observation_date) AS last,
               ROUND(AVG(cash_per_bu), 2) AS avg_cash,
               ROUND(AVG(basis_per_bu), 2) AS avg_basis
        FROM prices
        GROUP BY zone ORDER BY zone
    """)
    print(f"{'zone':<15} {'days':>6} {'first':>12} {'last':>12} {'avg_cash':>10} {'avg_basis':>10}")
    for r in cur.fetchall():
        print(f"{r[0]:<15} {r[1]:>6} {r[2]:>12} {r[3]:>12} {r[4]:>10} {r[5]:>10}")

    # Spot-check: any dates where basis is NULL but cash exists?
    cur.execute("""
        SELECT COUNT(*) FROM prices WHERE cash_per_bu IS NOT NULL AND basis_per_bu IS NULL
    """)
    n_no_basis = cur.fetchone()[0]
    if n_no_basis:
        print(f"\nNote: {n_no_basis} rows have cash but no basis (early dates before futures quoting)")

    con.close()


if __name__ == "__main__":
    main()
