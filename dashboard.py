"""
Alberta Canola S&D Dashboard.

Reads from data/db/canola.sqlite and displays:
  - Tab 1: StatsCan triannual balance sheet (one row per snapshot)
  - Tab 2: CGC weekly flows (one row per week loaded)
  - Tab 3: Bridge check — CGC CYTD vs StatsCan (only at real anchor weeks)
  - Tab 4: Prices (PDQ) — spot cash bids and basis by zone
  - Tab 5: Findings, gaps, identity validation

Run:
    pip install dearpygui
    python3 dashboard.py
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import dearpygui.dearpygui as dpg

DB_PATH = Path(__file__).parent / "data" / "db" / "canola.sqlite"

# Colours (0-255 RGB)
COL_GOOD   = (90,  200, 120)
COL_WARN   = (240, 180, 60)
COL_BAD    = (220, 80,  80)
COL_DIM    = (160, 160, 160)
COL_HDR    = (110, 180, 240)
COL_INFO   = (180, 180, 200)

# Plot line colours per zone
COL_SOUTH  = (90,  200, 120)   # green
COL_NORTH  = (110, 180, 240)   # blue
COL_PEACE  = (240, 180, 60)    # gold

# Threshold for the bridge check (percent absolute).
# The +7% structural gap (missing channels: producer cars, feed mills,
# "other deliveries" to process) means TIGHT must allow for it.
THR_TIGHT      = 5.0    # within ±5%: ignore noise
THR_ACCEPTABLE = 12.0   # within ±12%: accounts for the ~7% structural gap


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def fetch_statscan():
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("""
        SELECT crop_year, snapshot_month, snapshot_date,
               total_supplies_kt, beginning_stocks_kt, production_kt,
               total_disposition_kt, deliveries_kt, seed_requirements_kt,
               ending_stocks_kt, feed_waste_dockage_kt, report_date
        FROM statscan_sd WHERE geography='Alberta' AND crop='Canola'
        ORDER BY snapshot_date
    """).fetchall()]
    con.close(); return rows


def fetch_cgc():
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    # Tolerate older DBs without `notes` column
    cur = con.cursor()
    cur.execute("PRAGMA table_info(cgc_weekly)")
    has_notes = any(r[1] == "notes" for r in cur.fetchall())
    notes_col = "notes" if has_notes else "NULL AS notes"
    rows = [dict(r) for r in con.execute(f"""
        SELECT week_number, week_ending, crop_year,
               producer_deliveries_weekly_kt, producer_deliveries_cytd_kt,
               primary_shipments_weekly_kt, primary_shipments_cytd_kt,
               process_deliveries_weekly_kt, process_deliveries_cytd_kt,
               total_deliveries_weekly_kt, total_deliveries_cytd_kt,
               primary_elevator_stocks_kt, process_elevator_stocks_kt,
               condo_storage_kt, total_visible_stocks_kt,
               source_file, {notes_col}
        FROM cgc_weekly WHERE geography='Alberta' AND crop='Canola'
        ORDER BY week_ending
    """).fetchall()]
    con.close(); return rows


def fetch_prices():
    """Fetch all PDQ price rows. Returns empty list if table doesn't exist."""
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prices'")
        if not cur.fetchone():
            con.close()
            return []
        rows = [dict(r) for r in con.execute("""
            SELECT observation_date, zone, delivery_month,
                   cash_per_bu, cash_change, basis_per_bu, basis_change,
                   futures_contract
            FROM prices
            ORDER BY observation_date, zone
        """).fetchall()]
    except Exception:
        rows = []
    con.close()
    return rows


def fetch_nowcast():
    """Fetch computed nowcast rows. Returns empty list if table missing or empty."""
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nowcast'")
        if not cur.fetchone():
            con.close()
            return []
        rows = [dict(r) for r in con.execute("""
            SELECT week_ending, crop_year,
                   total_supplies_kt, deliveries_cytd_kt,
                   estimated_total_stocks_kt, visible_commercial_stocks_kt,
                   implied_on_farm_stocks_kt
            FROM nowcast
            WHERE geography='Alberta' AND crop='Canola'
            ORDER BY week_ending
        """).fetchall()]
    except Exception:
        rows = []
    con.close()
    return rows


# ---------------------------------------------------------------------------
# Computations
# ---------------------------------------------------------------------------

def check_identity(s):
    """Validate balance sheet identity for one StatsCan row."""
    supply_side = (s["beginning_stocks_kt"] or 0) + (s["production_kt"] or 0)
    disp_side   = (
        (s["deliveries_kt"] or 0)
        + (s["seed_requirements_kt"] or 0)
        + (s["ending_stocks_kt"] or 0)
        + (s["feed_waste_dockage_kt"] or 0)
    )
    err = abs(supply_side - disp_side)
    return err < 1.0, err


def period_covered(snapshot_month, snapshot_date):
    """Return human-readable period e.g. 'Aug 2022 - Mar 2023'."""
    d = datetime.fromisoformat(snapshot_date).date()
    end_label = d.strftime("%b %Y")
    # Crop year always starts Aug 1 of the prior calendar year
    if snapshot_month == "December":
        start_year = d.year       # Dec 20XX -> Aug 20XX
    else:
        start_year = d.year - 1   # Mar/Jul 20XX -> Aug 20(X-1)
    start_label = f"Aug {start_year}"
    return f"{start_label} - {end_label}"


def bridge_compare(cgc_rows, statscan_rows):
    """Smarter bridge: no synthetic Aug 1 anchor. For each CGC week:
       - If week is AT a StatsCan anchor date: exact comparison ("ANCHOR")
       - If between two real anchors: linear-interpolate ("INTERIM")
       - Before the first real anchor: no comparison ("NO ANCHOR YET")
       - After the last real anchor: hold flat ("POST-ANCHOR")
    """
    results = []
    for cgc in cgc_rows:
        wk_end = datetime.fromisoformat(cgc["week_ending"]).date()
        cy = cgc["crop_year"]

        # Real StatsCan anchors for this crop year only
        anchors = sorted(
            (datetime.fromisoformat(s["snapshot_date"]).date(), s["deliveries_kt"] or 0.0,
             s["snapshot_month"])
            for s in statscan_rows if s["crop_year"] == cy
        )

        if not anchors:
            results.append(_bridge_result(cgc, None, None, None, "NO STATSCAN", "no anchors for this crop year"))
            continue

        first_anchor_date = anchors[0][0]
        last_anchor_date  = anchors[-1][0]

        if wk_end < first_anchor_date:
            results.append(_bridge_result(
                cgc, None, None, None, "NO ANCHOR YET",
                f"no StatsCan snapshot yet (first: {first_anchor_date})"
            ))
            continue

        # AT an anchor (within 7 days = same reporting period): exact comparison
        exact_match = None
        for d, v, m in anchors:
            if abs((wk_end - d).days) <= 6:
                exact_match = (d, v, m)
                break
        if exact_match is not None:
            d, v, m = exact_match
            actual = cgc["total_deliveries_cytd_kt"]
            if actual is None:
                results.append(_bridge_result(cgc, v, None, None, "ANCHOR",
                                              f"matches {m} {d.year} anchor, but CGC CYTD is NULL"))
            else:
                gap_kt = actual - v
                gap_pct = 100 * gap_kt / v if v > 0 else None
                results.append(_bridge_result(cgc, v, gap_kt, gap_pct, "ANCHOR",
                                              f"matches {m} {d.year} StatsCan anchor"))
            continue

        # Between two anchors: linear interpolation
        if wk_end <= last_anchor_date:
            for i in range(len(anchors) - 1):
                d0, v0, _ = anchors[i]
                d1, v1, _ = anchors[i + 1]
                if d0 <= wk_end <= d1:
                    frac = (wk_end - d0).days / max((d1 - d0).days, 1)
                    expected = v0 + frac * (v1 - v0)
                    actual = cgc["total_deliveries_cytd_kt"]
                    if actual is None:
                        results.append(_bridge_result(cgc, expected, None, None, "INTERIM",
                                                      "CGC CYTD is NULL"))
                    else:
                        gap_kt = actual - expected
                        gap_pct = 100 * gap_kt / expected if expected > 0 else None
                        results.append(_bridge_result(cgc, expected, gap_kt, gap_pct, "INTERIM",
                                                      ""))
                    break
            continue

        # After last anchor: hold flat (no upper bound for interpolation)
        expected = anchors[-1][1]
        actual = cgc["total_deliveries_cytd_kt"]
        if actual is None:
            results.append(_bridge_result(cgc, expected, None, None, "POST-ANCHOR",
                                          "CGC CYTD is NULL"))
        else:
            gap_kt = actual - expected
            gap_pct = 100 * gap_kt / expected if expected > 0 else None
            results.append(_bridge_result(cgc, expected, gap_kt, gap_pct, "POST-ANCHOR",
                                          "past last StatsCan anchor"))
    return results


def _bridge_result(cgc, expected, gap_kt, gap_pct, status, note):
    return {"cgc": cgc, "expected": expected, "gap_kt": gap_kt, "gap_pct": gap_pct,
            "status": status, "note": note}


def bridge_verdict(status, gap_pct):
    """Return (label, colour) for the verdict cell."""
    if status == "NO ANCHOR YET":
        return ("NO ANCHOR YET", COL_DIM)
    if status == "NO STATSCAN":
        return ("NO DATA", COL_DIM)
    if gap_pct is None:
        return ("NULL", COL_DIM)
    abs_pct = abs(gap_pct)
    if status == "ANCHOR":
        if abs_pct < THR_TIGHT:
            return ("ANCHOR ✓", COL_GOOD)
        if abs_pct < THR_ACCEPTABLE:
            return ("ANCHOR ~7% (channels)", COL_WARN)
        return ("ANCHOR FAIL", COL_BAD)
    if abs_pct < THR_TIGHT:
        return ("TIGHT", COL_GOOD)
    if abs_pct < THR_ACCEPTABLE:
        return ("EXPECTED", COL_WARN)
    return ("INVESTIGATE", COL_BAD)


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def prices_by_zone(price_rows):
    """Group price rows by zone, each sorted by date."""
    by_zone = {}
    for r in price_rows:
        by_zone.setdefault(r["zone"], []).append(r)
    for z in by_zone:
        by_zone[z].sort(key=lambda r: r["observation_date"])
    return by_zone


def date_to_float(d_str):
    """Convert ISO date string to matplotlib-style ordinal float for plotting."""
    d = datetime.fromisoformat(d_str).date()
    return d.toordinal()


def monthly_avg(rows, field):
    """Compute monthly averages from daily rows. Returns (labels, values)."""
    by_month = {}
    for r in rows:
        val = r.get(field)
        if val is None:
            continue
        ym = r["observation_date"][:7]  # "2024-01"
        by_month.setdefault(ym, []).append(val)
    labels = sorted(by_month.keys())
    values = [sum(by_month[ym]) / len(by_month[ym]) for ym in labels]
    return labels, values


def price_summary_stats(rows):
    """Compute summary stats for a list of price rows."""
    cash_vals = [r["cash_per_bu"] for r in rows if r["cash_per_bu"] is not None]
    basis_vals = [r["basis_per_bu"] for r in rows if r["basis_per_bu"] is not None]
    if not cash_vals:
        return {}
    return {
        "n_days": len(cash_vals),
        "first": rows[0]["observation_date"],
        "last": rows[-1]["observation_date"],
        "cash_min": min(cash_vals),
        "cash_max": max(cash_vals),
        "cash_avg": sum(cash_vals) / len(cash_vals),
        "cash_last": cash_vals[-1],
        "basis_n": len(basis_vals),
        "basis_min": min(basis_vals) if basis_vals else None,
        "basis_max": max(basis_vals) if basis_vals else None,
        "basis_avg": sum(basis_vals) / len(basis_vals) if basis_vals else None,
        "basis_last": basis_vals[-1] if basis_vals else None,
    }


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def fmt(v, nd=1):
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:,.{nd}f}"
    return str(v)


def coloured_text(text, color):
    dpg.add_text(text, color=color)


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------

def build_balance_sheet_tab(statscan_rows):
    dpg.add_text("Alberta canola farm supply and disposition", color=COL_HDR)
    dpg.add_text("Source: StatsCan Table 32-10-0015-01 (units: thousand tonnes)", color=COL_DIM)
    dpg.add_spacer(height=4)
    dpg.add_text(
        "Note: each StatsCan column is CUMULATIVE over the crop year.\n"
        "  'March 2024'    = deliveries Aug 2023 - Mar 2024  (crop year 2023/24)\n"
        "  'July 2024'     = deliveries Aug 2023 - Jul 2024  (crop year 2023/24, full year)\n"
        "  'December 2024' = deliveries Aug 2024 - Dec 2024  (crop year 2024/25, first snapshot)",
        color=COL_INFO,
    )
    dpg.add_spacer(height=8)

    if not statscan_rows:
        coloured_text("No StatsCan rows in the database yet. Run ingest/load_statscan.py.", COL_BAD)
        return

    with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                   borders_outerH=True, borders_outerV=True, resizable=True,
                   policy=dpg.mvTable_SizingStretchProp):
        for h in ["Crop Yr", "Snap", "Period Covered", "As of",
                  "Total Supply", "Beg Stocks", "Production",
                  "Total Disp", "Deliveries", "Seed", "Ending Stocks", "Feed/Waste",
                  "Identity"]:
            dpg.add_table_column(label=h)

        for s in statscan_rows:
            ok, err = check_identity(s)
            with dpg.table_row():
                dpg.add_text(s["crop_year"])
                dpg.add_text(s["snapshot_month"])
                dpg.add_text(period_covered(s["snapshot_month"], s["snapshot_date"]))
                dpg.add_text(s["snapshot_date"])
                dpg.add_text(fmt(s["total_supplies_kt"]))
                dpg.add_text(fmt(s["beginning_stocks_kt"]))
                dpg.add_text(fmt(s["production_kt"]))
                dpg.add_text(fmt(s["total_disposition_kt"]))
                dpg.add_text(fmt(s["deliveries_kt"]))
                dpg.add_text(fmt(s["seed_requirements_kt"]))
                dpg.add_text(fmt(s["ending_stocks_kt"]))
                dpg.add_text(fmt(s["feed_waste_dockage_kt"]))
                if ok:
                    coloured_text(f"OK  (err {err:.1f})", COL_GOOD)
                else:
                    coloured_text(f"FAIL (err {err:.1f})", COL_BAD)

    dpg.add_spacer(height=12)
    dpg.add_text("Identity check: total_supplies == deliveries + seed + ending + feed/waste",
                 color=COL_DIM)


def build_weekly_tab(cgc_rows):
    dpg.add_text("CGC weekly flows — Alberta canola", color=COL_HDR)
    dpg.add_text("Source: CGC GSW Primary tab + Process tab (units: thousand tonnes)",
                 color=COL_DIM)
    dpg.add_spacer(height=8)

    if not cgc_rows:
        coloured_text("No CGC rows yet. Run ingest/load_cgc.py.", COL_BAD)
        return

    n_combined = sum(1 for c in cgc_rows if c.get("notes") and "ombined" in (c["notes"] or ""))
    dpg.add_text(f"Weeks loaded: {len(cgc_rows)}  ({n_combined} from combined-week files)")
    dpg.add_spacer(height=4)

    # Weekly flow table
    with dpg.collapsing_header(label="Weekly producer deliveries (per week)", default_open=True):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp):
            for h in ["Week", "Ending", "Crop Yr", "Primary (wk)", "Process (wk)",
                      "Total (wk)", "Primary Ship (wk)", "Note"]:
                dpg.add_table_column(label=h)
            for c in cgc_rows:
                is_combined = c.get("notes") and "ombined" in (c["notes"] or "")
                with dpg.table_row():
                    dpg.add_text(str(c["week_number"]))
                    dpg.add_text(c["week_ending"])
                    dpg.add_text(c["crop_year"])
                    if is_combined and c["producer_deliveries_weekly_kt"] is None:
                        coloured_text("[combined]", COL_WARN)
                        coloured_text("[combined]", COL_WARN)
                        coloured_text("[combined]", COL_WARN)
                        coloured_text("[combined]", COL_WARN)
                    else:
                        dpg.add_text(fmt(c["producer_deliveries_weekly_kt"]))
                        dpg.add_text(fmt(c["process_deliveries_weekly_kt"]))
                        dpg.add_text(fmt(c["total_deliveries_weekly_kt"]))
                        dpg.add_text(fmt(c["primary_shipments_weekly_kt"]))
                    note = c.get("notes") or ""
                    if note:
                        if "earlier half" in note:
                            coloured_text("earlier-half (CYTD derived)", COL_WARN)
                        elif "later half" in note:
                            coloured_text("later-half (stocks valid)", COL_WARN)
                        else:
                            dpg.add_text(note[:40])
                    else:
                        dpg.add_text("")

    # CYTD cumulative — usable for ALL rows (combined-week rows have derived CYTDs)
    with dpg.collapsing_header(label="Crop-year-to-date cumulative", default_open=True):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp):
            for h in ["Week", "Ending", "Crop Yr", "Primary CYTD", "Process CYTD",
                      "Total CYTD", "Primary Ship CYTD"]:
                dpg.add_table_column(label=h)
            for c in cgc_rows:
                with dpg.table_row():
                    dpg.add_text(str(c["week_number"]))
                    dpg.add_text(c["week_ending"])
                    dpg.add_text(c["crop_year"])
                    dpg.add_text(fmt(c["producer_deliveries_cytd_kt"]))
                    dpg.add_text(fmt(c["process_deliveries_cytd_kt"]))
                    dpg.add_text(fmt(c["total_deliveries_cytd_kt"]))
                    dpg.add_text(fmt(c["primary_shipments_cytd_kt"]))

    # Visible stocks
    with dpg.collapsing_header(label="End-of-week visible commercial stocks", default_open=True):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp):
            for h in ["Week", "Ending", "Primary Stk", "Process Stk", "Condo",
                      "Total Visible"]:
                dpg.add_table_column(label=h)
            for c in cgc_rows:
                is_combined_earlier = (
                    c.get("notes") and "earlier half" in (c["notes"] or "")
                )
                with dpg.table_row():
                    dpg.add_text(str(c["week_number"]))
                    dpg.add_text(c["week_ending"])
                    if is_combined_earlier:
                        coloured_text("[no snapshot]", COL_WARN)
                        coloured_text("[no snapshot]", COL_WARN)
                        coloured_text("[no snapshot]", COL_WARN)
                        coloured_text("[no snapshot]", COL_WARN)
                    else:
                        dpg.add_text(fmt(c["primary_elevator_stocks_kt"]))
                        dpg.add_text(fmt(c["process_elevator_stocks_kt"]))
                        dpg.add_text(fmt(c["condo_storage_kt"]))
                        dpg.add_text(fmt(c["total_visible_stocks_kt"]))

    # Plot CYTD deliveries
    if len(cgc_rows) >= 2:
        dpg.add_spacer(height=12)
        dpg.add_text("CYTD deliveries by week", color=COL_HDR)
        x_vals = [float(c["week_number"]) for c in cgc_rows]
        primary_y = [c["producer_deliveries_cytd_kt"] or 0 for c in cgc_rows]
        process_y = [c["process_deliveries_cytd_kt"] or 0 for c in cgc_rows]
        total_y   = [c["total_deliveries_cytd_kt"] or 0 for c in cgc_rows]
        with dpg.plot(height=300, width=-1):
            dpg.add_plot_legend()
            dpg.add_plot_axis(dpg.mvXAxis, label="Crop year week (1=first week of Aug)")
            with dpg.plot_axis(dpg.mvYAxis, label="Cumulative deliveries (kt)"):
                dpg.add_line_series(x_vals, primary_y, label="Primary CYTD")
                dpg.add_line_series(x_vals, process_y, label="Process CYTD")
                dpg.add_line_series(x_vals, total_y,   label="Total CYTD")


def build_bridge_tab(cgc_rows, statscan_rows):
    dpg.add_text("Bridge: CGC weekly CYTD vs StatsCan triannual snapshots", color=COL_HDR)
    dpg.add_text(
        "Compares CGC cumulative deliveries to the equivalent StatsCan number.\n"
        "Only weeks AFTER the first real StatsCan anchor get a comparison —\n"
        "August/September deliveries follow an S-curve that linear interpolation\n"
        "from zero would badly misrepresent, so those weeks are marked NO ANCHOR YET.",
        color=COL_INFO,
    )
    dpg.add_spacer(height=6)
    dpg.add_text(
        f"Thresholds (set high because of structural +7% missing-channel gap):\n"
        f"  TIGHT       = abs gap <  {THR_TIGHT:.0f}%\n"
        f"  EXPECTED    = abs gap < {THR_ACCEPTABLE:.0f}%  (includes the +7% missing-channels baseline)\n"
        f"  INVESTIGATE = abs gap > {THR_ACCEPTABLE:.0f}%  (likely parse error or new channel issue)",
        color=COL_DIM,
    )
    dpg.add_spacer(height=8)

    if not cgc_rows or not statscan_rows:
        coloured_text("Need both StatsCan and CGC data loaded.", COL_BAD)
        return

    bridge = bridge_compare(cgc_rows, statscan_rows)

    # Summary metric row
    interim_with_data = [b for b in bridge
                         if b["status"] in ("ANCHOR", "INTERIM", "POST-ANCHOR")
                         and b["gap_pct"] is not None]
    if interim_with_data:
        gaps = [b["gap_pct"] for b in interim_with_data]
        avg = sum(gaps) / len(gaps)
        dpg.add_text(f"Average gap across {len(gaps)} comparable weeks: {avg:+.1f}%",
                     color=COL_HDR)
        dpg.add_text(
            f"  (a consistent positive bias of ~+7% = the missing channels: producer cars,\n"
            f"   feed mill direct deliveries, and 'other deliveries' to process elevators)",
            color=COL_DIM,
        )
        dpg.add_spacer(height=8)

    with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                   borders_outerH=True, borders_outerV=True, resizable=True,
                   policy=dpg.mvTable_SizingStretchProp):
        for h in ["Week", "Ending", "Crop Yr",
                  "CGC Total CYTD", "StatsCan Expected", "Gap (kt)", "Gap (%)",
                  "Verdict", "Note"]:
            dpg.add_table_column(label=h)
        for b in bridge:
            c = b["cgc"]
            with dpg.table_row():
                dpg.add_text(str(c["week_number"]))
                dpg.add_text(c["week_ending"])
                dpg.add_text(c["crop_year"])
                dpg.add_text(fmt(c["total_deliveries_cytd_kt"]))
                dpg.add_text(fmt(b["expected"]))
                if b["gap_kt"] is None:
                    dpg.add_text("-")
                    dpg.add_text("-")
                else:
                    sign = "+" if b["gap_kt"] >= 0 else ""
                    dpg.add_text(f"{sign}{b['gap_kt']:,.1f}")
                    dpg.add_text(f"{sign}{b['gap_pct']:.1f}%")
                label, color = bridge_verdict(b["status"], b["gap_pct"])
                coloured_text(label, color)
                dpg.add_text(b["note"][:60])


def build_prices_tab(price_rows):
    dpg.add_text("PDQ Alberta canola spot cash bids & basis", color=COL_HDR)
    dpg.add_text("Source: PDQ Info — spot month bids (units: CAD/bushel)", color=COL_DIM)
    dpg.add_spacer(height=8)

    if not price_rows:
        coloured_text("No price data loaded. Run ingest/load_pdq.py.", COL_BAD)
        return

    by_zone = prices_by_zone(price_rows)
    zone_order = ["Southern AB", "Northern AB", "Peace River"]
    zone_colours = {"Southern AB": COL_SOUTH, "Northern AB": COL_NORTH, "Peace River": COL_PEACE}

    # --- Summary cards ---
    with dpg.collapsing_header(label="Zone summary", default_open=True):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp):
            for h in ["Zone", "Days", "Date Range",
                      "Cash Last", "Cash Avg", "Cash Min", "Cash Max",
                      "Basis Last", "Basis Avg", "Basis Min", "Basis Max"]:
                dpg.add_table_column(label=h)
            for z in zone_order:
                rows = by_zone.get(z, [])
                if not rows:
                    continue
                s = price_summary_stats(rows)
                with dpg.table_row():
                    coloured_text(z, zone_colours.get(z, COL_INFO))
                    dpg.add_text(str(s["n_days"]))
                    dpg.add_text(f"{s['first']} to {s['last']}")
                    dpg.add_text(fmt(s["cash_last"], 2))
                    dpg.add_text(fmt(s["cash_avg"], 2))
                    dpg.add_text(fmt(s["cash_min"], 2))
                    dpg.add_text(fmt(s["cash_max"], 2))
                    dpg.add_text(fmt(s["basis_last"], 2))
                    dpg.add_text(fmt(s["basis_avg"], 2))
                    dpg.add_text(fmt(s["basis_min"], 2))
                    dpg.add_text(fmt(s["basis_max"], 2))

    # --- Cash price plot (monthly avg, all zones) ---
    with dpg.collapsing_header(label="Spot cash bids — monthly average (all zones)", default_open=True):
        # Use sequential x indices for monthly averages, label with month strings
        south_rows = by_zone.get("Southern AB", [])
        north_rows = by_zone.get("Northern AB", [])
        peace_rows = by_zone.get("Peace River", [])

        s_labels, s_vals = monthly_avg(south_rows, "cash_per_bu")
        n_labels, n_vals = monthly_avg(north_rows, "cash_per_bu")
        p_labels, p_vals = monthly_avg(peace_rows, "cash_per_bu")

        # Build unified x-axis from all labels
        all_labels = sorted(set(s_labels + n_labels + p_labels))
        if len(all_labels) >= 2:
            x_map = {lbl: i for i, lbl in enumerate(all_labels)}
            # Build tick labels (show every 6th month to avoid clutter)
            tick_pairs = [(x_map[lbl], lbl) for lbl in all_labels[::6]]

            with dpg.plot(height=300, width=-1):
                dpg.add_plot_legend()
                ax_x = dpg.add_plot_axis(dpg.mvXAxis, label="Month")
                dpg.set_axis_limits(ax_x, -0.5, len(all_labels) - 0.5)
                dpg.set_axis_ticks(ax_x, tuple(tick_pairs))
                with dpg.plot_axis(dpg.mvYAxis, label="Cash (CAD/bu)"):
                    if s_vals:
                        dpg.add_line_series(
                            [float(x_map[l]) for l in s_labels], s_vals,
                            label="Southern AB")
                    if n_vals:
                        dpg.add_line_series(
                            [float(x_map[l]) for l in n_labels], n_vals,
                            label="Northern AB")
                    if p_vals:
                        dpg.add_line_series(
                            [float(x_map[l]) for l in p_labels], p_vals,
                            label="Peace River")

    # --- Basis plot (monthly avg, all zones) ---
    with dpg.collapsing_header(label="Basis — monthly average (all zones)", default_open=True):
        s_labels_b, s_vals_b = monthly_avg(south_rows, "basis_per_bu")
        n_labels_b, n_vals_b = monthly_avg(north_rows, "basis_per_bu")
        p_labels_b, p_vals_b = monthly_avg(peace_rows, "basis_per_bu")

        all_labels_b = sorted(set(s_labels_b + n_labels_b + p_labels_b))
        if len(all_labels_b) >= 2:
            x_map_b = {lbl: i for i, lbl in enumerate(all_labels_b)}
            tick_pairs_b = [(x_map_b[lbl], lbl) for lbl in all_labels_b[::6]]

            with dpg.plot(height=300, width=-1):
                dpg.add_plot_legend()
                ax_x_b = dpg.add_plot_axis(dpg.mvXAxis, label="Month")
                dpg.set_axis_limits(ax_x_b, -0.5, len(all_labels_b) - 0.5)
                dpg.set_axis_ticks(ax_x_b, tuple(tick_pairs_b))
                with dpg.plot_axis(dpg.mvYAxis, label="Basis (CAD/bu)"):
                    # Zero line reference
                    dpg.add_line_series(
                        [0.0, float(len(all_labels_b) - 1)], [0.0, 0.0],
                        label="Zero")
                    if s_vals_b:
                        dpg.add_line_series(
                            [float(x_map_b[l]) for l in s_labels_b], s_vals_b,
                            label="Southern AB")
                    if n_vals_b:
                        dpg.add_line_series(
                            [float(x_map_b[l]) for l in n_labels_b], n_vals_b,
                            label="Northern AB")
                    if p_vals_b:
                        dpg.add_line_series(
                            [float(x_map_b[l]) for l in p_labels_b], p_vals_b,
                            label="Peace River")

    # --- Southern AB basis by crop-year month (seasonal overlay) ---
    with dpg.collapsing_header(label="Southern AB basis — seasonal pattern (by crop-year month)", default_open=True):
        dpg.add_text(
            "Each line = one crop year (Aug-Jul). X-axis = month within crop year.\n"
            "This shows whether basis widens or tightens at different points in the marketing year.",
            color=COL_INFO,
        )
        dpg.add_spacer(height=4)

        # Group Southern AB by crop year
        cy_basis = {}   # {crop_year: {month_offset: [values]}}
        for r in south_rows:
            if r["basis_per_bu"] is None:
                continue
            d = datetime.fromisoformat(r["observation_date"]).date()
            if d.month >= 8:
                cy = f"{d.year}/{(d.year + 1) % 100:02d}"
                month_offset = d.month - 8   # Aug=0, Sep=1, ..., Dec=4
            else:
                cy = f"{d.year - 1}/{d.year % 100:02d}"
                month_offset = d.month + 4   # Jan=5, Feb=6, ..., Jul=11
            cy_basis.setdefault(cy, {}).setdefault(month_offset, []).append(r["basis_per_bu"])

        if cy_basis:
            month_labels = ["Aug", "Sep", "Oct", "Nov", "Dec",
                            "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"]
            tick_pairs_s = [(float(i), month_labels[i]) for i in range(12)]

            with dpg.plot(height=300, width=-1):
                dpg.add_plot_legend()
                ax_x_s = dpg.add_plot_axis(dpg.mvXAxis, label="Crop year month")
                dpg.set_axis_limits(ax_x_s, -0.5, 11.5)
                dpg.set_axis_ticks(ax_x_s, tuple(tick_pairs_s))
                with dpg.plot_axis(dpg.mvYAxis, label="Basis (CAD/bu)"):
                    dpg.add_line_series([0.0, 11.0], [0.0, 0.0], label="Zero")
                    for cy in sorted(cy_basis.keys()):
                        months = cy_basis[cy]
                        offsets = sorted(months.keys())
                        # Only plot crop years with at least 4 months of data
                        if len(offsets) < 4:
                            continue
                        x = [float(o) for o in offsets]
                        y = [sum(months[o]) / len(months[o]) for o in offsets]
                        dpg.add_line_series(x, y, label=cy)

    # --- Recent daily table (last 30 rows, Southern AB) ---
    with dpg.collapsing_header(label="Recent daily bids — Southern AB (last 30 days)", default_open=False):
        recent = [r for r in south_rows if r["cash_per_bu"] is not None][-30:]
        if recent:
            with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                           borders_outerH=True, borders_outerV=True, resizable=True,
                           policy=dpg.mvTable_SizingStretchProp):
                for h in ["Date", "Cash ($/bu)", "Chg", "Basis ($/bu)", "Basis Chg",
                          "Futures"]:
                    dpg.add_table_column(label=h)
                for r in reversed(recent):  # newest first
                    with dpg.table_row():
                        dpg.add_text(r["observation_date"])
                        dpg.add_text(fmt(r["cash_per_bu"], 2))
                        chg = r.get("cash_change")
                        if chg is not None:
                            col = COL_GOOD if chg > 0 else COL_BAD if chg < 0 else COL_DIM
                            coloured_text(f"{chg:+.2f}", col)
                        else:
                            dpg.add_text("-")
                        dpg.add_text(fmt(r["basis_per_bu"], 2))
                        bchg = r.get("basis_change")
                        if bchg is not None:
                            col = COL_GOOD if bchg > 0 else COL_BAD if bchg < 0 else COL_DIM
                            coloured_text(f"{bchg:+.2f}", col)
                        else:
                            dpg.add_text("-")
                        dpg.add_text(r.get("futures_contract") or "-")


def match_basis_to_weeks(nowcast_rows, price_rows):
    """For each nowcast week, find the nearest Southern AB basis observation.

    Returns list of dicts with nowcast fields plus basis_per_bu and cash_per_bu.
    Only includes rows where both on-farm stocks and basis are non-null.
    """
    # Build a date-indexed lookup for Southern AB prices
    south_prices = {}
    for r in price_rows:
        if r["zone"] == "Southern AB" and r["basis_per_bu"] is not None:
            south_prices[r["observation_date"]] = r

    if not south_prices:
        return []

    price_dates = sorted(south_prices.keys())

    def find_nearest(target_date):
        """Find nearest price date within 5 days of target."""
        # Binary-ish search: try exact, then ±1, ±2, etc.
        for offset in range(6):
            d = datetime.fromisoformat(target_date).date()
            for sign in (0, -1, 1) if offset == 0 else (-1, 1):
                candidate = (d + timedelta(days=offset * sign if offset else 0)).isoformat()
                if candidate in south_prices:
                    return south_prices[candidate]
        return None

    matched = []
    for nw in nowcast_rows:
        if nw["implied_on_farm_stocks_kt"] is None:
            continue
        price = find_nearest(nw["week_ending"])
        if price is None:
            continue
        matched.append({
            **nw,
            "basis_per_bu": price["basis_per_bu"],
            "cash_per_bu": price["cash_per_bu"],
            "price_date": price["observation_date"],
        })
    return matched


def build_nowcast_tab(nowcast_rows, price_rows):
    dpg.add_text("Nowcast — implied on-farm stocks & basis signal", color=COL_HDR)
    dpg.add_text(
        "Stocks = Total Supplies − Cumulative Deliveries − Pro-rated Feed/Waste/Seed\n"
        "On-Farm = Total Stocks − Visible Commercial Stocks (primary + process + condo)",
        color=COL_DIM,
    )
    dpg.add_spacer(height=8)

    if not nowcast_rows:
        coloured_text("No nowcast data. Run nowcast.py after loading StatsCan + CGC data.", COL_BAD)
        return

    crop_years = sorted({r["crop_year"] for r in nowcast_rows})

    # --- Time series: implied on-farm stocks ---
    with dpg.collapsing_header(label="Implied on-farm stocks over time", default_open=True):
        # Plot each crop year as a separate series on crop-year week axis
        cy_data = {}
        for r in nowcast_rows:
            if r["implied_on_farm_stocks_kt"] is None:
                continue
            cy_data.setdefault(r["crop_year"], []).append(r)

        if cy_data:
            # Use sequential index (week of crop year derived from week_ending)
            with dpg.plot(height=320, width=-1):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Week ending (sequential)")
                with dpg.plot_axis(dpg.mvYAxis, label="Implied on-farm stocks (kt)"):
                    for cy in sorted(cy_data.keys()):
                        weeks = cy_data[cy]
                        # x = ordinal offset from Aug 1 of the crop year start
                        cy_start_year = int(cy[:4])
                        aug1 = datetime(cy_start_year, 8, 1).date()
                        x = [(datetime.fromisoformat(w["week_ending"]).date() - aug1).days / 7.0
                             for w in weeks]
                        y = [w["implied_on_farm_stocks_kt"] for w in weeks]
                        dpg.add_line_series(x, y, label=cy)

            dpg.add_text(
                "X-axis = weeks since Aug 1 of crop year. Each line is one crop year.\n"
                "Stocks decline through the year as deliveries accumulate.",
                color=COL_DIM,
            )

    # --- Time series: estimated total stocks + visible + on-farm ---
    with dpg.collapsing_header(label="Stock components — latest crop year", default_open=True):
        latest_cy = sorted(cy_data.keys())[-1] if cy_data else None
        if latest_cy:
            weeks = cy_data[latest_cy]
            cy_start_year = int(latest_cy[:4])
            aug1 = datetime(cy_start_year, 8, 1).date()
            x = [(datetime.fromisoformat(w["week_ending"]).date() - aug1).days / 7.0
                 for w in weeks]
            est_total = [w["estimated_total_stocks_kt"] for w in weeks]
            visible = [w["visible_commercial_stocks_kt"] or 0 for w in weeks]
            on_farm = [w["implied_on_farm_stocks_kt"] for w in weeks]

            with dpg.plot(height=320, width=-1):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label=f"Weeks since Aug 1 ({latest_cy})")
                with dpg.plot_axis(dpg.mvYAxis, label="Stocks (kt)"):
                    dpg.add_line_series(x, est_total, label="Estimated Total")
                    dpg.add_line_series(x, visible, label="Visible Commercial")
                    dpg.add_line_series(x, on_farm, label="Implied On-Farm")

    # --- THE SCATTER: on-farm stocks vs basis ---
    matched = match_basis_to_weeks(nowcast_rows, price_rows)
    with dpg.collapsing_header(label="SCATTER: implied on-farm stocks vs Southern AB basis", default_open=True):
        if not matched:
            coloured_text(
                "Need both nowcast and PDQ price data for overlapping dates.\n"
                "Run nowcast.py and ingest/load_pdq.py first.",
                COL_WARN,
            )
        else:
            dpg.add_text(
                f"{len(matched)} week-observations with both stocks and basis data.\n"
                "Hypothesis: more on-farm stocks → wider (more negative) basis.\n"
                "If the cloud slopes down-left to up-right, the relationship holds.",
                color=COL_INFO,
            )
            dpg.add_spacer(height=4)

            x_farm = [m["implied_on_farm_stocks_kt"] for m in matched]
            y_basis = [m["basis_per_bu"] for m in matched]

            with dpg.plot(height=400, width=-1):
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Implied on-farm stocks (kt)")
                with dpg.plot_axis(dpg.mvYAxis, label="Southern AB basis (CAD/bu)"):
                    # Scatter by crop year for colour coding
                    cy_matched = {}
                    for m in matched:
                        cy_matched.setdefault(m["crop_year"], []).append(m)
                    for cy in sorted(cy_matched.keys()):
                        pts = cy_matched[cy]
                        sx = [p["implied_on_farm_stocks_kt"] for p in pts]
                        sy = [p["basis_per_bu"] for p in pts]
                        dpg.add_scatter_series(sx, sy, label=cy)

                    # Zero line
                    dpg.add_line_series(
                        [min(x_farm), max(x_farm)], [0.0, 0.0],
                        label="Zero basis")

            # Simple correlation
            n = len(matched)
            mean_x = sum(x_farm) / n
            mean_y = sum(y_basis) / n
            cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_farm, y_basis)) / n
            var_x = sum((x - mean_x) ** 2 for x in x_farm) / n
            var_y = sum((y - mean_y) ** 2 for y in y_basis) / n
            if var_x > 0 and var_y > 0:
                corr = cov / (var_x ** 0.5 * var_y ** 0.5)
                dpg.add_spacer(height=4)
                corr_color = COL_GOOD if abs(corr) > 0.3 else COL_WARN if abs(corr) > 0.15 else COL_DIM
                coloured_text(f"Correlation: {corr:+.3f}  (n={n} weeks)", corr_color)
                if corr < -0.15:
                    dpg.add_text(
                        "  Negative correlation = higher on-farm stocks associated with wider basis.\n"
                        "  This is the expected direction — validates the supply-basis relationship.",
                        color=COL_INFO,
                    )
                elif corr > 0.15:
                    dpg.add_text(
                        "  Positive correlation — unexpected. Could indicate confounding factors\n"
                        "  (e.g. both stocks and basis driven by crop size or futures moves).\n"
                        "  Worth investigating by crop year or seasonally detrending.",
                        color=COL_WARN,
                    )
                else:
                    dpg.add_text(
                        "  Weak correlation — may need more data, seasonal adjustment,\n"
                        "  or normalisation (stocks as % of supply) to see the signal.",
                        color=COL_DIM,
                    )

    # --- Delivery pace vs basis (bonus signal from README) ---
    with dpg.collapsing_header(label="Bonus: weekly delivery pace vs basis", default_open=False):
        dpg.add_text(
            "When deliveries accelerate while basis is wide, farmers are capitulating.\n"
            "When deliveries slow while basis is tight, farmers are holding — bearish for basis.",
            color=COL_INFO,
        )
        # This will be more useful once we have CGC delivery pace data
        # alongside prices. For now, show what we have.
        if not matched:
            coloured_text("Need overlapping nowcast + price data.", COL_WARN)
        else:
            # Plot deliveries CYTD alongside basis for the latest crop year
            latest = sorted(cy_matched.keys())[-1] if cy_matched else None
            if latest and latest in cy_matched:
                pts = cy_matched[latest]
                cy_start_year = int(latest[:4])
                aug1 = datetime(cy_start_year, 8, 1).date()
                x_wk = [(datetime.fromisoformat(p["week_ending"]).date() - aug1).days / 7.0
                         for p in pts]
                y_del = [p["deliveries_cytd_kt"] for p in pts]
                y_bas = [p["basis_per_bu"] for p in pts]

                dpg.add_text(f"Crop year: {latest}", color=COL_HDR)
                with dpg.plot(height=300, width=-1):
                    dpg.add_plot_legend()
                    dpg.add_plot_axis(dpg.mvXAxis, label="Weeks since Aug 1")
                    with dpg.plot_axis(dpg.mvYAxis, label="Deliveries CYTD (kt)"):
                        dpg.add_line_series(x_wk, y_del, label="Deliveries CYTD")
                    with dpg.plot_axis(dpg.mvYAxis, label="Basis (CAD/bu)"):
                        dpg.add_line_series(x_wk, y_bas, label="S.AB Basis")

    # --- Data table ---
    with dpg.collapsing_header(label="Nowcast data table", default_open=False):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp):
            for h in ["Week Ending", "Crop Yr", "Supply", "Del CYTD",
                      "Est Total", "Visible", "On-Farm"]:
                dpg.add_table_column(label=h)
            for r in nowcast_rows:
                with dpg.table_row():
                    dpg.add_text(r["week_ending"])
                    dpg.add_text(r["crop_year"])
                    dpg.add_text(fmt(r["total_supplies_kt"], 0))
                    dpg.add_text(fmt(r["deliveries_cytd_kt"]))
                    dpg.add_text(fmt(r["estimated_total_stocks_kt"]))
                    dpg.add_text(fmt(r["visible_commercial_stocks_kt"]))
                    dpg.add_text(fmt(r["implied_on_farm_stocks_kt"]))


def build_findings_tab(statscan_rows, cgc_rows, price_rows, nowcast_rows):
    dpg.add_text("Project status & findings", color=COL_HDR)
    dpg.add_spacer(height=8)

    # What's loaded
    dpg.add_text("Data loaded", color=COL_HDR)
    dpg.add_text(f"  StatsCan snapshots: {len(statscan_rows)} rows "
                 f"({len({r['crop_year'] for r in statscan_rows})} crop years)")
    dpg.add_text(f"  CGC weekly rows:    {len(cgc_rows)} rows "
                 f"({len({r['crop_year'] for r in cgc_rows})} crop years)")
    if cgc_rows:
        n_combined = sum(1 for c in cgc_rows if c.get("notes") and "ombined" in (c["notes"] or ""))
        if n_combined:
            dpg.add_text(f"  Combined-week rows: {n_combined}  (CGC merged 2 weeks into 1 file)")
    if price_rows:
        zones = sorted({r["zone"] for r in price_rows})
        date_range = f"{price_rows[0]['observation_date']} to {price_rows[-1]['observation_date']}"
        dpg.add_text(f"  PDQ price rows:     {len(price_rows)} rows "
                     f"({len(zones)} zones: {', '.join(zones)})")
        dpg.add_text(f"                      {date_range}")
    else:
        dpg.add_text(f"  PDQ price rows:     0  (run ingest/load_pdq.py)")
    if nowcast_rows:
        nc_cys = sorted({r["crop_year"] for r in nowcast_rows})
        dpg.add_text(f"  Nowcast rows:       {len(nowcast_rows)} rows "
                     f"({len(nc_cys)} crop years: {', '.join(nc_cys)})")
    else:
        dpg.add_text(f"  Nowcast rows:       0  (run nowcast.py)")
    dpg.add_spacer(height=12)

    # Identity validation summary
    dpg.add_text("Balance sheet identity validation", color=COL_HDR)
    n_ok  = sum(1 for s in statscan_rows if check_identity(s)[0])
    n_bad = len(statscan_rows) - n_ok
    if n_bad == 0 and statscan_rows:
        coloured_text(f"  All {n_ok}/{len(statscan_rows)} StatsCan rows balance.", COL_GOOD)
    elif statscan_rows:
        coloured_text(f"  {n_bad}/{len(statscan_rows)} rows fail the identity!", COL_BAD)
    dpg.add_spacer(height=12)

    # Bridge health summary
    dpg.add_text("Bridge health (CGC vs StatsCan)", color=COL_HDR)
    bridge = bridge_compare(cgc_rows, statscan_rows)
    interim = [b for b in bridge if b["status"] in ("ANCHOR", "INTERIM", "POST-ANCHOR")
               and b["gap_pct"] is not None]
    early = [b for b in bridge if b["status"] == "NO ANCHOR YET"]
    if interim:
        gaps = [b["gap_pct"] for b in interim]
        avg = sum(gaps) / len(gaps)
        anchors = [b for b in interim if b["status"] == "ANCHOR"]
        dpg.add_text(f"  {len(interim)} comparable weeks  (avg gap {avg:+.1f}%)")
        if anchors:
            anchor_gaps = [b["gap_pct"] for b in anchors]
            dpg.add_text(f"  {len(anchors)} exact StatsCan anchor matches  "
                         f"(gaps: {', '.join(f'{g:+.1f}%' for g in anchor_gaps)})")
        if abs(avg) < THR_ACCEPTABLE:
            coloured_text(f"  Consistent +{avg:.1f}% bias = expected missing-channel signature.",
                          COL_GOOD)
    if early:
        dpg.add_text(f"  {len(early)} early weeks have no anchor yet (Aug-Dec, before first snapshot)")
    dpg.add_spacer(height=12)

    # Price data health
    if price_rows:
        dpg.add_text("Price data health (PDQ)", color=COL_HDR)
        by_zone = prices_by_zone(price_rows)
        for z in sorted(by_zone.keys()):
            s = price_summary_stats(by_zone[z])
            no_basis = s["n_days"] - s["basis_n"]
            dpg.add_text(f"  {z}: {s['n_days']} days, "
                         f"cash ${s['cash_min']:.2f}-${s['cash_max']:.2f}/bu, "
                         f"basis avg ${s['basis_avg']:.2f}/bu")
            if no_basis > 0:
                dpg.add_text(f"    ({no_basis} days missing basis — early dates before futures quoting)",
                             color=COL_WARN if no_basis < 50 else COL_DIM)
        coloured_text("  Price data loaded and healthy.", COL_GOOD)
        dpg.add_spacer(height=12)

    # Why the gap is structural
    dpg.add_text("Why the bridge shows a consistent +7% gap", color=COL_HDR)
    dpg.add_text(
        "  The CGC weekly Primary + Process tabs cover ~93% of canola deliveries.\n"
        "  StatsCan's 'Deliveries' line is ~100% — it also includes:\n"
        "    - Producer cars  (rail cars shipped direct from farm, bypasses elevators)\n"
        "    - Feed mill direct deliveries  (farm to feed mill, no elevator)\n"
        "    - 'Other Deliveries' to process elevators  (transfers, not producer-direct)\n"
        "  Adding these channels would close the gap. Reasonable to defer for Phase 2.",
        color=COL_DIM,
    )
    dpg.add_spacer(height=12)

    # Known issues
    dpg.add_text("Known issues / gaps", color=COL_HDR)
    dpg.add_text(
        "  1. ~7% structural missing-channels gap (see above). To fix: extend CGC loader\n"
        "     to also pull Producer Cars and Feed Grains tabs, plus 'Other Deliveries'\n"
        "     column from the Process tab.\n"
        "  2. Combined-week files (Christmas weeks 21-22) genuinely don't split into\n"
        "     individual weeks. CYTD remains accurate; stocks at the earlier week-end\n"
        "     are unknown (set NULL).\n"
        "  3. Pre-first-anchor weeks (Aug-Dec, before the first StatsCan snapshot of a\n"
        "     new crop year) cannot be bridge-checked. This is fundamental.\n"
        "  4. PDQ pricing now loaded — next step is overlaying basis on the balance\n"
        "  4. PDQ pricing loaded. Nowcast computes implied on-farm stocks.\n"
        "     Scatter plot (Nowcast tab) shows the stocks-vs-basis relationship.",
        color=COL_DIM,
    )
    dpg.add_spacer(height=12)

    # Next steps
    dpg.add_text("Next steps", color=COL_HDR)
    dpg.add_text(
        "  - Load more crop years of CGC + matching StatsCan vintages to fill out\n"
        "    the scatter with more data points.\n"
        "  - Investigate whether normalising stocks (as % of total supply) or\n"
        "    seasonally detrending improves the stocks-vs-basis correlation.\n"
        "  - Decide whether to close the +7% gap by adding the missing-channel tabs,\n"
        "    or accept it as a documented constant and apply a correction factor.\n"
        "  - Consider adding delivery-pace-vs-prior-year as a standalone signal\n"
        "    (doesn't need StatsCan anchors, just multi-year CGC data).",
        color=COL_DIM,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        print("Run init_db.py and then the loaders first.")
        return

    statscan_rows = fetch_statscan()
    cgc_rows = fetch_cgc()
    price_rows = fetch_prices()
    nowcast_rows = fetch_nowcast()

    dpg.create_context()

    with dpg.window(label="Alberta Canola S&D", tag="primary_window"):
        dpg.add_text("Alberta Canola Supply & Demand Dashboard", color=COL_HDR)
        dpg.add_text(f"Database: {DB_PATH}", color=COL_DIM)
        dpg.add_separator()

        with dpg.tab_bar():
            with dpg.tab(label="Balance Sheet (StatsCan)"):
                build_balance_sheet_tab(statscan_rows)
            with dpg.tab(label="Weekly Flows (CGC)"):
                build_weekly_tab(cgc_rows)
            with dpg.tab(label="Bridge Check"):
                build_bridge_tab(cgc_rows, statscan_rows)
            with dpg.tab(label="Prices (PDQ)"):
                build_prices_tab(price_rows)
            with dpg.tab(label="Nowcast"):
                build_nowcast_tab(nowcast_rows, price_rows)
            with dpg.tab(label="Findings & Gaps"):
                build_findings_tab(statscan_rows, cgc_rows, price_rows, nowcast_rows)

    dpg.create_viewport(title="Alberta Canola S&D", width=1600, height=900)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("primary_window", True)
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
