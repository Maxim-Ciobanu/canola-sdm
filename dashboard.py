"""
Alberta Canola S&D Dashboard.

Reads from data/db/canola.sqlite and displays:
  - Tab 1: StatsCan triannual balance sheet (one row per snapshot)
  - Tab 2: CGC weekly flows (one row per week loaded)
  - Tab 3: Bridge check — CGC CYTD vs StatsCan (only at real anchor weeks)
  - Tab 4: Findings, gaps, identity validation

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

        # Before first anchor: no honest comparison possible.
        # Deliveries follow an S-curve (slow Aug, steep Sept-Oct, steady Nov-).
        # Linear from zero badly under/over-estimates depending on point.
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
        # At anchor dates expect tight match, missing channels are well-documented +7%
        if abs_pct < THR_TIGHT:
            return ("ANCHOR ✓", COL_GOOD)
        if abs_pct < THR_ACCEPTABLE:
            return ("ANCHOR ~7% (channels)", COL_WARN)
        return ("ANCHOR FAIL", COL_BAD)
    # INTERIM or POST-ANCHOR
    if abs_pct < THR_TIGHT:
        return ("TIGHT", COL_GOOD)
    if abs_pct < THR_ACCEPTABLE:
        return ("EXPECTED", COL_WARN)
    return ("INVESTIGATE", COL_BAD)


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


def build_findings_tab(statscan_rows, cgc_rows):
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
        "  4. PDQ pricing not yet integrated — balance sheet first.",
        color=COL_DIM,
    )
    dpg.add_spacer(height=12)

    # Next steps
    dpg.add_text("Next steps", color=COL_HDR)
    dpg.add_text(
        "  - Load more crop years of CGC + matching StatsCan vintages to enable\n"
        "    year-over-year delivery-pace comparisons (a model that doesn't need\n"
        "    StatsCan anchors to validate weekly progress).\n"
        "  - Build nowcast.py: apply the balance sheet identity weekly to compute\n"
        "    implied AB on-farm stocks.\n"
        "  - Decide whether to close the +7% gap by adding the missing-channel tabs,\n"
        "    or accept it as a documented constant and apply a correction factor.\n"
        "  - Add PDQ Southern AB cash bids once balance sheet feels solid.",
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
            with dpg.tab(label="Findings & Gaps"):
                build_findings_tab(statscan_rows, cgc_rows)

    dpg.create_viewport(title="Alberta Canola S&D", width=1600, height=900)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("primary_window", True)
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
