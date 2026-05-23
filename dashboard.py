"""
Alberta Canola S&D Dashboard.

Reads from data/db/canola.sqlite and displays:
  - Tab 1: StatsCan triannual balance sheet (one row per snapshot)
  - Tab 2: CGC weekly flows (one row per week loaded)
  - Tab 3: Bridge check — CGC CYTD vs StatsCan, linear-interpolated
  - Tab 4: Findings, gaps, identity validation

Run:
    pip install dearpygui
    python3 dashboard.py
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

import dearpygui.dearpygui as dpg

DB_PATH = Path(__file__).parent / "data" / "db" / "canola.sqlite"

# Colour constants (RGB tuples — dearpygui uses 0-255)
COL_GOOD   = (90,  200, 120)   # green for "identity holds"
COL_WARN   = (240, 180, 60)    # amber for small gaps
COL_BAD    = (220, 80,  80)    # red for big gaps or missing
COL_DIM    = (160, 160, 160)   # grey for labels
COL_HDR    = (110, 180, 240)   # blue for headers


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def fetch_statscan():
    """Return list of dicts, one per snapshot row in statscan_sd."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT crop_year, snapshot_month, snapshot_date,
               total_supplies_kt, beginning_stocks_kt, production_kt,
               total_disposition_kt, deliveries_kt, seed_requirements_kt,
               ending_stocks_kt, feed_waste_dockage_kt, report_date
        FROM statscan_sd
        WHERE geography='Alberta' AND crop='Canola'
        ORDER BY snapshot_date
    """)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def fetch_cgc():
    """Return list of dicts, one per week loaded in cgc_weekly."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT week_number, week_ending, crop_year,
               producer_deliveries_weekly_kt, producer_deliveries_cytd_kt,
               primary_shipments_weekly_kt, primary_shipments_cytd_kt,
               process_deliveries_weekly_kt, process_deliveries_cytd_kt,
               total_deliveries_weekly_kt, total_deliveries_cytd_kt,
               primary_elevator_stocks_kt, process_elevator_stocks_kt,
               condo_storage_kt, total_visible_stocks_kt,
               source_file
        FROM cgc_weekly
        WHERE geography='Alberta' AND crop='Canola'
        ORDER BY week_ending
    """)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


# ---------------------------------------------------------------------------
# Computations
# ---------------------------------------------------------------------------

def check_identity(s):
    """Validate balance sheet identity for one StatsCan row. Returns (ok, err_kt)."""
    supply_side = (s["beginning_stocks_kt"] or 0) + (s["production_kt"] or 0)
    disp_side   = (
        (s["deliveries_kt"] or 0)
        + (s["seed_requirements_kt"] or 0)
        + (s["ending_stocks_kt"] or 0)
        + (s["feed_waste_dockage_kt"] or 0)
    )
    err = abs(supply_side - disp_side)
    return err < 1.0, err


def bridge_compare(cgc_rows, statscan_rows):
    """For each CGC week, find StatsCan snapshots in the same crop year and
    linear-interpolate the expected CYTD deliveries at the CGC week_ending date."""
    results = []
    for cgc in cgc_rows:
        wk_end = datetime.fromisoformat(cgc["week_ending"]).date()
        cy = cgc["crop_year"]
        snaps = [s for s in statscan_rows if s["crop_year"] == cy]
        if len(snaps) < 1:
            results.append({"cgc": cgc, "expected": None, "gap_kt": None, "gap_pct": None,
                            "note": f"no StatsCan snapshots for {cy}"})
            continue

        # Add a synthetic "Aug 1" anchor at zero deliveries
        cy_start_year = int(cy.split("/")[0])
        anchors = [(date(cy_start_year, 8, 1), 0.0)] + [
            (datetime.fromisoformat(s["snapshot_date"]).date(), s["deliveries_kt"] or 0.0)
            for s in snaps
        ]
        anchors.sort()

        # Find bracketing pair
        expected = None
        for i in range(len(anchors) - 1):
            d0, v0 = anchors[i]
            d1, v1 = anchors[i + 1]
            if d0 <= wk_end <= d1:
                frac = (wk_end - d0).days / max((d1 - d0).days, 1)
                expected = v0 + frac * (v1 - v0)
                break
        if expected is None and wk_end > anchors[-1][0]:
            expected = anchors[-1][1]   # past last snapshot, hold flat

        actual = cgc["total_deliveries_cytd_kt"] or 0.0
        gap_kt = actual - expected if expected is not None else None
        gap_pct = (100 * gap_kt / expected) if (expected and expected > 0) else None
        results.append({"cgc": cgc, "expected": expected, "gap_kt": gap_kt, "gap_pct": gap_pct,
                        "note": ""})
    return results


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def fmt(v, nd=1):
    """Format a number to nd decimals, or '-' if None."""
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
    """Table of StatsCan triannual snapshots."""
    dpg.add_text("Alberta canola farm supply and disposition", color=COL_HDR)
    dpg.add_text("Source: StatsCan Table 32-10-0015-01 (units: thousand tonnes)", color=COL_DIM)
    dpg.add_spacer(height=8)

    if not statscan_rows:
        coloured_text("No StatsCan rows in the database yet. Run ingest/load_statscan.py.", COL_BAD)
        return

    with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                   borders_outerH=True, borders_outerV=True, resizable=True,
                   policy=dpg.mvTable_SizingStretchProp):
        for h in ["Crop Yr", "Snap", "Date", "Total Supply", "Beg Stocks", "Production",
                  "Total Disp", "Deliveries", "Seed", "Ending Stocks", "Feed/Waste", "Identity"]:
            dpg.add_table_column(label=h)

        for s in statscan_rows:
            ok, err = check_identity(s)
            with dpg.table_row():
                dpg.add_text(s["crop_year"])
                dpg.add_text(s["snapshot_month"])
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
    """Table of CGC weekly flows."""
    dpg.add_text("CGC weekly flows — Alberta canola", color=COL_HDR)
    dpg.add_text("Source: CGC GSW Primary tab + Process tab (units: thousand tonnes)",
                 color=COL_DIM)
    dpg.add_spacer(height=8)

    if not cgc_rows:
        coloured_text("No CGC rows yet. Run ingest/load_cgc.py.", COL_BAD)
        return

    dpg.add_text(f"Weeks loaded: {len(cgc_rows)}")
    dpg.add_spacer(height=4)

    # Weekly flow table
    with dpg.collapsing_header(label="Weekly producer deliveries (per week)", default_open=True):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp):
            for h in ["Week", "Ending", "Crop Yr", "Primary (wk)", "Process (wk)",
                      "Total (wk)", "Primary Ship (wk)", "Source File"]:
                dpg.add_table_column(label=h)
            for c in cgc_rows:
                with dpg.table_row():
                    dpg.add_text(str(c["week_number"]))
                    dpg.add_text(c["week_ending"])
                    dpg.add_text(c["crop_year"])
                    dpg.add_text(fmt(c["producer_deliveries_weekly_kt"]))
                    dpg.add_text(fmt(c["process_deliveries_weekly_kt"]))
                    dpg.add_text(fmt(c["total_deliveries_weekly_kt"]))
                    dpg.add_text(fmt(c["primary_shipments_weekly_kt"]))
                    dpg.add_text(c["source_file"])

    # CYTD cumulative
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
                with dpg.table_row():
                    dpg.add_text(str(c["week_number"]))
                    dpg.add_text(c["week_ending"])
                    dpg.add_text(fmt(c["primary_elevator_stocks_kt"]))
                    dpg.add_text(fmt(c["process_elevator_stocks_kt"]))
                    dpg.add_text(fmt(c["condo_storage_kt"]))
                    dpg.add_text(fmt(c["total_visible_stocks_kt"]))

    # Plot of deliveries over weeks loaded
    if len(cgc_rows) >= 1:
        dpg.add_spacer(height=12)
        dpg.add_text("CYTD deliveries by week loaded", color=COL_HDR)
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
    """The big-deal sanity check: does CGC CYTD reconcile to StatsCan?"""
    dpg.add_text("Bridge: CGC weekly CYTD vs StatsCan triannual snapshots", color=COL_HDR)
    dpg.add_text(
        "For each CGC week, linear-interpolate the StatsCan deliveries to the week-ending\n"
        "date. The two should match. Persistent gaps indicate missing channels (producer\n"
        "cars, feed mills, 'other deliveries' to process elevators).",
        color=COL_DIM,
    )
    dpg.add_spacer(height=8)

    if not cgc_rows or not statscan_rows:
        coloured_text("Need both StatsCan and CGC data loaded.", COL_BAD)
        return

    bridge = bridge_compare(cgc_rows, statscan_rows)

    with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                   borders_outerH=True, borders_outerV=True, resizable=True,
                   policy=dpg.mvTable_SizingStretchProp):
        for h in ["Week", "Ending", "Crop Yr",
                  "CGC Total CYTD", "StatsCan Expected", "Gap (kt)", "Gap (%)", "Verdict"]:
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
                    coloured_text(b["note"] or "n/a", COL_WARN)
                else:
                    sign = "+" if b["gap_kt"] >= 0 else ""
                    dpg.add_text(f"{sign}{b['gap_kt']:,.1f}")
                    dpg.add_text(f"{sign}{b['gap_pct']:.1f}%")
                    if abs(b["gap_pct"]) < 3:
                        coloured_text("TIGHT", COL_GOOD)
                    elif abs(b["gap_pct"]) < 10:
                        coloured_text("ACCEPTABLE", COL_WARN)
                    else:
                        coloured_text("INVESTIGATE", COL_BAD)

    dpg.add_spacer(height=12)
    dpg.add_text("Interpretation", color=COL_HDR)
    dpg.add_text(
        "• TIGHT (<3%):     bridge holds, nowcast trusted.\n"
        "• ACCEPTABLE (<10%): expected — small channels not yet loaded.\n"
        "• INVESTIGATE:     possible parsing error, wrong crop year, or missing channel.",
        color=COL_DIM,
    )


def build_findings_tab(statscan_rows, cgc_rows):
    """Free-text panel summarising what we know, what's missing, what's next."""
    dpg.add_text("Project status & findings", color=COL_HDR)
    dpg.add_spacer(height=8)

    # What's loaded
    dpg.add_text("Data loaded", color=COL_HDR)
    dpg.add_text(f"• StatsCan snapshots: {len(statscan_rows)} rows "
                 f"({len({r['crop_year'] for r in statscan_rows})} crop years)")
    dpg.add_text(f"• CGC weekly rows:   {len(cgc_rows)} rows "
                 f"({len({r['crop_year'] for r in cgc_rows})} crop years)")
    if cgc_rows:
        weeks = sorted(c["week_number"] for c in cgc_rows)
        dpg.add_text(f"• Weeks loaded:      {weeks}")
    dpg.add_spacer(height=12)

    # Identity validation summary
    dpg.add_text("Balance sheet identity validation", color=COL_HDR)
    n_ok  = sum(1 for s in statscan_rows if check_identity(s)[0])
    n_bad = len(statscan_rows) - n_ok
    if n_bad == 0 and statscan_rows:
        coloured_text(f"All {n_ok}/{len(statscan_rows)} StatsCan rows balance.", COL_GOOD)
    else:
        coloured_text(f"{n_bad}/{len(statscan_rows)} rows fail the identity!", COL_BAD)
    dpg.add_spacer(height=12)

    # Known gaps
    dpg.add_text("Known gaps", color=COL_HDR)
    dpg.add_text(
        "1. Producer cars not yet loaded (small — ~9 kt nationally CYTD).\n"
        "2. Feed mill direct deliveries not yet loaded (separate tab, small).\n"
        "3. 'Other Deliveries' column from Process tab not loaded (~700 kt nat'l).\n"
        "4. Process elevator data is national in some sub-tables — AB-only available\n"
        "   only in the deeper tables. We use those.\n"
        "5. Pricing (PDQ) not yet integrated — balance sheet first.\n"
        "6. No on-farm stocks signal yet — needs more loaded weeks to compute.",
        color=COL_DIM,
    )
    dpg.add_spacer(height=12)

    # Next steps
    dpg.add_text("Next steps", color=COL_HDR)
    dpg.add_text(
        "• Load more CGC weekly files (one at a time, change FILE_PATH in load_cgc.py).\n"
        "• Build nowcast.py: apply the identity weekly to compute implied on-farm stocks.\n"
        "• Add PDQ Southern AB cash bids once balance sheet feels solid.\n"
        "• Add a delivery-pace-vs-prior-year metric once you have a second crop year loaded.",
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

    dpg.create_viewport(title="Alberta Canola S&D", width=1500, height=900)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("primary_window", True)
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
