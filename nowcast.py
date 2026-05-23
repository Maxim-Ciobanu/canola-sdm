"""
Rebuild the nowcast table using a proper StatsCan-anchored model.

WHAT WAS WRONG WITH THE OLD APPROACH
====================================
The old nowcast computed:
    estimated_total_stocks = total_supplies - CGC_cumulative_deliveries
                                            - pro-rated seed/feed/waste

The problem: CGC "deliveries" (Primary + Process) is NOT the same universe
as StatsCan "deliveries". CGC double-counts grain that moves from primary
elevators to process elevators (counted once as Primary delivery, once as
Process delivery). The result was that CGC totals exceeded StatsCan by
5-10% every year, which caused the nowcast to subtract too much from total
supplies, producing IMPOSSIBLE negative on-farm stocks every July.

WHAT THE NEW APPROACH DOES
==========================
The model uses StatsCan as truth at four anchor points per crop year:

    Anchor 0 (Aug 1):   total_stocks = prior_year_july_ending_stocks
                                     + current_year_production
                       (i.e. "beginning stocks + production" — what
                       StatsCan reports as Total Supplies)

    Anchor 1 (~Dec 31): total_stocks = supplies - SC_deliveries_to_Dec
                                                - seed - feed/waste_to_Dec

    Anchor 2 (~Mar 31): same identity at March

    Anchor 3 (~Jul 31): SC ending_stocks — the truth.

Between anchors, the model interpolates total_stocks along the *shape* of
the CGC weekly Primary deliveries curve. Primary is the cleanest channel
(no double-counting with Process). The interpolation is scaled so the model
lands exactly on each anchor, no matter how big the gap is.

Visible commercial stocks = primary + process + condo elevator stocks from CGC.
Implied on-farm stocks = total_stocks - visible commercial.

Run:
    python3 nowcast.py

Existing nowcast rows are replaced.
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "db" / "canola.sqlite"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def fetch_statscan(con):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("""
        SELECT crop_year, snapshot_month, snapshot_date,
               total_supplies_kt, beginning_stocks_kt, production_kt,
               deliveries_kt, seed_requirements_kt, ending_stocks_kt,
               feed_waste_dockage_kt
        FROM statscan_sd
        WHERE geography='Alberta' AND crop='Canola'
        ORDER BY snapshot_date
    """).fetchall()]


def fetch_cgc(con):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("""
        SELECT week_number, week_ending, crop_year,
               producer_deliveries_weekly_kt, producer_deliveries_cytd_kt,
               primary_elevator_stocks_kt, process_elevator_stocks_kt,
               condo_storage_kt
        FROM cgc_weekly
        WHERE geography='Alberta' AND crop='Canola'
        ORDER BY week_ending
    """).fetchall()]


# ---------------------------------------------------------------------------
# Anchor construction
# ---------------------------------------------------------------------------

def build_anchors(statscan_rows):
    """For each crop year, return a list of (anchor_date, total_stocks_kt) tuples
    in chronological order.

    Anchors come from StatsCan:
      - Aug 1 (start): prior crop year's July ending_stocks PLUS this crop year's
                       production. (i.e. total_supplies on Aug 1 = stocks already
                       on farm + crop just harvested.)
      - Dec/Mar/Jul:   total_supplies - deliveries - seed - feed/waste at each snapshot
    """
    # Index by crop_year and snapshot_month
    by_cy = {}
    for r in statscan_rows:
        by_cy.setdefault(r["crop_year"], {})[r["snapshot_month"]] = r

    anchors_by_cy = {}
    sorted_cys = sorted(by_cy.keys())

    for i, cy in enumerate(sorted_cys):
        snaps = by_cy[cy]
        # Need at least the July snapshot of this year (= ending stocks)
        if "July" not in snaps:
            continue

        july_snap = snaps["July"]
        # total_supplies is fixed across all three snapshots of the same year.
        # If December exists, use it (most recent vintage); otherwise fall back.
        ref_snap = snaps.get("December") or snaps.get("March") or july_snap
        total_supplies = ref_snap["total_supplies_kt"]
        production = ref_snap["production_kt"]
        seed_full_year = july_snap["seed_requirements_kt"] or 0
        feedwaste_full_year = july_snap["feed_waste_dockage_kt"] or 0

        anchors = []

        # Anchor 0: Aug 1 (start of crop year)
        # Total stocks = total supplies (= beginning stocks + production)
        cy_start_year = int(cy.split("/")[0])
        anchor_aug1 = date(cy_start_year, 8, 1)
        anchors.append({
            "date": anchor_aug1,
            "label": "Aug 1 start",
            "total_stocks_kt": total_supplies,
            "cumulative_deliveries_kt": 0.0,
            "cumulative_seed_kt": 0.0,
            "cumulative_feedwaste_kt": 0.0,
        })

        # Anchor 1: December snapshot
        if "December" in snaps:
            s = snaps["December"]
            # By Dec, almost no seed used yet (seed is May-June). Almost no feed/waste
            # cumulative either — most is later in year. Use proportional model:
            # seed is treated as one-time in May, so pro-rate as 0 by Dec.
            # feed/waste is treated as proportional to time elapsed.
            seed_dec = 0.0   # planting hasn't happened yet
            # feed/waste at Dec: pro-rate by share of year elapsed (Aug-Dec = 5/12)
            feedwaste_dec = feedwaste_full_year * (5 / 12)
            anchors.append({
                "date": datetime.fromisoformat(s["snapshot_date"]).date(),
                "label": "December",
                "total_stocks_kt": (total_supplies
                                    - (s["deliveries_kt"] or 0)
                                    - seed_dec
                                    - feedwaste_dec),
                "cumulative_deliveries_kt": s["deliveries_kt"] or 0,
                "cumulative_seed_kt": seed_dec,
                "cumulative_feedwaste_kt": feedwaste_dec,
            })

        # Anchor 2: March snapshot
        if "March" in snaps:
            s = snaps["March"]
            seed_mar = 0.0   # planting is May-June, still hasn't happened
            feedwaste_mar = feedwaste_full_year * (8 / 12)   # Aug-March = 8/12
            anchors.append({
                "date": datetime.fromisoformat(s["snapshot_date"]).date(),
                "label": "March",
                "total_stocks_kt": (total_supplies
                                    - (s["deliveries_kt"] or 0)
                                    - seed_mar
                                    - feedwaste_mar),
                "cumulative_deliveries_kt": s["deliveries_kt"] or 0,
                "cumulative_seed_kt": seed_mar,
                "cumulative_feedwaste_kt": feedwaste_mar,
            })

        # Anchor 3: July snapshot — must equal SC ending stocks exactly
        anchors.append({
            "date": datetime.fromisoformat(july_snap["snapshot_date"]).date(),
            "label": "July (truth)",
            "total_stocks_kt": july_snap["ending_stocks_kt"],
            "cumulative_deliveries_kt": july_snap["deliveries_kt"] or 0,
            "cumulative_seed_kt": seed_full_year,
            "cumulative_feedwaste_kt": feedwaste_full_year,
        })

        anchors_by_cy[cy] = anchors

    return anchors_by_cy


# ---------------------------------------------------------------------------
# CGC-shape-based interpolation
# ---------------------------------------------------------------------------

def cgc_cumulative_at(cgc_rows_for_cy, target_date):
    """Get CGC Primary cumulative deliveries on or just before target_date.

    Returns 0 if target_date is before any CGC week, or the latest CYTD
    value at or before target_date.
    """
    # Filter for valid (non-NULL CYTD) rows and sort
    valid = [(datetime.fromisoformat(r["week_ending"]).date(),
              r["producer_deliveries_cytd_kt"])
             for r in cgc_rows_for_cy
             if r["producer_deliveries_cytd_kt"] is not None]
    if not valid:
        return 0.0
    valid.sort()

    # Aug 1 baseline: zero deliveries
    if target_date < valid[0][0]:
        # Linearly scale up the first week's CYTD from 0 at Aug 1
        cy_start = date(target_date.year if target_date.month >= 8 else target_date.year - 1, 8, 1)
        first_date, first_cytd = valid[0]
        if target_date <= cy_start:
            return 0.0
        total_days = (first_date - cy_start).days
        elapsed = (target_date - cy_start).days
        return first_cytd * (elapsed / total_days) if total_days > 0 else 0.0

    # Find the latest week ending on or before target_date
    latest = valid[0][1]
    for d, v in valid:
        if d <= target_date:
            latest = v
        else:
            break
    return latest


def interpolate_between_anchors(anchor_a, anchor_b, target_date, cgc_rows_for_cy):
    """Estimate total_stocks at target_date, between two anchors A and B.

    Method: use CGC Primary cumulative deliveries as the "shape" indicator.
    Scale that shape so the interpolation exactly matches both anchors.

    cgc_cum(d) = CGC Primary CYTD at date d
    Within [A, B], stocks decrease as deliveries increase. The fraction of
    the way through the [A, B] period in terms of CGC-shape is:
        f = (cgc_cum(target) - cgc_cum(A.date)) / (cgc_cum(B.date) - cgc_cum(A.date))
    
    Then: stocks(target) = A.total_stocks + f * (B.total_stocks - A.total_stocks)

    Falls back to time-linear interpolation if the CGC shape doesn't move
    (e.g. weekly file missing in that span).
    """
    if target_date <= anchor_a["date"]:
        return anchor_a["total_stocks_kt"]
    if target_date >= anchor_b["date"]:
        return anchor_b["total_stocks_kt"]

    cgc_a = cgc_cumulative_at(cgc_rows_for_cy, anchor_a["date"])
    cgc_b = cgc_cumulative_at(cgc_rows_for_cy, anchor_b["date"])
    cgc_t = cgc_cumulative_at(cgc_rows_for_cy, target_date)

    cgc_span = cgc_b - cgc_a
    if cgc_span <= 0:
        # CGC shape isn't usable here — fall back to time-linear
        total_days = (anchor_b["date"] - anchor_a["date"]).days
        elapsed = (target_date - anchor_a["date"]).days
        frac = elapsed / total_days if total_days > 0 else 0.0
    else:
        frac = (cgc_t - cgc_a) / cgc_span
        frac = max(0.0, min(1.0, frac))   # clamp to [0,1]

    return anchor_a["total_stocks_kt"] + frac * (anchor_b["total_stocks_kt"] - anchor_a["total_stocks_kt"])


# ---------------------------------------------------------------------------
# Nowcast computation
# ---------------------------------------------------------------------------

def compute_nowcast(cgc_rows, anchors_by_cy):
    """For each CGC week, produce a nowcast row.

    Returns list of dicts ready for INSERT.
    """
    # Group CGC rows by crop_year so anchor-bracketing only looks within year
    cgc_by_cy = {}
    for r in cgc_rows:
        cgc_by_cy.setdefault(r["crop_year"], []).append(r)

    rows_out = []
    skipped_no_anchors = 0

    for r in cgc_rows:
        cy = r["crop_year"]
        wk_end = datetime.fromisoformat(r["week_ending"]).date()
        anchors = anchors_by_cy.get(cy)
        if not anchors:
            skipped_no_anchors += 1
            continue

        # Find the bracketing anchor pair
        anchor_a = anchors[0]
        anchor_b = anchors[-1]
        for i in range(len(anchors) - 1):
            if anchors[i]["date"] <= wk_end <= anchors[i + 1]["date"]:
                anchor_a = anchors[i]
                anchor_b = anchors[i + 1]
                break
        else:
            # wk_end is before first anchor or after last
            if wk_end < anchors[0]["date"]:
                anchor_b = anchors[0]
                # Synthetic Aug 1 as anchor_a if not already there
                # (in practice anchors[0] IS Aug 1, so this shouldn't trigger)
            elif wk_end > anchors[-1]["date"]:
                anchor_a = anchors[-1]
                anchor_b = anchors[-1]

        total_stocks = interpolate_between_anchors(
            anchor_a, anchor_b, wk_end, cgc_by_cy[cy]
        )

        # Visible commercial stocks: just sum the CGC stock numbers
        prim = r["primary_elevator_stocks_kt"]
        proc = r["process_elevator_stocks_kt"]
        condo = r["condo_storage_kt"]
        if prim is None and proc is None and condo is None:
            visible = None
            implied_on_farm = None
        else:
            visible = (prim or 0) + (proc or 0) + (condo or 0)
            implied_on_farm = total_stocks - visible
            # Floor on-farm at zero (shouldn't happen, but if it does we know
            # the visible-stocks side is over-reporting or the anchor is stale)
            if implied_on_farm < 0:
                implied_on_farm = 0.0

        # For reference, also store the CGC-side CYTD (informational only)
        cgc_cytd = r["producer_deliveries_cytd_kt"]

        # total_supplies for this row: the supply side of the balance sheet
        # (constant across the crop year) — pull it from the start anchor
        total_supplies = anchors[0]["total_stocks_kt"]

        rows_out.append({
            "week_ending":                  r["week_ending"],
            "crop_year":                    cy,
            "geography":                    "Alberta",
            "crop":                         "Canola",
            "total_supplies_kt":            total_supplies,
            "deliveries_cytd_kt":           cgc_cytd,
            "estimated_total_stocks_kt":    total_stocks,
            "visible_commercial_stocks_kt": visible,
            "implied_on_farm_stocks_kt":    implied_on_farm,
        })

    if skipped_no_anchors:
        print(f"  WARN: skipped {skipped_no_anchors} CGC weeks (no StatsCan anchors for that crop year)")
    return rows_out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    print(f"Reading from {DB_PATH}")
    statscan = fetch_statscan(con)
    cgc = fetch_cgc(con)
    print(f"  {len(statscan)} StatsCan rows, {len(cgc)} CGC rows")

    print("\nBuilding anchors per crop year...")
    anchors_by_cy = build_anchors(statscan)
    for cy in sorted(anchors_by_cy.keys()):
        anchors = anchors_by_cy[cy]
        labels = ", ".join(f"{a['label']}={a['total_stocks_kt']:.0f}kt" for a in anchors)
        print(f"  {cy}: {labels}")

    print("\nComputing nowcast for every CGC week...")
    rows = compute_nowcast(cgc, anchors_by_cy)
    print(f"  produced {len(rows)} nowcast rows")

    # Wipe old nowcast and re-insert
    cur.execute("DELETE FROM nowcast")
    cur.executemany("""
        INSERT INTO nowcast (
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
    """, rows)
    con.commit()
    print(f"  wrote {len(rows)} rows to nowcast")

    # Validation: report year-end implied stocks vs StatsCan truth
    print("\n--- Year-end validation: nowcast vs StatsCan July ending stocks ---")
    print(f"{'CY':<8} {'NC est total':>13} {'NC visible':>11} {'NC on-farm':>11} {'SC ending':>10} {'gap':>8}")
    for cy in sorted(anchors_by_cy.keys()):
        nc = cur.execute("""SELECT estimated_total_stocks_kt, visible_commercial_stocks_kt,
                                   implied_on_farm_stocks_kt
                            FROM nowcast WHERE crop_year=?
                            ORDER BY week_ending DESC LIMIT 1""", (cy,)).fetchone()
        sc = next((s for s in statscan if s["crop_year"] == cy and s["snapshot_month"] == "July"), None)
        if nc and sc:
            gap = nc[0] - sc["ending_stocks_kt"]
            print(f"{cy:<8} {nc[0]:>13.1f} {nc[1] or 0:>11.1f} {nc[2] or 0:>11.1f} "
                  f"{sc['ending_stocks_kt']:>10.1f} {gap:>+8.1f}")

    print("\n(Gaps near zero mean the nowcast lands exactly on the StatsCan anchor.)")
    print("(Latest crop year may show a real gap if it hasn't reached its final SC snapshot yet.)")

    con.close()


if __name__ == "__main__":
    main()
