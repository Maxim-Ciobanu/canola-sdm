"""
Alberta Canola — Actionable Signals Dashboard

Replaces information-dense tables with concrete actionable signals.

Five tabs:
  1. NOW                — current market snapshot, overall recommendation
  2. Signal Detail      — each signal's calculation explained
  3. Historical Patterns — cross-year overlays with current year highlighted
  4. Data Quality       — coverage, anchor health, freshness
  5. Raw Data           — flat tables for when you need receipts

Run:
    pip install dearpygui
    python3 dashboard.py
"""

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import dearpygui.dearpygui as dpg

DB_PATH = Path(__file__).parent / "data" / "db" / "canola.sqlite"

# Colours
COL_BULL    = (90,  200, 120)
COL_BEAR    = (220, 80,  80)
COL_NEUTRAL = (180, 180, 180)
COL_WARN    = (240, 180, 60)
COL_HDR     = (110, 180, 240)
COL_DIM     = (150, 150, 150)
COL_INFO    = (200, 200, 230)
COL_BIG     = (255, 255, 255)

DATA = {"statscan": [], "cgc": [], "nowcast": [], "prices": []}


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_all():
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    cur = con.cursor()
    DATA["statscan"] = [dict(r) for r in cur.execute("""
        SELECT * FROM statscan_sd WHERE geography='Alberta' AND crop='Canola'
        ORDER BY snapshot_date""").fetchall()]
    DATA["cgc"] = [dict(r) for r in cur.execute("""
        SELECT * FROM cgc_weekly WHERE geography='Alberta' AND crop='Canola'
        ORDER BY week_ending""").fetchall()]
    DATA["nowcast"] = [dict(r) for r in cur.execute("""
        SELECT * FROM nowcast WHERE geography='Alberta' AND crop='Canola'
        ORDER BY week_ending""").fetchall()]
    DATA["prices"] = [dict(r) for r in cur.execute("""
        SELECT * FROM prices WHERE zone='Southern AB' AND basis_per_bu IS NOT NULL
        ORDER BY observation_date""").fetchall()]
    con.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_week_number_map():
    return {r["week_ending"]: r["week_number"] for r in DATA["cgc"]}


def percentile_rank(value, history):
    if value is None or not history: return None
    cleaned = [v for v in history if v is not None]
    if not cleaned: return None
    below = sum(1 for v in cleaned if v < value)
    return 100.0 * below / len(cleaned)


def cross_year_at_week(rows, week_number, field, week_map=None):
    """Return {crop_year: value} at the given week_number for that field."""
    out = {}
    for r in rows:
        wn = r.get("week_number")
        if wn is None and week_map:
            wn = week_map.get(r["week_ending"])
        if wn == week_number and r.get(field) is not None:
            out[r["crop_year"]] = r[field]
    return out


def latest_cgc_row():    return DATA["cgc"][-1] if DATA["cgc"] else None
def latest_nowcast_row(): return DATA["nowcast"][-1] if DATA["nowcast"] else None
def latest_basis_obs():   return DATA["prices"][-1] if DATA["prices"] else None


def weekly_avg_basis_by_week_ending():
    """Map week_ending -> avg basis in the 7-day window ending that date."""
    week_endings = sorted({r["week_ending"] for r in DATA["cgc"]})
    bucket = defaultdict(list)
    for p in DATA["prices"]:
        obs = p["observation_date"]
        for we in week_endings:
            if obs <= we:
                bucket[we].append(p["basis_per_bu"]); break
    return {we: sum(vs)/len(vs) for we, vs in bucket.items() if vs}


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

def compute_signals():
    cgc = latest_cgc_row()
    nc = latest_nowcast_row()
    if not cgc or not nc:
        return {"error": "no data"}

    current_week = cgc["week_number"]
    current_cy = cgc["crop_year"]
    week_map = get_week_number_map()

    signals = {
        "as_of_week": current_week,
        "as_of_date": cgc["week_ending"],
        "current_crop_year": current_cy,
        "signals": [],
    }

    # 1. Implied on-farm percentile
    h = cross_year_at_week(DATA["nowcast"], current_week, "implied_on_farm_stocks_kt", week_map)
    curr = h.get(current_cy)
    no_curr = {cy: v for cy, v in h.items() if cy != current_cy}
    signals["signals"].append(_sig_on_farm(curr, percentile_rank(curr, no_curr.values()), no_curr))

    # 2. Delivery pace
    h = cross_year_at_week(DATA["cgc"], current_week, "producer_deliveries_cytd_kt")
    curr = h.get(current_cy)
    no_curr = {cy: v for cy, v in h.items() if cy != current_cy}
    signals["signals"].append(_sig_pace(curr, percentile_rank(curr, no_curr.values()), no_curr))

    # 3. Visible commercial stocks
    h = cross_year_at_week(DATA["cgc"], current_week, "total_visible_stocks_kt")
    curr = h.get(current_cy)
    no_curr = {cy: v for cy, v in h.items() if cy != current_cy}
    signals["signals"].append(_sig_visible(curr, percentile_rank(curr, no_curr.values()), no_curr))

    # 4. Basis level
    basis_weekly = weekly_avg_basis_by_week_ending()
    by_cy_for_wk = {}
    for we, b in basis_weekly.items():
        if week_map.get(we) == current_week:
            cy_of_we = next((r["crop_year"] for r in DATA["cgc"] if r["week_ending"] == we), None)
            if cy_of_we: by_cy_for_wk[cy_of_we] = b
    curr = by_cy_for_wk.get(current_cy)
    no_curr = {cy: v for cy, v in by_cy_for_wk.items() if cy != current_cy}
    signals["signals"].append(_sig_basis(curr, percentile_rank(curr, no_curr.values()),
                                          no_curr, latest_basis_obs()))

    # 5. Basis momentum
    signals["signals"].append(_sig_basis_momentum())

    # 6. Pipeline
    signals["signals"].append(_sig_pipeline())

    bull = sum(1 for s in signals["signals"] if s["direction"] == "BULLISH")
    bear = sum(1 for s in signals["signals"] if s["direction"] == "BEARISH")
    if bull >= 3 and bull > bear:
        signals["overall"] = ("HOLD", COL_BULL,
                              f"{bull} bullish vs {bear} bearish — supply tightness favours holding")
    elif bear >= 3 and bear > bull:
        signals["overall"] = ("SELL", COL_BEAR,
                              f"{bear} bearish vs {bull} bullish — looseness suggests reducing exposure")
    else:
        signals["overall"] = ("WATCH", COL_WARN,
                              f"{bull} bullish vs {bear} bearish — mixed; no strong signal")
    return signals


def _sig_on_farm(curr, pct, history):
    if curr is None or pct is None:
        return _na("Implied on-farm stocks", curr, "Not enough same-week history.")
    if pct <= 25:
        return _bull("Implied on-farm stocks", "TIGHT", curr, pct,
                     f"AB on-farm canola at {curr:,.0f} kt is in the bottom 25% "
                     f"({pct:.0f}th pctile) for this week across 10 years of history. "
                     f"Tight supply favours stronger basis.")
    if pct >= 75:
        return _bear("Implied on-farm stocks", "LOOSE", curr, pct,
                     f"AB on-farm canola at {curr:,.0f} kt is in the top 25% "
                     f"({pct:.0f}th pctile). Loose supply weakens basis.")
    return _neutral("Implied on-farm stocks", "NORMAL", curr, pct,
                    f"AB on-farm canola at {curr:,.0f} kt is mid-range ({pct:.0f}th pctile).")


def _sig_pace(curr, pct, history):
    if curr is None or pct is None:
        return _na("Delivery pace (CYTD)", curr, "Not enough same-week history.")
    if pct <= 25:
        return _bull("Delivery pace (CYTD)", "SLOW", curr, pct,
                     f"Primary CYTD deliveries at {curr:,.0f} kt are in the bottom 25% "
                     f"({pct:.0f}th pctile) for this week. Farmers are holding back. "
                     f"Elevators have to bid harder, which firms basis.")
    if pct >= 75:
        return _bear("Delivery pace (CYTD)", "FAST", curr, pct,
                     f"Primary CYTD deliveries at {curr:,.0f} kt are in the top 25% "
                     f"({pct:.0f}th pctile). Heavy delivery flow weakens basis.")
    return _neutral("Delivery pace (CYTD)", "NORMAL", curr, pct,
                    f"CYTD deliveries at {curr:,.0f} kt are mid-range ({pct:.0f}th pctile).")


def _sig_visible(curr, pct, history):
    if curr is None or pct is None:
        return _na("Visible commercial stocks", curr, "Not enough history.")
    if pct <= 25:
        return _bull("Visible commercial stocks", "EMPTY", curr, pct,
                     f"Visible elevator stocks at {curr:,.0f} kt are in the bottom 25% "
                     f"({pct:.0f}th pctile). Empty elevators bid up for grain.")
    if pct >= 75:
        return _bear("Visible commercial stocks", "FULL", curr, pct,
                     f"Visible elevator stocks at {curr:,.0f} kt are in the top 25% "
                     f"({pct:.0f}th pctile). Stuffed pipeline; less reason to bid up.")
    return _neutral("Visible commercial stocks", "NORMAL", curr, pct,
                    f"Visible stocks at {curr:,.0f} kt are mid-range ({pct:.0f}th pctile).")


def _sig_basis(curr_avg, pct, history, latest_obs):
    latest_b = latest_obs["basis_per_bu"] if latest_obs else None
    latest_date = latest_obs["observation_date"] if latest_obs else "n/a"
    latest_str = f"${latest_b:+.2f}/bu" if latest_b is not None else "n/a"
    if curr_avg is None or pct is None:
        return _na("Basis level (Southern AB)", latest_b,
                   f"Latest observed basis: {latest_str} on {latest_date}. "
                   f"Not enough same-week history to score.")
    if pct <= 25:
        return _bull("Basis level (Southern AB)", "WEAK", latest_b, pct,
                     f"This week's avg basis ${curr_avg:+.2f}/bu is in the bottom 25% "
                     f"({pct:.0f}th pctile) for this week. Latest: {latest_str}. "
                     f"Historically weak basis tends to revert upward.")
    if pct >= 75:
        return _bear("Basis level (Southern AB)", "STRONG", latest_b, pct,
                     f"This week's avg basis ${curr_avg:+.2f}/bu is in the top 25% "
                     f"({pct:.0f}th pctile). Latest: {latest_str}. "
                     f"Historically strong basis can revert downward — selling opportunity.")
    return _neutral("Basis level (Southern AB)", "NORMAL", latest_b, pct,
                    f"This week's avg basis ${curr_avg:+.2f}/bu is mid-range "
                    f"({pct:.0f}th pctile). Latest: {latest_str}.")


def _sig_basis_momentum():
    if not DATA["prices"]:
        return _na("Basis momentum (4-week)", None, "No price data.")
    weekly_basis = weekly_avg_basis_by_week_ending()
    if len(weekly_basis) < 5:
        return _na("Basis momentum (4-week)", None, "Need 5+ weeks of basis data.")
    sorted_weeks = sorted(weekly_basis.keys())
    last = weekly_basis[sorted_weeks[-1]]
    prev = weekly_basis[sorted_weeks[-5]]
    delta = last - prev
    if delta > 0.10:
        return _bull("Basis momentum (4-week)", "STRENGTHENING", delta, None,
                     f"Basis has risen ${delta:+.2f}/bu over the last 4 weeks "
                     f"(${prev:+.2f} → ${last:+.2f}). Upward momentum.")
    if delta < -0.10:
        return _bear("Basis momentum (4-week)", "WEAKENING", delta, None,
                     f"Basis has fallen ${delta:+.2f}/bu over the last 4 weeks "
                     f"(${prev:+.2f} → ${last:+.2f}). Downward momentum.")
    return _neutral("Basis momentum (4-week)", "FLAT", delta, None,
                    f"Basis moved only ${delta:+.2f}/bu over 4 weeks. No clear direction.")


def _sig_pipeline():
    cgc = DATA["cgc"]
    if len(cgc) < 4:
        return _na("Pipeline tightness", None, "Need 4+ weeks.")
    last_4 = cgc[-4:]
    deliv = [r["producer_deliveries_weekly_kt"] for r in last_4
             if r["producer_deliveries_weekly_kt"] is not None]
    ship = [r["primary_shipments_weekly_kt"] for r in last_4
            if r["primary_shipments_weekly_kt"] is not None]
    if not deliv or not ship:
        return _na("Pipeline tightness", None, "Missing weekly flow data in recent weeks.")
    avg_d = sum(deliv) / len(deliv)
    avg_s = sum(ship) / len(ship)
    if avg_d <= 0:
        return _na("Pipeline tightness", None, "Zero deliveries.")
    ratio = avg_s / avg_d
    if ratio < 0.85:
        return _bear("Pipeline tightness", "BACKED UP", ratio, None,
                     f"Last 4 weeks: shipments only {ratio:.0%} of deliveries. "
                     f"Primaries filling up — basis will weaken to deter flow.")
    if ratio > 1.15:
        return _bull("Pipeline tightness", "DRAWING DOWN", ratio, None,
                     f"Last 4 weeks: shipments {ratio:.0%} of deliveries. "
                     f"Elevators pulling faster than receiving — they'll bid up to refill.")
    return _neutral("Pipeline tightness", "BALANCED", ratio, None,
                    f"Shipments at {ratio:.0%} of deliveries — pipeline in balance.")


def _bull(name, verdict, value, pct, explain):
    return {"name": name, "verdict": verdict, "direction": "BULLISH",
            "color": COL_BULL, "value": value, "percentile": pct, "explanation": explain}
def _bear(name, verdict, value, pct, explain):
    return {"name": name, "verdict": verdict, "direction": "BEARISH",
            "color": COL_BEAR, "value": value, "percentile": pct, "explanation": explain}
def _neutral(name, verdict, value, pct, explain):
    return {"name": name, "verdict": verdict, "direction": "NEUTRAL",
            "color": COL_NEUTRAL, "value": value, "percentile": pct, "explanation": explain}
def _na(name, value, explain):
    return {"name": name, "verdict": "n/a", "direction": "NEUTRAL",
            "color": COL_DIM, "value": value, "percentile": None, "explanation": explain}


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def fmt(v, nd=1):
    if v is None: return "-"
    if isinstance(v, (int, float)): return f"{v:,.{nd}f}"
    return str(v)


# ---------------------------------------------------------------------------
# Tab 1 — NOW
# ---------------------------------------------------------------------------

def build_now_tab():
    signals = compute_signals()
    if "error" in signals:
        dpg.add_text("No data — run loaders + nowcast.py first.", color=COL_BEAR); return

    dpg.add_text(f"As of week {signals['as_of_week']} (ending {signals['as_of_date']}), "
                 f"crop year {signals['current_crop_year']}", color=COL_DIM)
    dpg.add_spacer(height=12)

    label, color, explain = signals["overall"]
    with dpg.group(horizontal=True):
        dpg.add_text("RECOMMENDATION:", color=COL_HDR)
        dpg.add_text(label, color=color)
    dpg.add_text("  " + explain, color=COL_INFO)
    dpg.add_spacer(height=16)

    dpg.add_separator()
    dpg.add_text("Signal scorecard", color=COL_HDR)
    dpg.add_spacer(height=6)
    with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                   borders_outerH=True, borders_outerV=True, resizable=True,
                   policy=dpg.mvTable_SizingStretchProp):
        for h in ["Signal", "Verdict", "Direction", "Current value", "Percentile"]:
            dpg.add_table_column(label=h)
        for s in signals["signals"]:
            with dpg.table_row():
                dpg.add_text(s["name"])
                dpg.add_text(s["verdict"], color=s["color"])
                dpg.add_text(s["direction"], color=s["color"])
                v = s["value"]
                if v is None:
                    dpg.add_text("-")
                elif "Basis" in s["name"]:
                    if "momentum" in s["name"]:
                        dpg.add_text(f"{v:+.2f}/bu (Δ)")
                    else:
                        dpg.add_text(f"${v:+.2f}/bu")
                elif s["name"] == "Pipeline tightness":
                    dpg.add_text(f"{v:.0%} (ship/deliv)")
                else:
                    dpg.add_text(f"{v:,.0f} kt")
                dpg.add_text("-" if s["percentile"] is None else f"{s['percentile']:.0f}%")

    dpg.add_spacer(height=16)
    dpg.add_separator()

    cgc = latest_cgc_row(); nc = latest_nowcast_row(); basis = latest_basis_obs()
    dpg.add_text("Key numbers right now", color=COL_HDR)
    dpg.add_spacer(height=4)
    with dpg.group(horizontal=True):
        with dpg.group():
            dpg.add_text("Total AB stocks", color=COL_DIM)
            dpg.add_text(fmt(nc["estimated_total_stocks_kt"]) + " kt", color=COL_BIG)
        dpg.add_spacer(width=40)
        with dpg.group():
            dpg.add_text("Implied on-farm", color=COL_DIM)
            dpg.add_text(fmt(nc["implied_on_farm_stocks_kt"]) + " kt", color=COL_BIG)
        dpg.add_spacer(width=40)
        with dpg.group():
            dpg.add_text("Visible commercial", color=COL_DIM)
            dpg.add_text(fmt(nc["visible_commercial_stocks_kt"]) + " kt", color=COL_BIG)
        dpg.add_spacer(width=40)
        with dpg.group():
            dpg.add_text("Latest basis (S.AB)", color=COL_DIM)
            if basis:
                dpg.add_text(f"${basis['basis_per_bu']:+.2f}/bu", color=COL_BIG)
                dpg.add_text(f"  {basis['delivery_month']} on {basis['observation_date']}",
                             color=COL_DIM)


# ---------------------------------------------------------------------------
# Tab 2 — Signal Detail
# ---------------------------------------------------------------------------

def build_signal_detail_tab():
    signals = compute_signals()
    if "error" in signals:
        dpg.add_text("No data.", color=COL_BEAR); return

    dpg.add_text("Each signal explained — what it measures, what it's pointing at right now",
                 color=COL_HDR)
    dpg.add_spacer(height=10)

    for s in signals["signals"]:
        with dpg.collapsing_header(label=f"{s['name']}  →  {s['verdict']}  ({s['direction']})",
                                   default_open=True):
            dpg.add_text(s["explanation"], color=COL_INFO, wrap=1500)
            dpg.add_spacer(height=4)

    dpg.add_spacer(height=20)
    dpg.add_separator()
    dpg.add_text("How the overall recommendation is computed", color=COL_HDR)
    dpg.add_text(
        "Each signal independently classifies as BULLISH / BEARISH / NEUTRAL based on percentile\n"
        "(top 25 / bottom 25 / middle).\n"
        "  - 3+ bullish AND bullish > bearish  → HOLD\n"
        "  - 3+ bearish AND bearish > bullish  → SELL\n"
        "  - Mixed signals                     → WATCH",
        color=COL_DIM,
    )


# ---------------------------------------------------------------------------
# Tab 3 — Historical Patterns
# ---------------------------------------------------------------------------

def build_history_tab():
    if not DATA["nowcast"]:
        dpg.add_text("No nowcast data.", color=COL_BEAR); return

    cgc = latest_cgc_row()
    current_cy = cgc["crop_year"] if cgc else None

    dpg.add_text("Cross-year overlay charts", color=COL_HDR)
    dpg.add_text(f"Current crop year ({current_cy}) is the line to watch — "
                 f"where is it relative to the spread?", color=COL_DIM)
    dpg.add_spacer(height=10)

    week_map = get_week_number_map()

    def add_overlay(title, source, field, ylabel):
        dpg.add_text(title, color=COL_HDR)
        by_cy = defaultdict(list)
        for r in source:
            if source is DATA["nowcast"]:
                wn = week_map.get(r["week_ending"])
            else:
                wn = r.get("week_number")
            if wn is None or r.get(field) is None: continue
            by_cy[r["crop_year"]].append((wn, r[field]))
        with dpg.plot(height=320, width=-1):
            dpg.add_plot_legend()
            dpg.add_plot_axis(dpg.mvXAxis, label="Crop year week (1 = first wk of Aug)")
            with dpg.plot_axis(dpg.mvYAxis, label=ylabel):
                for cy in sorted(by_cy.keys()):
                    pts = sorted(by_cy[cy])
                    x = [float(p[0]) for p in pts]; y = [float(p[1]) for p in pts]
                    label = cy + (" ←NOW" if cy == current_cy else "")
                    dpg.add_line_series(x, y, label=label)
        dpg.add_spacer(height=18)

    add_overlay("Implied on-farm stocks (THE tightness signal)",
                DATA["nowcast"], "implied_on_farm_stocks_kt",
                "Implied on-farm (kt)")
    add_overlay("Visible commercial stocks (Primary + Process + Condo)",
                DATA["cgc"], "total_visible_stocks_kt", "Visible stocks (kt)")
    add_overlay("CYTD producer deliveries (Primary) — the pace gauge",
                DATA["cgc"], "producer_deliveries_cytd_kt", "CYTD deliveries (kt)")
    add_overlay("Weekly Primary deliveries (raw flow)",
                DATA["cgc"], "producer_deliveries_weekly_kt", "Weekly deliveries (kt)")


# ---------------------------------------------------------------------------
# Tab 4 — Data Quality
# ---------------------------------------------------------------------------

def build_quality_tab():
    statscan = DATA["statscan"]; cgc = DATA["cgc"]; nc = DATA["nowcast"]; pr = DATA["prices"]

    dpg.add_text("Data quality & freshness", color=COL_HDR)
    dpg.add_spacer(height=10)

    with dpg.group(horizontal=True):
        with dpg.group():
            dpg.add_text("Latest CGC week", color=COL_DIM)
            if cgc: dpg.add_text(cgc[-1]["week_ending"], color=COL_BIG)
        dpg.add_spacer(width=40)
        with dpg.group():
            dpg.add_text("Latest StatsCan snapshot", color=COL_DIM)
            if statscan: dpg.add_text(statscan[-1]["snapshot_date"], color=COL_BIG)
        dpg.add_spacer(width=40)
        with dpg.group():
            dpg.add_text("Latest basis quote", color=COL_DIM)
            if pr: dpg.add_text(pr[-1]["observation_date"], color=COL_BIG)
    dpg.add_spacer(height=20)

    dpg.add_text("CGC weeks loaded per crop year", color=COL_HDR)
    cgc_by_cy = defaultdict(list)
    for r in cgc: cgc_by_cy[r["crop_year"]].append(r["week_number"])
    for cy in sorted(cgc_by_cy.keys()):
        weeks = sorted(set(cgc_by_cy[cy]))
        missing = sorted(set(range(weeks[0], weeks[-1] + 1)) - set(weeks))
        tag = "" if not missing else f"  missing: {missing}"
        dpg.add_text(f"  {cy}:  {len(weeks)} weeks{tag}")
    dpg.add_spacer(height=12)

    dpg.add_text("StatsCan snapshots per crop year", color=COL_HDR)
    sc_by_cy = defaultdict(set)
    for s in statscan: sc_by_cy[s["crop_year"]].add(s["snapshot_month"])
    order = {"December": 0, "March": 1, "July": 2}
    for cy in sorted(sc_by_cy.keys()):
        snaps = sorted(sc_by_cy[cy], key=lambda m: order.get(m, 99))
        dpg.add_text(f"  {cy}:  {', '.join(snaps)}")
    dpg.add_spacer(height=20)

    dpg.add_text("Year-end nowcast vs StatsCan truth (should be ~0)", color=COL_HDR)
    dpg.add_text("Non-zero gap = stale nowcast, missing anchor, or unfinished crop year.",
                 color=COL_DIM)
    with dpg.table(header_row=True, borders_innerH=True, borders_outerH=True,
                   borders_outerV=True, borders_innerV=True, resizable=True,
                   policy=dpg.mvTable_SizingStretchProp):
        for h in ["CY", "Nowcast last-week total", "SC July ending", "Gap"]:
            dpg.add_table_column(label=h)
        for cy in sorted({r["crop_year"] for r in nc}):
            last_nc = next((r for r in reversed(nc) if r["crop_year"] == cy), None)
            sc_jul = next((s for s in statscan if s["crop_year"] == cy and s["snapshot_month"] == "July"), None)
            with dpg.table_row():
                dpg.add_text(cy)
                dpg.add_text(fmt(last_nc["estimated_total_stocks_kt"]) if last_nc else "-")
                dpg.add_text(fmt(sc_jul["ending_stocks_kt"]) if sc_jul else "-")
                if last_nc and sc_jul:
                    gap = last_nc["estimated_total_stocks_kt"] - sc_jul["ending_stocks_kt"]
                    color = COL_BULL if abs(gap) < 1 else (COL_WARN if abs(gap) < 50 else COL_BEAR)
                    dpg.add_text(f"{gap:+.1f}", color=color)
                else:
                    dpg.add_text("-")


# ---------------------------------------------------------------------------
# Tab 5 — Raw Data
# ---------------------------------------------------------------------------

def build_raw_tab():
    dpg.add_text("Raw tables (when signals leave you wanting receipts)", color=COL_HDR)
    dpg.add_spacer(height=10)

    cgc_years = sorted({r["crop_year"] for r in DATA["cgc"]})
    with dpg.group(horizontal=True):
        dpg.add_text("Filter by crop year:")
        dpg.add_combo(["ALL"] + cgc_years,
                      default_value=cgc_years[-1] if cgc_years else "ALL",
                      width=120, callback=_raw_filter_callback, tag="raw_filter")
    dpg.add_spacer(height=8)
    with dpg.child_window(tag="raw_content", border=False, height=-1):
        _build_raw_content(cgc_years[-1] if cgc_years else "ALL")


def _raw_filter_callback(sender, app_data):
    dpg.delete_item("raw_content", children_only=True)
    dpg.push_container_stack("raw_content")
    _build_raw_content(app_data)
    dpg.pop_container_stack()


def _build_raw_content(cy_filter):
    cgc = DATA["cgc"]; nc = DATA["nowcast"]
    if cy_filter != "ALL":
        cgc = [r for r in cgc if r["crop_year"] == cy_filter]
        nc  = [r for r in nc if r["crop_year"] == cy_filter]

    with dpg.collapsing_header(label=f"Nowcast ({len(nc)} rows)", default_open=True):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp, scrollY=True, height=300):
            for h in ["CY", "Week ending", "Total stocks", "Visible commercial", "Implied on-farm"]:
                dpg.add_table_column(label=h)
            for r in nc:
                with dpg.table_row():
                    dpg.add_text(r["crop_year"])
                    dpg.add_text(r["week_ending"])
                    dpg.add_text(fmt(r["estimated_total_stocks_kt"]))
                    dpg.add_text(fmt(r["visible_commercial_stocks_kt"]))
                    dpg.add_text(fmt(r["implied_on_farm_stocks_kt"]))

    with dpg.collapsing_header(label=f"CGC weekly ({len(cgc)} rows)", default_open=False):
        with dpg.table(header_row=True, borders_innerH=True, borders_innerV=True,
                       borders_outerH=True, borders_outerV=True, resizable=True,
                       policy=dpg.mvTable_SizingStretchProp, scrollY=True, height=300):
            for h in ["Wk", "Ending", "CY", "PD wk", "PD CYTD", "Pri ship wk",
                      "Pri stk", "Proc stk", "Condo", "Total visible"]:
                dpg.add_table_column(label=h)
            for r in cgc:
                with dpg.table_row():
                    dpg.add_text(str(r["week_number"]))
                    dpg.add_text(r["week_ending"])
                    dpg.add_text(r["crop_year"])
                    dpg.add_text(fmt(r["producer_deliveries_weekly_kt"]))
                    dpg.add_text(fmt(r["producer_deliveries_cytd_kt"]))
                    dpg.add_text(fmt(r["primary_shipments_weekly_kt"]))
                    dpg.add_text(fmt(r["primary_elevator_stocks_kt"]))
                    dpg.add_text(fmt(r["process_elevator_stocks_kt"]))
                    dpg.add_text(fmt(r["condo_storage_kt"]))
                    dpg.add_text(fmt(r["total_visible_stocks_kt"]))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}"); return

    fetch_all()
    print(f"Loaded {len(DATA['statscan'])} statscan, {len(DATA['cgc'])} cgc, "
          f"{len(DATA['nowcast'])} nowcast, {len(DATA['prices'])} prices")

    dpg.create_context()
    with dpg.window(label="Alberta Canola Signals", tag="primary_window"):
        dpg.add_text("Alberta Canola — Actionable Signals", color=COL_HDR)
        dpg.add_text(f"Database: {DB_PATH}", color=COL_DIM)
        dpg.add_separator()
        with dpg.tab_bar():
            with dpg.tab(label="NOW"):              build_now_tab()
            with dpg.tab(label="Signal Detail"):    build_signal_detail_tab()
            with dpg.tab(label="Historical Patterns"): build_history_tab()
            with dpg.tab(label="Data Quality"):     build_quality_tab()
            with dpg.tab(label="Raw Data"):         build_raw_tab()

    dpg.create_viewport(title="Alberta Canola Signals", width=1700, height=950)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("primary_window", True)
    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
