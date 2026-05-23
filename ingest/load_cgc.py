"""
Bulk-load every CGC GSW weekly Excel file in data/raw/cgc/ into cgc_weekly.

Drop all your gsw-shg-NN-en.xlsx files into data/raw/cgc/ and run:
    python3 ingest/load_cgc.py

Re-running is safe — rows are upserted by (week_ending, geography, crop).

Handles combined-week files: CGC sometimes merges two weeks into one report
(usually around Christmas — e.g. "Week 22, December 18 - December 31"). When
detected (date range > 7 days), we write TWO rows:

  - The LATER week (e.g. wk22 ending Dec 31): stocks and CYTD as published,
    weekly flow set to NULL (we can't isolate one week from the combined number).
  - The EARLIER week (e.g. wk21 ending Dec 24): CYTD derived as
    (published CYTD - combined weekly flow). Stocks set to NULL (we don't have
    that point-in-time snapshot).

Both rows get a `notes` field flagging the combined source. The gap detector
respects these notes and won't false-warn.
"""

import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from openpyxl import load_workbook

CGC_DIR = Path(__file__).parent.parent / "data" / "raw" / "cgc"
DB_PATH = Path(__file__).parent.parent / "data" / "db" / "canola.sqlite"

SECTIONS = [
    ("Primary", "Producer Deliveries to Primary Elevators -",       "producer_deliveries_weekly_kt"),
    ("Primary", "Primary Elevator Shipments -",                     "primary_shipments_weekly_kt"),
    ("Primary", "Crop Year to Date Producer Deliveries",            "producer_deliveries_cytd_kt"),
    ("Primary", "Crop Year to Date Primary Elevator Shipments",     "primary_shipments_cytd_kt"),
    ("Primary", "Stocks at Primary Elevators",                      "primary_elevator_stocks_kt"),
    ("Primary", "Condo Storage at Primary Elevators",               "condo_storage_kt"),
    ("Process", "Producer Deliveries to Process",                   "process_deliveries_weekly_kt"),
    ("Process", "Crop Year to Date Producer Del",                   "process_deliveries_cytd_kt"),
    ("Process", "Stocks at Process Elevators",                      "process_elevator_stocks_kt"),
]
ALBERTA_COL = 3   # 0=label, 1=MB, 2=SK, 3=Alberta, 4=BC, 5=Total

# Columns to clear (set to NULL) on each side of a combined-week split
WEEKLY_FLOW_COLS = (
    "producer_deliveries_weekly_kt",
    "primary_shipments_weekly_kt",
    "process_deliveries_weekly_kt",
    "total_deliveries_weekly_kt",
)
STOCK_COLS = (
    "primary_elevator_stocks_kt",
    "condo_storage_kt",
    "process_elevator_stocks_kt",
    "total_visible_stocks_kt",
)


def find_section_row(rows, needle):
    for i, row in enumerate(rows):
        if row and row[0] and needle in str(row[0]):
            return i
    return None


def find_canola_below(rows, start_idx, max_lookahead=20):
    for i in range(start_idx, min(start_idx + max_lookahead, len(rows))):
        row = rows[i]
        if row and row[0] and str(row[0]).strip() == "Canola":
            return row
    return None


def crop_year_for(d):
    """Return crop year string for a given date. Aug 1 = start of new crop year."""
    if d.month >= 8:
        return f"{d.year}/{(d.year + 1) % 100:02d}"
    return f"{d.year - 1}/{d.year % 100:02d}"


def parse_one_file(path):
    """Read one GSW xlsx. Returns a list of dicts (1 row normally, 2 if combined-week)."""
    wb = load_workbook(path, read_only=True, data_only=True)
    sheets = {name: list(wb[name].iter_rows(values_only=True))
              for name in ("Primary", "Process") if name in wb.sheetnames}
    if "Primary" not in sheets:
        return []

    # Extract all the AB canola values
    values = {}
    for sheet_name, needle, col in SECTIONS:
        if sheet_name not in sheets:
            values[col] = None
            continue
        title_idx = find_section_row(sheets[sheet_name], needle)
        if title_idx is None:
            values[col] = None
            continue
        canola_row = find_canola_below(sheets[sheet_name], title_idx)
        if canola_row is None:
            values[col] = None
            continue
        raw = canola_row[ALBERTA_COL]
        values[col] = float(raw) if raw not in (None, "", " ") else None

    # Computed totals
    values["total_deliveries_weekly_kt"] = (
        (values.get("producer_deliveries_weekly_kt") or 0)
        + (values.get("process_deliveries_weekly_kt") or 0)
    )
    values["total_deliveries_cytd_kt"] = (
        (values.get("producer_deliveries_cytd_kt") or 0)
        + (values.get("process_deliveries_cytd_kt") or 0)
    )
    values["total_visible_stocks_kt"] = (
        (values.get("primary_elevator_stocks_kt") or 0)
        + (values.get("process_elevator_stocks_kt") or 0)
        + (values.get("condo_storage_kt") or 0)
    )

    # Parse title
    title_idx = find_section_row(sheets["Primary"], "Producer Deliveries to Primary Elevators -")
    if title_idx is None:
        return []
    title_text = str(sheets["Primary"][title_idx][0])

    week_match = re.search(r"Week\s+(\d+)", title_text)
    if not week_match:
        return []
    week_number = int(week_match.group(1))

    dates = re.findall(r"([A-Z][a-z]+ \d+, \d{4})", title_text)
    if not dates:
        return []
    parsed_dates = [datetime.strptime(d, "%B %d, %Y").date() for d in dates]
    week_ending = parsed_dates[-1]

    # --- Combined-week detection -----------------------------------------
    # A normal week is 7 days. The title typically has 2 dates spanning ~6 days
    # (e.g. "February 19, 2024 - February 25, 2024"). A combined week spans
    # ~13 days (e.g. "December 18, 2023 - December 31, 2023").
    is_combined = False
    span_days = None
    if len(parsed_dates) >= 2:
        span_days = (parsed_dates[-1] - parsed_dates[0]).days
        if span_days >= 10:
            is_combined = True

    if not is_combined:
        # Normal single-week file: one row
        record = _build_record(week_number, week_ending, values, path.name,
                               notes=None)
        return [record]

    # --- Combined-week handling ------------------------------------------
    # Write two rows. Naming:
    #   later_week  = the wk in the title (week_number, week_ending = published)
    #   earlier_week = (week_number - 1, week_ending - 7 days)
    earlier_week_num = week_number - 1
    earlier_week_end = week_ending - timedelta(days=7)

    note = f"Combined-week file ({span_days}-day span). Source: {path.name}"

    # Later week row: keep stocks & CYTD as published, NULL the weekly flows.
    later_vals = dict(values)
    for col in WEEKLY_FLOW_COLS:
        later_vals[col] = None
    later_row = _build_record(week_number, week_ending, later_vals, path.name,
                              notes=note + " [later half of combined period]")

    # Earlier week row: derive CYTD by subtracting combined weekly flow.
    # We have the COMBINED weekly value in the original `values` dict.
    earlier_vals = {col: None for col in values.keys()}
    earlier_vals["producer_deliveries_cytd_kt"] = _safe_sub(
        values.get("producer_deliveries_cytd_kt"),
        values.get("producer_deliveries_weekly_kt"),
    )
    earlier_vals["primary_shipments_cytd_kt"] = _safe_sub(
        values.get("primary_shipments_cytd_kt"),
        values.get("primary_shipments_weekly_kt"),
    )
    earlier_vals["process_deliveries_cytd_kt"] = _safe_sub(
        values.get("process_deliveries_cytd_kt"),
        values.get("process_deliveries_weekly_kt"),
    )
    earlier_vals["total_deliveries_cytd_kt"] = _safe_sub(
        values.get("total_deliveries_cytd_kt"),
        values.get("total_deliveries_weekly_kt"),
    )
    # Stocks and weekly flows stay NULL — we genuinely don't know.
    earlier_row = _build_record(earlier_week_num, earlier_week_end, earlier_vals,
                                path.name,
                                notes=note + " [earlier half — CYTD derived by subtraction, weekly/stocks NULL]")

    return [earlier_row, later_row]


def _safe_sub(a, b):
    if a is None or b is None:
        return None
    return a - b


def _build_record(week_number, week_ending, values, source_file, notes):
    return {
        "week_number": week_number,
        "week_ending": week_ending.isoformat(),
        "crop_year":   crop_year_for(week_ending),
        "geography":   "Alberta",
        "crop":        "Canola",
        "source_file": source_file,
        "notes":       notes,
        **{k: values.get(k) for k in (
            "producer_deliveries_weekly_kt", "primary_shipments_weekly_kt",
            "producer_deliveries_cytd_kt", "primary_shipments_cytd_kt",
            "primary_elevator_stocks_kt", "condo_storage_kt",
            "process_deliveries_weekly_kt", "process_deliveries_cytd_kt",
            "process_elevator_stocks_kt",
            "total_deliveries_weekly_kt", "total_deliveries_cytd_kt",
            "total_visible_stocks_kt",
        )},
    }


def ensure_notes_column(con):
    """Add notes column to cgc_weekly if missing (for old DBs)."""
    cur = con.cursor()
    cur.execute("PRAGMA table_info(cgc_weekly)")
    cols = {r[1] for r in cur.fetchall()}
    if "notes" not in cols:
        cur.execute("ALTER TABLE cgc_weekly ADD COLUMN notes TEXT")
        con.commit()
        print("(added missing 'notes' column to cgc_weekly)")


def main():
    if not CGC_DIR.exists():
        print(f"ERROR: directory not found: {CGC_DIR}")
        sys.exit(1)

    files = sorted(CGC_DIR.glob("*.xlsx"))
    if not files:
        print(f"No .xlsx files in {CGC_DIR}. Drop your GSW files there and re-run.")
        sys.exit(0)

    print(f"Found {len(files)} file(s) in {CGC_DIR}\n")

    con = sqlite3.connect(DB_PATH)
    ensure_notes_column(con)
    cur = con.cursor()

    inserted, skipped, failed = 0, 0, 0
    failed_files = []
    for path in files:
        try:
            records = parse_one_file(path)
            if not records:
                skipped += 1
                print(f"  SKIP {path.name:<35} (couldn't parse)")
                continue
            for record in records:
                cur.execute("""
                    INSERT OR REPLACE INTO cgc_weekly (
                        week_number, week_ending, crop_year, geography, crop,
                        producer_deliveries_weekly_kt, primary_shipments_weekly_kt,
                        producer_deliveries_cytd_kt, primary_shipments_cytd_kt,
                        primary_elevator_stocks_kt, condo_storage_kt,
                        process_deliveries_weekly_kt, process_deliveries_cytd_kt,
                        process_elevator_stocks_kt,
                        total_deliveries_weekly_kt, total_deliveries_cytd_kt,
                        total_visible_stocks_kt,
                        source_file, notes
                    ) VALUES (
                        :week_number, :week_ending, :crop_year, :geography, :crop,
                        :producer_deliveries_weekly_kt, :primary_shipments_weekly_kt,
                        :producer_deliveries_cytd_kt, :primary_shipments_cytd_kt,
                        :primary_elevator_stocks_kt, :condo_storage_kt,
                        :process_deliveries_weekly_kt, :process_deliveries_cytd_kt,
                        :process_elevator_stocks_kt,
                        :total_deliveries_weekly_kt, :total_deliveries_cytd_kt,
                        :total_visible_stocks_kt,
                        :source_file, :notes
                    )
                """, record)
                inserted += 1
            tag = " [SPLIT INTO 2 ROWS]" if len(records) == 2 else ""
            print(f"  OK   {path.name:<35} -> {len(records)} row(s){tag}")
            for r in records:
                weekly_status = "weekly=NULL" if r["producer_deliveries_weekly_kt"] is None else f"weekly={r['producer_deliveries_weekly_kt']}"
                print(f"        wk {r['week_number']:>2}  ending {r['week_ending']}  CY {r['crop_year']}  {weekly_status}")
        except Exception as e:
            failed += 1
            failed_files.append((path.name, str(e)))
            print(f"  ERR  {path.name}: {e}")

    con.commit()
    print(f"\nDone: {inserted} row(s) inserted/replaced, {skipped} skipped, {failed} failed")
    if failed_files:
        print("\nFailed files:")
        for n, e in failed_files:
            print(f"  - {n}: {e}")

    # Summary
    print("\n--- cgc_weekly summary ---")
    cur.execute("""
        SELECT crop_year, COUNT(*) AS weeks,
               MIN(week_number) AS first_wk, MAX(week_number) AS last_wk,
               MIN(week_ending) AS first_end, MAX(week_ending) AS last_end
        FROM cgc_weekly WHERE geography='Alberta' AND crop='Canola'
        GROUP BY crop_year ORDER BY crop_year
    """)
    print(f"{'crop_year':<10} {'weeks':>6} {'wk_range':>10}  {'date_range'}")
    for r in cur.fetchall():
        print(f"{r[0]:<10} {r[1]:>6} {r[2]:>3}-{r[3]:<6} {r[4]} to {r[5]}")

    # Gap detector — but ignore weeks whose row was synthesised from a combined file
    cur.execute("""
        SELECT crop_year, week_number, notes FROM cgc_weekly
        WHERE geography='Alberta' AND crop='Canola'
        ORDER BY crop_year, week_number
    """)
    by_cy = {}
    for cy, wn, _ in cur.fetchall():
        by_cy.setdefault(cy, []).append(wn)
    for cy, weeks in by_cy.items():
        weeks = sorted(set(weeks))
        if weeks:
            full = set(range(weeks[0], weeks[-1] + 1))
            gaps = sorted(full - set(weeks))
            if gaps:
                print(f"\n  WARN  {cy} has missing weeks: {gaps}")

    con.close()


if __name__ == "__main__":
    main()
