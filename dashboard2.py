"""
Alberta Canola S&D Dashboard (multi-year edition).

Reads from data/db/canola.sqlite and displays:
  - Tab 1: StatsCan triannual balance sheet (all years, with roll-forward check)
  - Tab 2: CGC weekly flows (filter by crop year)
  - Tab 3: Bridge check (filter by crop year)
  - Tab 4: Year-over-year overlay (all crop years on one chart)
  - Tab 5: Findings, gaps, coverage

Run:
    pip install dearpygui
    python3 dashboard.py
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import dearpygui.dearpygui as dpg

DB_PATH = Path(__file__).parent / "data" / "db" / "canola.sqlite"

COL_GOOD   = (90,  200, 120)
COL_WARN   = (240, 180, 60)
COL_BAD    = (220, 80,  80)
COL_DIM    = (160, 160, 160)
COL_HDR    = (110, 180, 240)
COL_INFO   = (180, 180, 200)

THR_TIGHT      = 5.0
THR_ACCEPTABLE = 12.0

# Module-level cache so we don't re-query inside callbacks
DATA = {"statscan": [], "cgc": []}


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
    cur = con.cursor(); cur.execute("PRAGMA table_info(cgc_weekly)")
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


# ---------------------------------------------------------------------------
# Computations
# ---------------------------------------------------------------------------

def check_identity(s):
    supply = (s["beginning_stocks_kt"] or 0) + (s["production_kt"] or 0)
    disp = sum((s[k] or 0) for k in
               ("deliveries_kt", "seed_requirements_kt", "ending_stocks_kt", "feed_waste_dockage_kt"))
    err = abs(supply - disp)
    return err < 1.0, err


def period_covered(snapshot_month, snapshot_date):
    d = datetime.fromisoformat(snapshot_date).date()
    end_label = d.strftime("%b %Y")
    start_year = d.year if snapshot_month == "December" else d.year - 1
    return f"Aug {start_year} - {end_label}"


def check_rollforward(statscan_rows):
    """For each year's July snapshot (full crop year), check that the FOLLOWING
    crop year's first snapshot (usually December) has matching beginning_stocks.
    Returns list of (prev_cy, next_cy, prev_ending, next_beginning, gap).
    """
    by_cy = {}
    for s in statscan_rows:
        by_cy.setdefault(s["crop_year"], []).append(s)

    checks = []
    cys = sorted(by_cy.keys())
    for i in range(len(cys) - 1):
        prev_cy, next_cy = cys[i], cys[i + 1]
        # Find the LAST snapshot of prev_cy (usually July)
        prev_snaps = sorted(by_cy[prev_cy], key=lambda s: s["snapshot_date"])
        last_prev = prev_snaps[-1]
        # Find the FIRST snapshot of next_cy (usually December)
        next_snaps = sorted(by_cy[next_cy], key=lambda s: s["snapshot_date"])
        first_next = next_snaps[0]

        ending = last_prev["ending_stocks_kt"]
        beginning = first_next["beginning_stocks_kt"]
        gap = (beginning or 0) - (ending or 0) if (ending is not None and beginning is not None) else None
        checks.append({
            "prev_cy": prev_cy,
            "prev_snap": last_prev["snapshot_month"],
            "prev_ending": ending,
            "next_cy": next_cy,
            "next_snap": first_next["snapshot_month"],
            "next_beginning": beginning,
            "gap": gap,
        })
    return checks


def bridge_compare(cgc_rows, statscan_rows):
    """No synthetic Aug 1 anchor. Only compare weeks with real StatsCan reference."""
    results = []
    for cgc in cgc_rows:
        wk_end = datetime.fromisoformat(cgc["week_ending"]).date()
        cy = cgc["crop_year"]
        anchors = sorted(
            (datetime.fromisoformat(s["snapshot_date"]).date(),
             s["deliveries_kt"] or 0.0, s["snapshot_month"])
            for s in statscan_rows if s["crop_year"] == cy
        )

        if not anchors:
            results.append(_bridge_result(cgc, None, None, None, "NO STATSCAN",
                                          "no anchors for this crop year"))
            continue

        first_anchor_date = anchors[0][0]
        last_anchor_date = anchors[-1][0]

        if wk_end < first_anchor_date:
            results.append(_bridge_result(cgc, None, None, None, "NO ANCHOR YET",
                                          f"first anchor: {first_anchor_date}"))
            continue

        exact = None
        for d, v, m in anchors:
            if abs((wk_end - d).days) <= 6:
                exact = (d, v, m); break
        if exact is not None:
            d, v, m = exact
            actual = cgc["total_deliveries_cytd_kt"]
            if actual is None:
                results.append(_bridge_result(cgc, v, None, None, "ANCHOR", f"{m} {d.year} but CGC NULL"))
            else:
                gap_kt = actual - v
                gap_pct = 100 * gap_kt / v if v > 0 else None
                results.append(_bridge_result(cgc, v, gap_kt, gap_pct, "ANCHOR", f"matches {m} {d.year}"))
            continue

        if wk_end <= last_anchor_date:
            for i in range(len(anchors) - 1):
                d0, v0, _ = anchors[i]
                d1, v1, _ = anchors[i + 1]
                if d0 <= wk_end <= d1:
                    frac = (wk_end - d0).days / max((d1 - d0).days, 1)
                    expected = v0 + frac * (v1 - v0)
                    actual = cgc["total_deliveries_cytd_kt"]
                    if actual is None:
                        results.append(_bridge_result(cgc, expected, None, None, "INTERIM", "CGC NULL"))
                    else:
                        gap_kt = actual - expected
                        gap_pct = 100 * gap_kt / expected if expected > 0 else None
                        results.append(_bridge_result(cgc, expected, gap_kt, gap_pct, "INTERIM", ""))
                    break
            continue

        expected = anchors[-1][1]
        actual = cgc["total_deliveries_cytd_kt"]
        if actual is None:
            results.append(_bridge_result(cgc, expected, None, None, "POST-ANCHOR", "CGC NULL"))
        else:
            gap_kt = actual - expected
            gap_pct = 100 * gap_kt / expected if expected > 0 else None
            results.append(_bridge_result(cgc, expected, gap_kt, gap_pct, "POST-ANCHOR", "past last anchor"))
    return results


def _bridge_result(cgc, expected, gap_kt, gap_pct, status, note):
    return {"cgc": cgc, "expected": expected, "gap_kt": gap_kt,
            "gap_pct": gap_pct, "status": status, "note": note}


def bridge_verdict(status, gap_pct):
    if status == "NO ANCHOR YET":
        return ("NO ANCHOR YET", COL_DIM)
    if status == "NO STATSCAN":
        return ("NO DATA", COL_DIM)
    if gap_pct is None:
        return ("NULL", COL_DIM)
    abs_pct = abs(gap_pct)
    if status == "ANCHOR":
        if abs_pct < THR_TIGHT:    return ("ANCHOR ✓", COL_GOOD)
        if abs_pct < THR_ACCEPTABLE: return ("ANCHOR ~7% (channels)", COL_WARN)
        return ("ANCHOR FAIL", COL_BAD)
    if abs_pct < THR_TIGHT:        return ("TIGHT", COL_GOOD)
    if abs_pct < THR_ACCEPTABLE:   return ("EXPECTED", COL_WARN)
    return ("INVESTIGATE", COL_BAD)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def fmt(v, nd=1):
    if v is None: return "-"
    if isinstance(v, (int, float)): return f"{v:,.{nd}f}"
    return str(v)


def coloured_text(text, color):
    dpg.add_text(text, color=color)


def all_crop_years_in_cgc():
    """Return sorted list of crop years that appear in cgc_weekly."""
    return sorted({r["crop_year"] for r in DATA["cgc"]})


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------

def build_balance_sheet_tab():
    statscan_rows = DATA["statscan"]

    dpg.add_text("Alberta canola farm supply and disposition", color=COL_HDR)
    dpg.add_text("Source: StatsCan Table 32-10-0015-01 (units: thousand tonnes)", color=COL_DIM)
    dpg.add_text(
        "Each StatsCan column is CUMULATIVE over the crop year.\n"
        "  March 2024  = deliveries Aug 2023 - Mar 2024  (crop year 2023/24)\n"
        "  July 2024   = full crop year 2023/24\n"
        "  Dec 2024    = first snapshot of crop year 2024/25",
        color=COL_INFO,
    )
    dpg.add_spacer(height=10)

    if not statscan_rows:
        coloured_text("No StatsCan rows loaded.", COL_BAD)
        return

    # Roll-forward check
    dpg.add_text("Roll-forward consistency: ending stocks of year N should equal beginning stocks of year N+1",
                 color=COL_HDR)
    checks = check_rollforward(statscan_rows)
    if not checks:
        dpg.add_text("  (need at least 2 crop years of StatsCan data for roll-forward check)",
                     color=COL_DIM)
    else:
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp):
            for h in ["Prev CY", "Prev Snap", "Prev Ending Stk",
                      "Next CY", "Next Snap", "Next Beginning Stk", "Gap (kt)", "Status"]:
                dpg.add_table_column(label=h)
            for c in checks:
                with dpg.table_row():
                    dpg.add_text(c["prev_cy"])
                    dpg.add_text(c["prev_snap"])
                    dpg.add_text(fmt(c["prev_ending"]))
                    dpg.add_text(c["next_cy"])
                    dpg.add_text(c["next_snap"])
                    dpg.add_text(fmt(c["next_beginning"]))
                    if c["gap"] is None:
                        coloured_text("?", COL_DIM)
                        coloured_text("missing data", COL_DIM)
                    elif abs(c["gap"]) < 1.0:
                        coloured_text(f"{c['gap']:+.1f}", COL_GOOD)
                        coloured_text("ROLLS CLEAN", COL_GOOD)
                    elif abs(c["gap"]) < 50.0:
                        coloured_text(f"{c['gap']:+.1f}", COL_WARN)
                        coloured_text("MINOR REVISION", COL_WARN)
                    else:
                        coloured_text(f"{c['gap']:+.1f}", COL_BAD)
                        coloured_text("BIG GAP", COL_BAD)
    dpg.add_spacer(height=12)
    dpg.add_text("(NOTE: StatsCan revises beginning stocks at the start of each new crop year, "
                 "so small gaps are normal. Big gaps suggest a vintage mismatch.)",
                 color=COL_DIM)
    dpg.add_spacer(height=16)

    # Full snapshot table
    dpg.add_text("All StatsCan snapshots", color=COL_HDR)
    with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                   borders_outerH=True, borders_outerV=True, resizable=True,
                   policy=dpg.mvTable_SizingStretchProp, scrollY=True, height=400):
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
                if ok: coloured_text(f"OK ({err:.1f})", COL_GOOD)
                else:  coloured_text(f"FAIL ({err:.1f})", COL_BAD)


def build_weekly_tab():
    cgc_rows = DATA["cgc"]
    dpg.add_text("CGC weekly flows — Alberta canola", color=COL_HDR)
    if not cgc_rows:
        coloured_text("No CGC rows loaded.", COL_BAD); return

    crop_years = ["ALL"] + all_crop_years_in_cgc()
    dpg.add_text(f"Total rows loaded: {len(cgc_rows)} across {len(crop_years)-1} crop year(s)")
    dpg.add_spacer(height=4)

    # Filter dropdown
    with dpg.group(horizontal=True):
        dpg.add_text("Filter by crop year:")
        dpg.add_combo(crop_years, default_value="ALL", width=120,
                      callback=_weekly_filter_callback, tag="weekly_filter")
    dpg.add_spacer(height=8)

    # Placeholder container that we rebuild on filter change
    with dpg.child_window(tag="weekly_content", border=False, height=-1):
        _build_weekly_content("ALL")


def _weekly_filter_callback(sender, app_data):
    dpg.delete_item("weekly_content", children_only=True)
    dpg.push_container_stack("weekly_content")
    _build_weekly_content(app_data)
    dpg.pop_container_stack()


def _build_weekly_content(crop_year_filter):
    rows = DATA["cgc"]
    if crop_year_filter != "ALL":
        rows = [r for r in rows if r["crop_year"] == crop_year_filter]

    n_combined = sum(1 for r in rows if r.get("notes") and "ombined" in (r["notes"] or ""))
    dpg.add_text(f"Showing {len(rows)} row(s)  ({n_combined} from combined-week files)")
    dpg.add_spacer(height=8)

    with dpg.collapsing_header(label="Weekly producer deliveries (per week)", default_open=False):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp, scrollY=True, height=300):
            for h in ["Week", "Ending", "Crop Yr", "Primary (wk)", "Process (wk)",
                      "Total (wk)", "Primary Ship (wk)", "Note"]:
                dpg.add_table_column(label=h)
            for c in rows:
                is_combined = c.get("notes") and "ombined" in (c["notes"] or "")
                with dpg.table_row():
                    dpg.add_text(str(c["week_number"]))
                    dpg.add_text(c["week_ending"])
                    dpg.add_text(c["crop_year"])
                    if is_combined and c["producer_deliveries_weekly_kt"] is None:
                        for _ in range(4): coloured_text("[combined]", COL_WARN)
                    else:
                        dpg.add_text(fmt(c["producer_deliveries_weekly_kt"]))
                        dpg.add_text(fmt(c["process_deliveries_weekly_kt"]))
                        dpg.add_text(fmt(c["total_deliveries_weekly_kt"]))
                        dpg.add_text(fmt(c["primary_shipments_weekly_kt"]))
                    note = c.get("notes") or ""
                    if "earlier half" in note: coloured_text("earlier-half", COL_WARN)
                    elif "later half" in note: coloured_text("later-half", COL_WARN)
                    else: dpg.add_text("")

    with dpg.collapsing_header(label="Crop-year-to-date cumulative", default_open=True):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp, scrollY=True, height=300):
            for h in ["Week", "Ending", "Crop Yr", "Primary CYTD", "Process CYTD",
                      "Total CYTD", "Primary Ship CYTD"]:
                dpg.add_table_column(label=h)
            for c in rows:
                with dpg.table_row():
                    dpg.add_text(str(c["week_number"]))
                    dpg.add_text(c["week_ending"])
                    dpg.add_text(c["crop_year"])
                    dpg.add_text(fmt(c["producer_deliveries_cytd_kt"]))
                    dpg.add_text(fmt(c["process_deliveries_cytd_kt"]))
                    dpg.add_text(fmt(c["total_deliveries_cytd_kt"]))
                    dpg.add_text(fmt(c["primary_shipments_cytd_kt"]))

    with dpg.collapsing_header(label="End-of-week visible commercial stocks", default_open=False):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp, scrollY=True, height=300):
            for h in ["Week", "Ending", "Primary Stk", "Process Stk", "Condo", "Total Visible"]:
                dpg.add_table_column(label=h)
            for c in rows:
                is_earlier = c.get("notes") and "earlier half" in (c["notes"] or "")
                with dpg.table_row():
                    dpg.add_text(str(c["week_number"]))
                    dpg.add_text(c["week_ending"])
                    if is_earlier:
                        for _ in range(4): coloured_text("[no snapshot]", COL_WARN)
                    else:
                        dpg.add_text(fmt(c["primary_elevator_stocks_kt"]))
                        dpg.add_text(fmt(c["process_elevator_stocks_kt"]))
                        dpg.add_text(fmt(c["condo_storage_kt"]))
                        dpg.add_text(fmt(c["total_visible_stocks_kt"]))

    # Single-year CYTD chart
    if crop_year_filter != "ALL" and len(rows) >= 2:
        dpg.add_spacer(height=12)
        dpg.add_text(f"CYTD deliveries — {crop_year_filter}", color=COL_HDR)
        x = [float(c["week_number"]) for c in rows]
        primary = [c["producer_deliveries_cytd_kt"] or 0 for c in rows]
        process = [c["process_deliveries_cytd_kt"] or 0 for c in rows]
        total   = [c["total_deliveries_cytd_kt"] or 0 for c in rows]
        with dpg.plot(height=300, width=-1):
            dpg.add_plot_legend()
            dpg.add_plot_axis(dpg.mvXAxis, label="Crop year week (1 = first week of Aug)")
            with dpg.plot_axis(dpg.mvYAxis, label="Cumulative deliveries (kt)"):
                dpg.add_line_series(x, primary, label="Primary CYTD")
                dpg.add_line_series(x, process, label="Process CYTD")
                dpg.add_line_series(x, total,   label="Total CYTD")


def build_bridge_tab():
    cgc_rows = DATA["cgc"]; statscan_rows = DATA["statscan"]
    dpg.add_text("Bridge: CGC weekly CYTD vs StatsCan triannual snapshots", color=COL_HDR)
    dpg.add_text(
        "Compares CGC cumulative deliveries to the equivalent StatsCan number.\n"
        "Weeks before the first StatsCan anchor are marked NO ANCHOR YET (S-curve\n"
        "deliveries make linear-from-zero interpolation misleading there).\n"
        f"Thresholds: TIGHT <{THR_TIGHT:.0f}%, EXPECTED <{THR_ACCEPTABLE:.0f}%, "
        f"INVESTIGATE >{THR_ACCEPTABLE:.0f}%",
        color=COL_INFO,
    )
    dpg.add_spacer(height=8)

    if not cgc_rows or not statscan_rows:
        coloured_text("Need both StatsCan and CGC data loaded.", COL_BAD); return

    crop_years = ["ALL"] + all_crop_years_in_cgc()
    with dpg.group(horizontal=True):
        dpg.add_text("Filter by crop year:")
        dpg.add_combo(crop_years, default_value="ALL", width=120,
                      callback=_bridge_filter_callback, tag="bridge_filter")
    dpg.add_spacer(height=8)

    with dpg.child_window(tag="bridge_content", border=False, height=-1):
        _build_bridge_content("ALL")


def _bridge_filter_callback(sender, app_data):
    dpg.delete_item("bridge_content", children_only=True)
    dpg.push_container_stack("bridge_content")
    _build_bridge_content(app_data)
    dpg.pop_container_stack()


def _build_bridge_content(crop_year_filter):
    cgc_rows = DATA["cgc"]; statscan_rows = DATA["statscan"]
    if crop_year_filter != "ALL":
        cgc_rows = [r for r in cgc_rows if r["crop_year"] == crop_year_filter]

    bridge = bridge_compare(cgc_rows, statscan_rows)
    comparable = [b for b in bridge if b["status"] in ("ANCHOR", "INTERIM", "POST-ANCHOR")
                  and b["gap_pct"] is not None]
    if comparable:
        gaps = [b["gap_pct"] for b in comparable]
        avg = sum(gaps) / len(gaps)
        dpg.add_text(f"Average gap across {len(gaps)} comparable weeks: {avg:+.1f}%", color=COL_HDR)
        dpg.add_text("  (consistent +7% to +8% = the missing-channel signature)", color=COL_DIM)
        dpg.add_spacer(height=8)

    with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                   borders_outerH=True, borders_outerV=True, resizable=True,
                   policy=dpg.mvTable_SizingStretchProp, scrollY=True, height=-1):
        for h in ["Week", "Ending", "Crop Yr", "CGC Total CYTD",
                  "StatsCan Expected", "Gap (kt)", "Gap (%)", "Verdict", "Note"]:
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
                    dpg.add_text("-"); dpg.add_text("-")
                else:
                    sign = "+" if b["gap_kt"] >= 0 else ""
                    dpg.add_text(f"{sign}{b['gap_kt']:,.1f}")
                    dpg.add_text(f"{sign}{b['gap_pct']:.1f}%")
                label, color = bridge_verdict(b["status"], b["gap_pct"])
                coloured_text(label, color)
                dpg.add_text((b["note"] or "")[:60])


def build_overlay_tab():
    """Year-over-year overlay — every crop year on a single chart, aligned by
    crop-year-week. This is THE chart for spotting unusual seasons."""
    cgc_rows = DATA["cgc"]
    dpg.add_text("Year-over-year overlay", color=COL_HDR)
    dpg.add_text(
        "Each line is one crop year's cumulative deliveries, aligned by week-of-crop-year\n"
        "(x = 1 means first week of August). Useful for spotting unusual seasons or pace.",
        color=COL_INFO,
    )
    dpg.add_spacer(height=8)

    if not cgc_rows:
        coloured_text("No CGC rows loaded.", COL_BAD); return

    by_cy = {}
    for r in cgc_rows:
        by_cy.setdefault(r["crop_year"], []).append(r)

    # Total CYTD overlay (primary + process)
    dpg.add_text("Total CYTD deliveries (Primary + Process)", color=COL_HDR)
    with dpg.plot(height=400, width=-1):
        dpg.add_plot_legend()
        dpg.add_plot_axis(dpg.mvXAxis, label="Crop year week (1 = first week of Aug)")
        with dpg.plot_axis(dpg.mvYAxis, label="Cumulative deliveries (kt)"):
            for cy in sorted(by_cy.keys()):
                rows = sorted(by_cy[cy], key=lambda r: r["week_number"])
                x = [float(r["week_number"]) for r in rows]
                y = [r["total_deliveries_cytd_kt"] or 0 for r in rows]
                dpg.add_line_series(x, y, label=cy)
    dpg.add_spacer(height=20)

    # Visible stocks overlay
    dpg.add_text("Visible commercial stocks (Primary + Process + Condo)", color=COL_HDR)
    with dpg.plot(height=400, width=-1):
        dpg.add_plot_legend()
        dpg.add_plot_axis(dpg.mvXAxis, label="Crop year week (1 = first week of Aug)")
        with dpg.plot_axis(dpg.mvYAxis, label="Visible stocks (kt)"):
            for cy in sorted(by_cy.keys()):
                rows = sorted(by_cy[cy], key=lambda r: r["week_number"])
                # Skip points where stocks are NULL (combined-week earlier-half rows)
                x, y = [], []
                for r in rows:
                    if r["total_visible_stocks_kt"] is not None:
                        x.append(float(r["week_number"]))
                        y.append(r["total_visible_stocks_kt"])
                if x:
                    dpg.add_line_series(x, y, label=cy)
    dpg.add_spacer(height=20)

    # Per-week flow comparison
    dpg.add_text("Weekly producer deliveries (Primary, weekly flow)", color=COL_HDR)
    with dpg.plot(height=400, width=-1):
        dpg.add_plot_legend()
        dpg.add_plot_axis(dpg.mvXAxis, label="Crop year week")
        with dpg.plot_axis(dpg.mvYAxis, label="Weekly Primary deliveries (kt)"):
            for cy in sorted(by_cy.keys()):
                rows = sorted(by_cy[cy], key=lambda r: r["week_number"])
                x, y = [], []
                for r in rows:
                    if r["producer_deliveries_weekly_kt"] is not None:
                        x.append(float(r["week_number"]))
                        y.append(r["producer_deliveries_weekly_kt"])
                if x:
                    dpg.add_line_series(x, y, label=cy)


def build_findings_tab():
    statscan_rows = DATA["statscan"]; cgc_rows = DATA["cgc"]
    dpg.add_text("Project status & findings", color=COL_HDR)
    dpg.add_spacer(height=8)

    dpg.add_text("Data loaded", color=COL_HDR)
    cgc_cys = sorted({r['crop_year'] for r in cgc_rows})
    sc_cys = sorted({r['crop_year'] for r in statscan_rows})
    dpg.add_text(f"  StatsCan: {len(statscan_rows)} snapshot(s) across {len(sc_cys)} crop year(s): {sc_cys}")
    dpg.add_text(f"  CGC:      {len(cgc_rows)} weekly row(s) across {len(cgc_cys)} crop year(s): {cgc_cys}")
    if cgc_rows:
        n_combined = sum(1 for c in cgc_rows if c.get("notes") and "ombined" in (c["notes"] or ""))
        if n_combined:
            dpg.add_text(f"  Combined-week rows: {n_combined}")
    dpg.add_spacer(height=12)

    # Coverage matrix — which years have which months/weeks
    dpg.add_text("Coverage matrix", color=COL_HDR)
    if statscan_rows:
        dpg.add_text("StatsCan snapshots present:", color=COL_DIM)
        sc_by_cy = {}
        for s in statscan_rows:
            sc_by_cy.setdefault(s["crop_year"], set()).add(s["snapshot_month"])
        for cy in sc_cys:
            present = sc_by_cy.get(cy, set())
            wanted = ["December", "March", "July"]
            cells = [m if m in present else "-" for m in wanted]
            dpg.add_text(f"    {cy}:  Dec={cells[0][:3]:<3}  Mar={cells[1][:3]:<3}  Jul={cells[2][:3]:<3}")
    dpg.add_spacer(height=8)
    if cgc_rows:
        dpg.add_text("CGC weekly coverage (week count / 52):", color=COL_DIM)
        cgc_by_cy = {}
        for r in cgc_rows:
            cgc_by_cy.setdefault(r["crop_year"], []).append(r["week_number"])
        for cy in cgc_cys:
            weeks = sorted(set(cgc_by_cy[cy]))
            full = set(range(1, 53))
            missing = sorted(full - set(weeks))
            tag = "" if not missing else f"  missing: {missing[:5]}{'...' if len(missing) > 5 else ''}"
            dpg.add_text(f"    {cy}:  {len(weeks)}/52{tag}")
    dpg.add_spacer(height=12)

    # Identity validation
    dpg.add_text("Balance sheet identity validation", color=COL_HDR)
    n_ok = sum(1 for s in statscan_rows if check_identity(s)[0])
    n_bad = len(statscan_rows) - n_ok
    if statscan_rows and n_bad == 0:
        coloured_text(f"  All {n_ok}/{len(statscan_rows)} StatsCan rows balance.", COL_GOOD)
    elif statscan_rows:
        coloured_text(f"  {n_bad}/{len(statscan_rows)} rows fail!", COL_BAD)
    dpg.add_spacer(height=12)

    # Bridge health
    dpg.add_text("Bridge health (CGC vs StatsCan, all years)", color=COL_HDR)
    bridge = bridge_compare(cgc_rows, statscan_rows)
    comparable = [b for b in bridge if b["status"] in ("ANCHOR", "INTERIM", "POST-ANCHOR")
                  and b["gap_pct"] is not None]
    no_anchor = [b for b in bridge if b["status"] == "NO ANCHOR YET"]
    if comparable:
        gaps = [b["gap_pct"] for b in comparable]
        avg = sum(gaps) / len(gaps)
        dpg.add_text(f"  {len(comparable)} comparable weeks  (avg gap {avg:+.1f}%)")
    if no_anchor:
        dpg.add_text(f"  {len(no_anchor)} early-year weeks have no anchor yet (this is expected)")
    dpg.add_spacer(height=12)

    dpg.add_text("Known structural gaps", color=COL_HDR)
    dpg.add_text(
        "  1. ~7% missing channels (producer cars, feed mill direct, 'other deliveries' to Process)\n"
        "  2. Combined-week files split mathematically; CYTD exact, weekly flows + earlier-stocks NULL\n"
        "  3. Early-crop-year weeks (Aug-Dec) can't be bridge-checked before first StatsCan anchor\n"
        "  4. PDQ pricing not yet integrated",
        color=COL_DIM,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        return

    DATA["statscan"] = fetch_statscan()
    DATA["cgc"] = fetch_cgc()

    print(f"Loaded {len(DATA['statscan'])} StatsCan rows, {len(DATA['cgc'])} CGC rows")

    dpg.create_context()

    with dpg.window(label="Alberta Canola S&D", tag="primary_window"):
        dpg.add_text("Alberta Canola Supply & Demand Dashboard (multi-year)", color=COL_HDR)
        dpg.add_text(f"Database: {DB_PATH}", color=COL_DIM)
        dpg.add_separator()

        with dpg.tab_bar():
            with dpg.tab(label="Balance Sheet (StatsCan)"):
                build_balance_sheet_tab()
            with dpg.tab(label="Weekly Flows (CGC)"):
                build_weekly_tab()
            with dpg.tab(label="Bridge Check"):
                build_bridge_tab()
            with dpg.tab(label="Year-over-Year Overlay"):
                build_overlay_tab()
            with dpg.tab(label="Findings & Coverage"):
                build_findings_tab()

    dpg.create_viewport(title="Alberta Canola S&D", width=1700, height=950)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("primary_window", True)
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
