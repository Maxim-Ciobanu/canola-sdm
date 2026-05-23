"""
Bulk-load every StatsCan 32-10-0015-01 CSV in data/raw/statscan/ into
the statscan_sd table.

Drop your CSV files into data/raw/statscan/ and run:
    python3 ingest/load_statscan.py

Each CSV has 6 snapshot columns (e.g. Mar-23, Jul-23, Dec-23, Mar-24,
Jul-24, Dec-24) and we parse one row per snapshot.

Re-running is safe — rows are upserted by (crop_year, snapshot_month,
geography, crop, report_date).
"""

import csv
import sqlite3
import sys
from datetime import date
from pathlib import Path

STATSCAN_DIR = Path(__file__).parent.parent / "data" / "raw" / "statscan"
DB_PATH = Path(__file__).parent.parent / "data" / "db" / "canola.sqlite"

FIELD_MAP = {
    "Total supplies":                "total_supplies_kt",
    "Beginning stocks":              "beginning_stocks_kt",
    "Production":                    "production_kt",
    "Total disposition":             "total_disposition_kt",
    "Deliveries":                    "deliveries_kt",
    "Seed requirements":             "seed_requirements_kt",
    "Ending stocks":                 "ending_stocks_kt",
    "Animal feed, waste and dockage": "feed_waste_dockage_kt",
}

MONTH_MAP = {"Mar": ("March", 3, 31), "Jul": ("July", 7, 31), "Dec": ("December", 12, 31)}


def clean_label(s):
    """StatsCan adds footnote numbers e.g. 'Deliveries 3 6 7'. Strip them."""
    return s.strip().rstrip("0123456789 ").strip()


def label_to_meta(label):
    """e.g. 'Mar-23' -> ('2022/23', 'March', date(2023, 3, 31))

    Per the StatsCan footnote:
      March 20XX = cumulative Aug(X-1) to Mar X  -> crop year (X-1)/X
      July 20XX  = cumulative Aug(X-1) to Jul X  -> crop year (X-1)/X (full year)
      December 20XX = cumulative Aug X to Dec X  -> crop year X/(X+1)
    """
    mon_abbr, yr2 = label.split("-")
    yr = 2000 + int(yr2)
    snap_month, mm, dd = MONTH_MAP[mon_abbr]
    snap_date = date(yr, mm, dd)
    if mon_abbr == "Dec":
        crop_year = f"{yr}/{(yr + 1) % 100:02d}"
    else:
        crop_year = f"{yr - 1}/{yr % 100:02d}"
    return crop_year, snap_month, snap_date


def parse_one_csv(path):
    """Read one StatsCan CSV, return (list of insert dicts, report_date)."""
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            rows.append(row)

    # Release date
    report_date = None
    for row in rows:
        if row and row[0].startswith("Release date:"):
            report_date = row[0].replace("Release date:", "").strip()
            break
    if not report_date:
        return [], None

    # Geography label (should be Alberta — we assume so but check)
    geography = "Alberta"
    for row in rows:
        if len(row) >= 3 and row[1] == "Geography":
            geography = (row[2] or "Alberta").strip()
            break

    # Column header row (snapshot labels)
    header_row = None
    for row in rows:
        if row and row[0] == "Type of crop":
            header_row = row
            break
    if not header_row:
        return [], report_date
    snapshot_labels = [s for s in header_row[2:] if s]

    # Pull the canola balance sheet rows
    data_rows = {}
    collecting = False
    for row in rows:
        if not row or len(row) < 3:
            continue
        if row[0].startswith("Canola"):
            collecting = True
            continue
        if collecting:
            label = clean_label(row[1] or "")
            if label in FIELD_MAP:
                col = FIELD_MAP[label]
                vals = []
                for v in row[2:2 + len(snapshot_labels)]:
                    if v in (None, "", " "):
                        vals.append(None)
                    else:
                        vals.append(float(str(v).replace(",", "")))
                data_rows[col] = vals
            if all(c in (None, "", " ") for c in row):
                break

    inserts = []
    for i, label in enumerate(snapshot_labels):
        try:
            crop_year, snap_month, snap_date = label_to_meta(label)
        except (ValueError, KeyError):
            continue
        record = {
            "crop_year":      crop_year,
            "snapshot_month": snap_month,
            "snapshot_date":  snap_date.isoformat(),
            "geography":      geography,
            "crop":           "Canola",
            "source_table":   "32-10-0015-01",
            "report_date":    report_date,
        }
        for col in FIELD_MAP.values():
            record[col] = data_rows.get(col, [None] * len(snapshot_labels))[i]
        inserts.append(record)

    return inserts, report_date


def main():
    if not STATSCAN_DIR.exists():
        print(f"ERROR: directory not found: {STATSCAN_DIR}")
        sys.exit(1)

    files = sorted(STATSCAN_DIR.glob("*.csv"))
    if not files:
        print(f"No .csv files in {STATSCAN_DIR}. Drop your StatsCan CSVs there.")
        sys.exit(0)

    print(f"Found {len(files)} file(s) in {STATSCAN_DIR}\n")

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    total_inserts, failed = 0, 0
    for path in files:
        try:
            inserts, report_date = parse_one_csv(path)
            if not inserts:
                print(f"  SKIP {path.name}: couldn't parse any rows")
                continue
            cur.executemany("""
                INSERT OR REPLACE INTO statscan_sd (
                    crop_year, snapshot_month, snapshot_date, geography, crop,
                    total_supplies_kt, beginning_stocks_kt, production_kt,
                    total_disposition_kt, deliveries_kt, seed_requirements_kt,
                    ending_stocks_kt, feed_waste_dockage_kt,
                    source_table, report_date
                ) VALUES (
                    :crop_year, :snapshot_month, :snapshot_date, :geography, :crop,
                    :total_supplies_kt, :beginning_stocks_kt, :production_kt,
                    :total_disposition_kt, :deliveries_kt, :seed_requirements_kt,
                    :ending_stocks_kt, :feed_waste_dockage_kt,
                    :source_table, :report_date
                )
            """, inserts)
            total_inserts += len(inserts)
            print(f"  OK   {path.name:<35} -> {len(inserts):>2} rows  (vintage {report_date})")
        except Exception as e:
            failed += 1
            print(f"  ERR  {path.name}: {e}")

    con.commit()
    print(f"\nDone: {total_inserts} rows inserted/replaced from {len(files)} file(s), {failed} failed")

    # Summary
    print("\n--- statscan_sd summary ---")
    cur.execute("""
        SELECT crop_year, COUNT(*) AS snapshots,
               GROUP_CONCAT(DISTINCT snapshot_month) AS months
        FROM statscan_sd WHERE geography='Alberta' AND crop='Canola'
        GROUP BY crop_year ORDER BY crop_year
    """)
    print(f"{'crop_year':<10} {'snaps':>6}  {'months'}")
    for r in cur.fetchall():
        print(f"{r[0]:<10} {r[1]:>6}  {r[2]}")

    con.close()


if __name__ == "__main__":
    main()
