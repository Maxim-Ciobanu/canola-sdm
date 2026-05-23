"""
Alberta Canola Nowcast — weekly implied on-farm stocks.

Applies the balance sheet identity each week:

    Estimated Total Stocks at week N
        = Total Supplies  (fixed at crop-year start from StatsCan)
        - Cumulative deliveries to week N  (CGC weekly)
        - Pro-rated feed / waste / dockage / seed  (StatsCan annual, spread linearly)

    Implied On-Farm Stocks at week N
        = Estimated Total Stocks
        - Visible Commercial Stocks  (primary elevator + process elevator + condo)

For each crop year we need:
    - A StatsCan snapshot that gives total_supplies_kt and the annual feed/waste/seed
    - CGC weekly rows with CYTD deliveries and visible stocks

The "best" StatsCan snapshot for total_supplies is the FIRST one of a crop year
(December), since it establishes beginning stocks + production.  Later snapshots
(March, July) may revise feed/waste/seed estimates — we use the LATEST available
snapshot's feed/waste/seed figures.

Usage:
    python3 nowcast.py          # compute and store in DB
    python3 nowcast.py --dry    # print results without writing

Re-running is safe — rows are upserted by (week_ending, geography, crop).
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "db" / "canola.sqlite"


def get_statscan_by_crop_year(con):
    """Return dict of crop_year -> best snapshot params for the nowcast.

    For total_supplies: use any snapshot (should be consistent within a CY).
    For feed/waste/seed: use the LATEST snapshot (most revised estimate).
    For deliveries: we don't use StatsCan deliveries (CGC weekly replaces them).
    """
    rows = con.execute("""
        SELECT crop_year, snapshot_month, snapshot_date,
               total_supplies_kt, beginning_stocks_kt, production_kt,
               feed_waste_dockage_kt, seed_requirements_kt, deliveries_kt,
               ending_stocks_kt
        FROM statscan_sd
        WHERE geography='Alberta' AND crop='Canola'
        ORDER BY crop_year, snapshot_date
    """).fetchall()

    by_cy = {}
    for r in rows:
        cy = r[0]
        if cy not in by_cy:
            by_cy[cy] = {
                "crop_year": cy,
                "total_supplies_kt": r[3],
                "beginning_stocks_kt": r[4],
                "production_kt": r[5],
                # Will be overwritten by later snapshots (latest wins)
                "feed_waste_dockage_kt": r[6],
                "seed_requirements_kt": r[7],
                # Track the July (full-year) snapshot for calibration
                "jul_deliveries_kt": None,
                "jul_ending_stocks_kt": None,
            }
        # Always update feed/waste/seed to latest snapshot
        if r[6] is not None:
            by_cy[cy]["feed_waste_dockage_kt"] = r[6]
        if r[7] is not None:
            by_cy[cy]["seed_requirements_kt"] = r[7]
        # If this is a July snapshot, capture the full-year actuals
        if r[1] == "July":
            by_cy[cy]["jul_deliveries_kt"] = r[8]
            by_cy[cy]["jul_ending_stocks_kt"] = r[9]

    return by_cy


def get_cgc_weeks(con):
    """Return all CGC weekly rows grouped by crop year."""
    rows = con.execute("""
        SELECT week_number, week_ending, crop_year,
               total_deliveries_cytd_kt,
               primary_elevator_stocks_kt, process_elevator_stocks_kt,
               condo_storage_kt, total_visible_stocks_kt
        FROM cgc_weekly
        WHERE geography='Alberta' AND crop='Canola'
        ORDER BY crop_year, week_ending
    """).fetchall()

    by_cy = {}
    for r in rows:
        cy = r[2]
        by_cy.setdefault(cy, []).append({
            "week_number": r[0],
            "week_ending": r[1],
            "crop_year": cy,
            "total_deliveries_cytd_kt": r[3],
            "primary_elevator_stocks_kt": r[4],
            "process_elevator_stocks_kt": r[5],
            "condo_storage_kt": r[6],
            "total_visible_stocks_kt": r[7],
        })
    return by_cy


def compute_nowcast(statscan_params, cgc_weeks):
    """Compute nowcast for one crop year.

    Feed/waste/seed are pro-rated linearly across 52 weeks.
    This is a simplification — in reality feed/waste is somewhat seasonal —
    but it's the right starting point for Phase 1.

    Returns list of nowcast row dicts.
    """
    total_supplies = statscan_params.get("total_supplies_kt")
    feed_waste = statscan_params.get("feed_waste_dockage_kt") or 0
    seed = statscan_params.get("seed_requirements_kt") or 0

    if total_supplies is None:
        return []

    # Total non-delivery disposition spread over 52 weeks
    annual_other = feed_waste + seed
    weeks_in_year = 52

    results = []
    for wk in cgc_weeks:
        wn = wk["week_number"]
        deliveries_cytd = wk["total_deliveries_cytd_kt"]
        visible = wk["total_visible_stocks_kt"]

        if deliveries_cytd is None:
            continue

        # Pro-rate feed/waste/seed to this point in the crop year
        prorated_other = annual_other * (wn / weeks_in_year)

        # Estimated total stocks remaining
        estimated_total = total_supplies - deliveries_cytd - prorated_other

        # Implied on-farm = total remaining minus what's visibly in the system
        if visible is not None:
            implied_on_farm = estimated_total - visible
        else:
            implied_on_farm = None

        results.append({
            "week_ending": wk["week_ending"],
            "crop_year": wk["crop_year"],
            "geography": "Alberta",
            "crop": "Canola",
            "total_supplies_kt": total_supplies,
            "deliveries_cytd_kt": deliveries_cytd,
            "prorated_other_kt": round(prorated_other, 2),
            "estimated_total_stocks_kt": round(estimated_total, 2),
            "visible_commercial_stocks_kt": visible,
            "implied_on_farm_stocks_kt": round(implied_on_farm, 2) if implied_on_farm is not None else None,
        })

    return results


def main():
    dry_run = "--dry" in sys.argv

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        print("Run init_db.py and then the loaders first.")
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    statscan_by_cy = get_statscan_by_crop_year(con)
    cgc_by_cy = get_cgc_weeks(con)

    if not statscan_by_cy:
        print("No StatsCan data loaded. Run ingest/load_statscan.py first.")
        sys.exit(1)
    if not cgc_by_cy:
        print("No CGC weekly data loaded. Run ingest/load_cgc.py first.")
        sys.exit(1)

    all_results = []
    for cy in sorted(set(statscan_by_cy.keys()) & set(cgc_by_cy.keys())):
        params = statscan_by_cy[cy]
        weeks = cgc_by_cy[cy]
        results = compute_nowcast(params, weeks)
        all_results.extend(results)

        if results:
            first = results[0]
            last = results[-1]
            print(f"  {cy}: {len(results)} weeks  "
                  f"supply={params['total_supplies_kt']:.0f} kt  "
                  f"fw+seed={params['feed_waste_dockage_kt'] or 0:.0f}+{params['seed_requirements_kt'] or 0:.0f} kt  "
                  f"last on-farm={last['implied_on_farm_stocks_kt'] or '?'} kt")

    # Crop years with CGC data but no StatsCan
    missing = set(cgc_by_cy.keys()) - set(statscan_by_cy.keys())
    if missing:
        print(f"\n  SKIP (no StatsCan data): {', '.join(sorted(missing))}")

    if not all_results:
        print("\nNo overlapping crop years between StatsCan and CGC data.")
        sys.exit(0)

    if dry_run:
        print(f"\n--- DRY RUN: {len(all_results)} rows computed, not written ---")
        print(f"\n{'week_ending':<12} {'cy':<8} {'supply':>8} {'del_cytd':>9} {'pro_other':>10} "
              f"{'est_total':>10} {'visible':>9} {'on_farm':>9}")
        for r in all_results:
            print(f"{r['week_ending']:<12} {r['crop_year']:<8} "
                  f"{r['total_supplies_kt']:>8.0f} "
                  f"{r['deliveries_cytd_kt']:>9.1f} "
                  f"{r['prorated_other_kt']:>10.1f} "
                  f"{r['estimated_total_stocks_kt']:>10.1f} "
                  f"{(r['visible_commercial_stocks_kt'] or 0):>9.1f} "
                  f"{(r['implied_on_farm_stocks_kt'] or 0):>9.1f}")
        return

    # Write to DB
    cur = con.cursor()
    cur.executemany("""
        INSERT OR REPLACE INTO nowcast (
            week_ending, crop_year, geography, crop,
            total_supplies_kt, deliveries_cytd_kt,
            estimated_total_stocks_kt, visible_commercial_stocks_kt,
            implied_on_farm_stocks_kt
        ) VALUES (
            :week_ending, :crop_year, :geography, :crop,
            :total_supplies_kt, :deliveries_cytd_kt,
            :estimated_total_stocks_kt, :visible_commercial_stocks_kt,
            :implied_on_farm_stocks_kt
        )
    """, all_results)
    con.commit()
    print(f"\nDone: {len(all_results)} rows written to nowcast table.")

    # Summary
    print("\n--- nowcast summary ---")
    cur.execute("""
        SELECT crop_year, COUNT(*) AS weeks,
               MIN(week_ending), MAX(week_ending),
               ROUND(MAX(total_supplies_kt),0) AS supply,
               ROUND(MAX(deliveries_cytd_kt),0) AS max_del,
               ROUND(MIN(implied_on_farm_stocks_kt),0) AS min_farm,
               ROUND(MAX(implied_on_farm_stocks_kt),0) AS max_farm
        FROM nowcast WHERE geography='Alberta' AND crop='Canola'
        GROUP BY crop_year ORDER BY crop_year
    """)
    print(f"{'crop_year':<10} {'weeks':>6} {'date_range':>25} {'supply':>8} "
          f"{'max_del':>8} {'on_farm_range':>15}")
    for r in cur.fetchall():
        print(f"{r[0]:<10} {r[1]:>6} {r[2]} to {r[3]} {r[4]:>8.0f} "
              f"{r[5]:>8.0f} {r[6]:>7.0f}-{r[7]:.0f}")

    con.close()


if __name__ == "__main__":
    main()
