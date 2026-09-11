"""
Builds a single self-contained, offline-viewable HTML dashboard from a live
pull of the Impuruza program's events - reuses monitor_open_signals.py's
fetch_all_events(), which sweeps the whole nationwide event set in one fast
pass (~900 events per the Scale note in docs/API_NOTES.md), NOT the
expensive 48k tracked-entity crawl fetch_tracked_entities.py does.

Covers:
  - KPI row: total/confirmed/discarded/open signals, avg report->verification
    hours, % exceeding the 48h verification SLA
  - Signals reported per month - a trend line surfacing reporting-volume
    drop-offs (e.g. a recent month reporting far below the running average)
  - Verification outcome split (Confirmed / Discarded / Open)
  - Channel yield by EBS Type (Impuruza_EBS Type)
  - Signal timeliness cascade - computed here directly from raw timestamps
    at REAL hour precision. This is more precise than the DHIS2 Program
    Indicator equivalent in dhis2/program-indicators/, which is limited to
    day-truncated hours (see that folder's README).
  - Top human diseases / top animal diseases reported
  - One Health cross-signal correlation - same join+scoring logic as
    sql/one_health_correlation.sql and scripts/one_health_correlation.py,
    reimplemented in plain Python (no pandas dependency needed for a
    ~900-row dataset)
  - Top reporting locations by confirmed signal count

NOT included: Active Lookout Ratio (item 5) - that needs the separate,
expensive 48k tracked-entity sweep. Run
`scripts/audit_lookout_engagement.py --all` for that separately.

Privacy: aggregate-only. This script only ever calls /api/tracker/events
(program-stage data), never /api/tracker/trackedEntities - so it never even
receives a lookout's name or phone number, let alone displays one.

Usage:
    python3 scripts/build_html_dashboard.py
    open dashboard/impuruza_dashboard.html
"""

from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fetch_tracked_entities import build_decoder_maps, decode_value  # noqa: E402
from monitor_open_signals import (  # noqa: E402
    NATIONAL_ORG_UNIT,
    fetch_all_events,
    fetch_org_unit_names,
    parse_dhis2_datetime,
)
from dhis2_client import DHIS2Client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "dashboard"
OUT_PATH = OUT_DIR / "impuruza_dashboard.html"

# Field labels - see data/metadata/program.json for the underlying data element IDs
F_OUTCOME = "Signal Verification Outcome"
F_EBS_TYPE = "Impuruza_EBS Type"
F_DISEASE = "Disease eCBS"
F_ANIMAL_DISEASE = "Impuruza_Animal Disease"
F_SPECIES = "Impuruza_Type of Species"
F_CASE_COUNT = "# CEB Cases"
F_DEATHS = "Impuruza_Number of Deaths"
F_ANIMAL_AFFECTED = "Impuruza_Number of affected Animals"
F_ONSET = "Impuruza_When the event started?"
F_DETECTED = "Impuruza_when the event was detected?"
F_VERIFY_ACTUAL = "Impuruza_Actual Date of signal verification"
F_VERIFY_PLANNED = "Impuruza_Date of signal verification"

# Same 16 reference pairs as sql/one_health_correlation.sql / scripts/one_health_correlation.py -
# keep all three in sync if you edit one.
ZOONOTIC_DISEASE_MAP = [
    ("Anthrax", "Anthrax", "Cattle", 1.00),
    ("Anthrax", "Anthrax", "Goat", 0.90),
    ("Anthrax", "Anthrax", "Sheep", 0.85),
    ("Human Rabies", "Rabies", "Dog", 1.00),
    ("Human Rabies", "Rabies", "Cat", 0.60),
    ("Rift Valley Fever", "Rift Valley Fever", "Cattle", 0.95),
    ("Rift Valley Fever", "Rift Valley Fever", "Goat", 0.85),
    ("Rift Valley Fever", "Rift Valley Fever", "Sheep", 0.85),
    ("Human influenza due to a new subtype", "High Pathogenic Influenza virus", "Chicken", 0.90),
    ("Human influenza due to a new subtype", "High Pathogenic Influenza virus", "Duck", 0.85),
    ("Monkey  pox", "Monkeypox", None, 0.75),
    ("Zika", "Zika", None, 0.55),
    ("Viral hemorrhagic fever", "Crimean Congo hemorrhagic fever", "Cattle", 0.70),
    ("Viral hemorrhagic fever", "Crimean Congo hemorrhagic fever", "Goat", 0.65),
    ("Foodborne illnesses", "Salmonellosis", "Chicken", 0.60),
    ("Foodborne illnesses", "Campylobacter", "Cattle", 0.55),
]
LOW_CONFIDENCE_DEFAULT_WEIGHT = 0.15
WINDOW_DAYS = 14

OUTCOME_ORDER = ["Confirmed", "Discarded", "Open / pending"]
OUTCOME_COLOR_VAR = {"Confirmed": "--series-1", "Discarded": "--series-2", "Open / pending": "--series-3"}


# ---------------------------------------------------------------- data pull

def to_number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def decode_event(ev, field_names, field_option_sets, option_code_labels) -> dict:
    out = {}
    for dv in ev.get("dataValues", []):
        label, val = decode_value(dv["dataElement"], dv["value"], field_names, field_option_sets, option_code_labels)
        out[label] = val
    return out


def hours_between(a: str | None, b: str | None) -> float | None:
    """b - a, in hours. None if either timestamp is missing/unparseable."""
    if not a or not b:
        return None
    try:
        return (parse_dhis2_datetime(b) - parse_dhis2_datetime(a)).total_seconds() / 3600.0
    except ValueError:
        return None


def build_dataset(events, field_names, field_option_sets, option_code_labels) -> list[dict]:
    rows = []
    for ev in events:
        dv = decode_event(ev, field_names, field_option_sets, option_code_labels)
        rows.append(
            {
                "event": ev["event"],
                "orgUnit": ev.get("orgUnit"),
                "occurredAt": ev.get("occurredAt"),
                "outcome": dv.get(F_OUTCOME),
                "ebsType": dv.get(F_EBS_TYPE),
                "disease": dv.get(F_DISEASE),
                "animalDisease": dv.get(F_ANIMAL_DISEASE),
                "species": dv.get(F_SPECIES),
                "caseCount": to_number(dv.get(F_CASE_COUNT)),
                "deaths": to_number(dv.get(F_DEATHS)),
                "animalAffected": to_number(dv.get(F_ANIMAL_AFFECTED)),
                "onset": dv.get(F_ONSET),
                "detected": dv.get(F_DETECTED),
                "verifyActual": dv.get(F_VERIFY_ACTUAL),
                "verifyPlanned": dv.get(F_VERIFY_PLANNED),
            }
        )
    return rows


# ---------------------------------------------------------------- aggregation

def outcome_bucket(outcome_raw) -> str:
    # Normalized the same way monitor_open_signals.py's CLOSED_OUTCOMES check does, to
    # absorb this instance's inconsistent casing/trailing whitespace (e.g. "Confirmed ").
    norm = (outcome_raw or "").strip().lower()
    if norm == "confirmed":
        return "Confirmed"
    if norm == "discarded":
        return "Discarded"
    return "Open / pending"


def compute_outcome_funnel(rows) -> Counter:
    return Counter(outcome_bucket(r["outcome"]) for r in rows)


def compute_channel_yield(rows) -> dict:
    by_channel: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        channel = r["ebsType"] or "Unspecified"
        by_channel[channel][outcome_bucket(r["outcome"])] += 1
    return by_channel


def compute_timeliness(rows) -> dict:
    onset_detect, detect_report, report_verify = [], [], []
    negative_flags = 0
    over_48h = 0
    verify_complete = 0
    for r in rows:
        h1 = hours_between(r["onset"], r["detected"])
        if h1 is not None:
            if h1 < 0:
                negative_flags += 1
            else:
                onset_detect.append(h1)

        h2 = hours_between(r["detected"], r["occurredAt"])
        if h2 is not None:
            if h2 < 0:
                negative_flags += 1
            else:
                detect_report.append(h2)

        verify_date = r["verifyActual"] or r["verifyPlanned"]
        if outcome_bucket(r["outcome"]) in ("Confirmed", "Discarded"):
            h3 = hours_between(r["occurredAt"], verify_date)
            if h3 is not None:
                verify_complete += 1
                if h3 < 0:
                    negative_flags += 1
                else:
                    report_verify.append(h3)
                    if h3 > 48:
                        over_48h += 1

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "onsetDetectAvgHrs": avg(onset_detect),
        "detectReportAvgHrs": avg(detect_report),
        "reportVerifyAvgHrs": avg(report_verify),
        "pctOver48h": (100 * over_48h / verify_complete) if verify_complete else None,
        "verifyCompleteCount": verify_complete,
        "negativeLagFlags": negative_flags,
    }


def top_counts(rows, field, n=8):
    counts = Counter(r[field] for r in rows if r.get(field))
    ranked = counts.most_common()
    top = ranked[:n]
    other = sum(c for _, c in ranked[n:])
    if other:
        top.append(("Other", other))
    return top


def one_health_matches(rows, window_days=WINDOW_DAYS) -> list[dict]:
    human = [r for r in rows if r["disease"] and r["orgUnit"] and r["occurredAt"]]
    animal = [r for r in rows if r["animalDisease"] and r["orgUnit"] and r["occurredAt"]]

    weight_lookup = {(h, a, sp): w for h, a, sp, w in ZOONOTIC_DISEASE_MAP}

    def weight_for(h_disease, a_disease, species):
        # exact species match first, then the species=None ("any species") wildcard row
        if (h_disease, a_disease, species) in weight_lookup:
            return weight_lookup[(h_disease, a_disease, species)]
        if (h_disease, a_disease, None) in weight_lookup:
            return weight_lookup[(h_disease, a_disease, None)]
        return LOW_CONFIDENCE_DEFAULT_WEIGHT

    results = []
    for h in human:
        h_time = parse_dhis2_datetime(h["occurredAt"])
        for a in animal:
            if a["orgUnit"] != h["orgUnit"]:
                continue
            a_time = parse_dhis2_datetime(a["occurredAt"])
            delta_days = abs((h_time - a_time).total_seconds()) / 86400.0
            if delta_days > window_days:
                continue
            weight = weight_for(h["disease"], a["animalDisease"], a["species"])
            magnitude = math.log1p((h["caseCount"] or 0) + (h["deaths"] or 0) * 3 + (a["animalAffected"] or 0))
            decay = math.exp(-delta_days / 14.0)
            score = min(100.0, 100 * weight * decay * magnitude)
            results.append(
                {
                    "sector": h["orgUnit"],
                    "humanDisease": h["disease"],
                    "animalDisease": a["animalDisease"],
                    "species": a["species"] or "-",
                    "timeDeltaDays": round(delta_days, 1),
                    "score": round(score, 2),
                }
            )
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def compute_monthly_counts(rows) -> list[tuple[str, int]]:
    """Chronological signal counts by month (event/report date - occurredAt), to
    visualize the reporting-volume trend and surface recent drop-offs. Returns
    [(YYYY-MM, count), ...] sorted oldest to newest."""
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if not r["occurredAt"]:
            continue
        try:
            d = parse_dhis2_datetime(r["occurredAt"])
        except ValueError:
            continue
        counts[d.strftime("%Y-%m")] += 1
    return [(ym, counts[ym]) for ym in sorted(counts)]


def month_short(ym: str) -> str:
    return datetime.strptime(ym, "%Y-%m").strftime("%b '%y")


def month_full(ym: str) -> str:
    return datetime.strptime(ym, "%Y-%m").strftime("%B %Y")


def monthly_trend_note(monthly: list[tuple[str, int]], current_ym: str) -> str:
    """Plain-English, DATA-DRIVEN summary of the trend - never a hardcoded claim,
    always computed from whatever the live pull actually returned. Excludes the
    current calendar month from the comparison, since it's still accumulating and
    isn't a like-for-like count against a complete month."""
    complete = [(ym, c) for ym, c in monthly if ym != current_ym]
    if len(complete) < 2:
        return "Not enough complete months of data yet to compare a trend."
    *prior, last = complete
    last_label, last_count = last
    if not prior:
        return f"Only one complete month of data so far ({month_full(last_label)}: {last_count} signals)."
    prior_avg = sum(c for _, c in prior) / len(prior)
    if prior_avg == 0:
        return f"{month_full(last_label)} recorded {last_count} signals."
    pct_change = 100 * (last_count - prior_avg) / prior_avg
    direction = "down" if pct_change < 0 else "up"
    return (
        f"{month_full(last_label)} recorded {last_count} signals vs a {prior_avg:.0f}/month average "
        f"over the {len(prior)} prior complete month(s) - {abs(pct_change):.0f}% {direction}. "
        f"The current month (not shown in this comparison) is still accumulating."
    )


def top_locations(rows, org_unit_names, n=10):
    counts = Counter(r["orgUnit"] for r in rows if outcome_bucket(r["outcome"]) == "Confirmed" and r["orgUnit"])
    ranked = counts.most_common(n)
    return [(org_unit_names.get(oid, oid), c) for oid, c in ranked]


# ---------------------------------------------------------------- rendering

BAR_H = 24
BAR_GAP = 10
CHART_WIDTH = 640
LABEL_COL = 220


def truncate_label(label: str, max_chars: int = 26) -> str:
    """The visible axis label is measured against a fixed budget (LABEL_COL) and must
    never overflow the SVG's left edge - truncate with an ellipsis rather than clip.
    The full text is preserved everywhere else (tooltip data-label, table fallback)."""
    label = str(label)
    return label if len(label) <= max_chars else label[: max_chars - 1].rstrip() + "…"


def fmt_num(n) -> str:
    if n is None:
        return "N/A"
    if isinstance(n, float) and n != int(n):
        return f"{n:,.1f}"
    return f"{int(n):,}"


def bar_svg(rows: list[tuple], color_var: str, unit: str = "", value_fmt=fmt_num) -> str:
    """rows: list of (label, value). One flat hue, values labeled at the bar tip."""
    if not rows:
        return '<p class="empty">No data.</p>'
    max_val = max(v for _, v in rows) or 1
    plot_w = CHART_WIDTH - LABEL_COL - 70
    height = len(rows) * (BAR_H + BAR_GAP)
    bars = []
    for i, (label, val) in enumerate(rows):
        y = i * (BAR_H + BAR_GAP)
        w = max(2, (val / max_val) * plot_w)
        bars.append(
            f'<g class="bar-row">'
            f'<text x="{LABEL_COL - 10}" y="{y + BAR_H / 2}" text-anchor="end" dominant-baseline="middle" '
            f'class="bar-label"><title>{escape(str(label))}</title>{escape(truncate_label(label))}</text>'
            f'<rect class="bar hoverable" tabindex="0" data-label="{escape(str(label))}" data-value="{escape(value_fmt(val))}{unit}" '
            f'x="{LABEL_COL}" y="{y}" width="{w:.1f}" height="{BAR_H}" rx="4" fill="var({color_var})"/>'
            f'<text x="{LABEL_COL + w + 8:.1f}" y="{y + BAR_H / 2}" dominant-baseline="middle" '
            f'class="bar-value">{value_fmt(val)}{unit}</text>'
            f"</g>"
        )
    return (
        f'<svg viewBox="0 0 {CHART_WIDTH} {height}" width="100%" height="{height}" role="img" '
        f'aria-label="Bar chart">{"".join(bars)}</svg>'
    )


def stacked_bar_svg(categories: list[str], series_by_category: dict, series_order: list[str]) -> str:
    """One horizontal stacked bar per category - part-to-whole, categorical colors."""
    plot_w = CHART_WIDTH - LABEL_COL - 20
    height = len(categories) * (BAR_H + BAR_GAP)
    bars = []
    for i, cat in enumerate(categories):
        counts = series_by_category[cat]
        total = sum(counts.get(s, 0) for s in series_order) or 1
        y = i * (BAR_H + BAR_GAP)
        x = LABEL_COL
        segs = []
        for s in series_order:
            v = counts.get(s, 0)
            if not v:
                continue
            w = max(0, (v / total) * plot_w - 2)  # -2px surface gap between segments
            pct = 100 * v / total
            segs.append(
                f'<rect class="bar hoverable" tabindex="0" data-label="{escape(cat)} - {escape(s)}" '
                f'data-value="{v} ({pct:.0f}%)" x="{x:.1f}" y="{y}" width="{w:.1f}" height="{BAR_H}" '
                f'rx="4" fill="var({OUTCOME_COLOR_VAR[s]})"/>'
            )
            x += w + 2
        bars.append(
            f'<g class="bar-row">'
            f'<text x="{LABEL_COL - 10}" y="{y + BAR_H / 2}" text-anchor="end" dominant-baseline="middle" '
            f'class="bar-label"><title>{escape(cat)}</title>{escape(truncate_label(cat))}</text>{"".join(segs)}'
            f'<text x="{x + 8:.1f}" y="{y + BAR_H / 2}" dominant-baseline="middle" class="bar-value">{total}</text>'
            f"</g>"
        )
    return (
        f'<svg viewBox="0 0 {CHART_WIDTH} {height}" width="100%" height="{height}" role="img" '
        f'aria-label="Stacked bar chart">{"".join(bars)}</svg>'
    )


def line_chart_svg(points: list[tuple[str, int]], current_ym: str, color_var: str = "--series-1") -> str:
    """Trend-over-time line for signals-per-month: 2px line, round caps, >=8px
    markers, sparing endpoint label, hover tooltip via the shared .hoverable
    binding. The current (still-accumulating) month renders as a hollow ring
    rather than a filled dot, so it's visually distinct from complete months -
    never implying it's a finished count."""
    if not points:
        return '<p class="empty">No data.</p>'
    width, height = CHART_WIDTH, 220
    pad_left, pad_right, pad_top, pad_bottom = 12, 40, 20, 32
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    max_val = max(v for _, v in points) or 1
    n = len(points)
    step = plot_w / max(1, n - 1)

    def xy(i, v):
        return pad_left + i * step, pad_top + plot_h - (v / max_val) * plot_h

    coords = [xy(i, v) for i, (_, v) in enumerate(points)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    baseline_y = pad_top + plot_h

    els = [
        f'<line x1="{pad_left}" y1="{baseline_y}" x2="{pad_left + plot_w}" y2="{baseline_y}" stroke="var(--baseline)" stroke-width="1"/>',
        f'<path d="{path}" fill="none" stroke="var({color_var})" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    ]
    for (ym, val), (x, y) in zip(points, coords):
        is_current = ym == current_ym
        fill = "var(--surface-1)" if is_current else f"var({color_var})"
        els.append(
            f'<circle class="hoverable" tabindex="0" data-label="{escape(month_full(ym))}{" (in progress)" if is_current else ""}" '
            f'data-value="{val}" cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{fill}" stroke="var({color_var})" stroke-width="2"/>'
        )
        els.append(f'<text x="{x:.1f}" y="{baseline_y + 16}" text-anchor="middle" class="bar-label" font-size="10">{escape(month_short(ym))}</text>')
    last_x, last_y = coords[-1]
    els.append(f'<text x="{last_x:.1f}" y="{last_y - 12:.1f}" text-anchor="middle" class="bar-value">{points[-1][1]}</text>')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
        f'aria-label="Line chart of signals reported per month">{"".join(els)}</svg>'
    )


def legend_html(series_order: list[str]) -> str:
    items = "".join(
        f'<span class="legend-item"><span class="swatch" style="background:var({OUTCOME_COLOR_VAR[s]})"></span>{escape(s)}</span>'
        for s in series_order
    )
    return f'<div class="legend">{items}</div>'


def table_details(headers: list[str], rows: list[list[str]], summary="View data as table") -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<details class='table-fallback'><summary>{summary}</summary><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></details>"


def stat_tile(label: str, value: str, status: str | None = None) -> str:
    status_class = f" status-{status}" if status else ""
    return f'<div class="tile{status_class}"><div class="tile-label">{escape(label)}</div><div class="tile-value">{escape(value)}</div></div>'


def sla_status(pct: float | None) -> str:
    if pct is None:
        return "muted"
    if pct >= 25:
        return "critical"
    if pct >= 10:
        return "warning"
    return "good"


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Impuruza / CEBS Signal Analytics</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
    --status-good: #0ca30c; --status-warning: #fab219; --status-serious: #ec835a; --status-critical: #d03b3b;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--page); font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color:var(--text-primary); }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:32px 24px 80px; }}
  header.page-head {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .subtitle {{ color:var(--text-secondary); font-size:13px; margin:0; }}
  .kpi-row {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px,1fr)); gap:12px; margin-bottom:32px; }}
  .tile {{ background:var(--surface-1); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }}
  .tile-label {{ font-size:12px; color:var(--text-secondary); margin-bottom:6px; }}
  .tile-value {{ font-size:26px; font-weight:600; }}
  .tile.status-good .tile-value {{ color: var(--status-good); }}
  .tile.status-warning .tile-value {{ color: var(--status-warning); }}
  .tile.status-critical .tile-value {{ color: var(--status-critical); }}
  section.card {{ background:var(--surface-1); border:1px solid var(--border); border-radius:12px; padding:20px 24px; margin-bottom:24px; }}
  section.card h2 {{ font-size:15px; margin:0 0 4px; }}
  section.card .note {{ font-size:12px; color:var(--text-muted); margin:0 0 16px; }}
  .bar-label {{ font-size:12px; fill:var(--text-secondary); }}
  .bar-value {{ font-size:12px; fill:var(--text-primary); font-variant-numeric: tabular-nums; }}
  .hoverable {{ cursor:pointer; }}
  .hoverable:focus {{ outline:2px solid var(--series-1); }}
  .legend {{ display:flex; gap:16px; margin:12px 0 0; font-size:12px; color:var(--text-secondary); flex-wrap:wrap; }}
  .legend-item {{ display:flex; align-items:center; gap:6px; }}
  .swatch {{ width:10px; height:10px; border-radius:2px; display:inline-block; }}
  .empty {{ color:var(--text-muted); font-size:13px; }}
  details.table-fallback {{ margin-top:12px; font-size:12px; }}
  details.table-fallback summary {{ cursor:pointer; color:var(--text-secondary); }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; font-size:12px; }}
  th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); font-variant-numeric: tabular-nums; }}
  th {{ color:var(--text-muted); font-weight:500; }}
  .caveats {{ font-size:12px; color:var(--text-secondary); line-height:1.6; }}
  .caveats li {{ margin-bottom:6px; }}
  #tooltip {{ position:fixed; pointer-events:none; background:var(--text-primary); color:var(--surface-1); font-size:12px;
    padding:6px 10px; border-radius:6px; opacity:0; transform:translate(-50%,-120%); transition:opacity .08s; z-index:10; white-space:nowrap; }}
  #tooltip.show {{ opacity:1; }}
  .grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
  @media (max-width: 800px) {{ .grid-2 {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="viz-root">
<div class="wrap">
  <header class="page-head">
    <div>
      <h1>Impuruza / CEBS Signal Analytics</h1>
      <p class="subtitle">cbs2.moh.gov.rw &middot; nationwide &middot; generated {generated_at} &middot; {total_events} events scanned</p>
    </div>
  </header>

  <div class="kpi-row">
    {kpi_tiles}
  </div>

  <section class="card">
    <h2>Signals reported per month</h2>
    <p class="note">{monthly_trend_note}</p>
    {monthly_chart}
  </section>

  <section class="card">
    <h2>Signal verification outcome</h2>
    <p class="note">Part-to-whole split of every scanned signal. "Open / pending" signals have not yet been marked Confirmed or Discarded.</p>
    {outcome_chart}
    {outcome_legend}
    {outcome_table}
  </section>

  <section class="card">
    <h2>Channel yield by EBS Type</h2>
    <p class="note">Confirmed vs. Discarded vs. still-Open, per intake channel - identifies which channels produce real signals vs. noise.</p>
    {channel_chart}
    {channel_legend}
    {channel_table}
  </section>

  <section class="card">
    <h2>Signal timeliness cascade (hours)</h2>
    <p class="note">Computed directly from raw timestamps at real hour precision (not the day-truncated DHIS2 Program Indicator approximation - see dhis2/program-indicators/README.md). Based on {verify_complete_count} verification-complete signals.</p>
    {timeliness_chart}
  </section>

  <div class="grid-2">
    <section class="card">
      <h2>Top human diseases reported</h2>
      <p class="note">Disease eCBS field, all scanned signals.</p>
      {human_disease_chart}
      {human_disease_table}
    </section>
    <section class="card">
      <h2>Top animal diseases reported</h2>
      <p class="note">One Health / cross-cutting animal disease list.</p>
      {animal_disease_chart}
      {animal_disease_table}
    </section>
  </div>

  <section class="card">
    <h2>One Health cross-signal correlation</h2>
    <p class="note">Human and animal disease signals sharing an org unit within a {window_days}-day window, scored for zoonotic spillover plausibility against a seeded reference map (16 pairs, NOT exhaustive - see sql/one_health_correlation.sql). Undocumented pairs default to a low-confidence weight rather than being dropped.</p>
    {one_health_table}
  </section>

  <section class="card">
    <h2>Top reporting locations (confirmed signals)</h2>
    <p class="note">Ranked by the event's assigned org unit. Caveat: this is the reporting facility's org unit, not necessarily the free-text "Impuruza_Sector" field - see dhis2/program-indicators/README.md's geography caveat before treating this as validated hotspot geography.</p>
    {locations_chart}
    {locations_table}
  </section>

  <section class="card">
    <h2>Data quality &amp; scope notes</h2>
    <ul class="caveats">
      <li><strong>{negative_lag_flags} signals</strong> have an impossible date ordering (e.g. detection recorded before onset) and were excluded from the timeliness averages above rather than silently counted as zero or negative.</li>
      <li>Zero PII: this page is built entirely from <code>/api/tracker/events</code> (program-stage data) - it never calls the tracked-entity endpoint, so no lookout name or phone number is ever fetched, let alone displayed.</li>
      <li><strong>Active Lookout Ratio is not included here</strong> - it needs the separate, expensive 48k tracked-entity sweep. Run <code>scripts/audit_lookout_engagement.py --all</code> and fold its output in separately.</li>
      <li>"Confirmed"/"Discarded" outcome matching is normalized (stripped + lowercased) to absorb this instance's inconsistent casing/whitespace on that field - the DHIS2-native Program Indicator equivalent cannot do this normalization (no trim()/lower() in PI expression syntax).</li>
    </ul>
  </section>
</div>
</div>
<div id="tooltip"></div>
<script>
  (function() {{
    var tip = document.getElementById('tooltip');
    document.querySelectorAll('.hoverable').forEach(function(el) {{
      el.addEventListener('mousemove', function(e) {{
        tip.textContent = el.dataset.label + ': ' + el.dataset.value;
        tip.style.left = e.clientX + 'px';
        tip.style.top = e.clientY + 'px';
        tip.classList.add('show');
      }});
      el.addEventListener('mouseleave', function() {{ tip.classList.remove('show'); }});
      el.addEventListener('focus', function() {{
        var r = el.getBoundingClientRect();
        tip.textContent = el.dataset.label + ': ' + el.dataset.value;
        tip.style.left = (r.left + r.width / 2) + 'px';
        tip.style.top = r.top + 'px';
        tip.classList.add('show');
      }});
      el.addEventListener('blur', function() {{ tip.classList.remove('show'); }});
    }});
  }})();
</script>
</body>
</html>
"""


def main() -> None:
    client = DHIS2Client()
    field_names, field_option_sets, option_code_labels = build_decoder_maps()

    print("Pulling live events from cbs2.moh.gov.rw (nationwide, program-stage data only)...")
    events = fetch_all_events(client, NATIONAL_ORG_UNIT)
    print(f"  ...{len(events)} events fetched")

    rows = build_dataset(events, field_names, field_option_sets, option_code_labels)

    funnel = compute_outcome_funnel(rows)
    channel_yield = compute_channel_yield(rows)
    timeliness = compute_timeliness(rows)
    monthly = compute_monthly_counts(rows)
    current_ym = datetime.now().strftime("%Y-%m")
    human_top = top_counts(rows, "disease")
    animal_top = top_counts(rows, "animalDisease")
    one_health = one_health_matches(rows)

    org_unit_ids = {r["orgUnit"] for r in rows if r["orgUnit"]}
    print(f"Resolving {len(org_unit_ids)} org unit names...")
    org_unit_names = fetch_org_unit_names(client, org_unit_ids)
    locations = top_locations(rows, org_unit_names)

    total = len(rows)
    confirmed = funnel.get("Confirmed", 0)
    discarded = funnel.get("Discarded", 0)
    open_pending = funnel.get("Open / pending", 0)
    pct_over_48h = timeliness["pctOver48h"]

    kpi_tiles = "".join(
        [
            stat_tile("Total signals", fmt_num(total)),
            stat_tile("Confirmed", fmt_num(confirmed)),
            stat_tile("Discarded", fmt_num(discarded)),
            stat_tile("Open / pending", fmt_num(open_pending)),
            stat_tile("Avg report→verify (hrs)", fmt_num(timeliness["reportVerifyAvgHrs"])),
            stat_tile(
                "% exceeding 48h SLA",
                (f"{pct_over_48h:.0f}%" if pct_over_48h is not None else "N/A"),
                status=sla_status(pct_over_48h),
            ),
        ]
    )

    outcome_rows = [(k, funnel.get(k, 0)) for k in OUTCOME_ORDER]
    outcome_categories = ["All signals"]
    outcome_by_cat = {"All signals": funnel}

    channel_categories = sorted(channel_yield.keys(), key=lambda c: -sum(channel_yield[c].values()))

    timeliness_rows = [
        ("Onset → Detection", timeliness["onsetDetectAvgHrs"] or 0),
        ("Detection → Report", timeliness["detectReportAvgHrs"] or 0),
        ("Report → Verification", timeliness["reportVerifyAvgHrs"] or 0),
    ]

    one_health_display = one_health[:15]
    one_health_html = (
        table_details(
            ["Location", "Human disease", "Animal disease", "Species", "Days apart", "Spillover score"],
            [
                [org_unit_names.get(m["sector"], m["sector"]), m["humanDisease"], m["animalDisease"], m["species"], m["timeDeltaDays"], m["score"]]
                for m in one_health_display
            ],
            summary=f"{len(one_health)} candidate pair(s) found - showing top {len(one_health_display)}",
        )
        if one_health
        else '<p class="empty">No cross-domain human/animal signal pairs found within the current event set and 14-day window.</p>'
    )

    html = PAGE_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_events=total,
        kpi_tiles=kpi_tiles,
        monthly_chart=line_chart_svg(monthly, current_ym),
        monthly_trend_note=monthly_trend_note(monthly, current_ym),
        outcome_chart=stacked_bar_svg(outcome_categories, outcome_by_cat, OUTCOME_ORDER),
        outcome_legend=legend_html(OUTCOME_ORDER),
        outcome_table=table_details(["Outcome", "Count"], [[k, v] for k, v in outcome_rows]),
        channel_chart=stacked_bar_svg(channel_categories, channel_yield, OUTCOME_ORDER),
        channel_legend=legend_html(OUTCOME_ORDER),
        channel_table=table_details(
            ["Channel", "Confirmed", "Discarded", "Open / pending", "Total"],
            [
                [c, channel_yield[c].get("Confirmed", 0), channel_yield[c].get("Discarded", 0), channel_yield[c].get("Open / pending", 0), sum(channel_yield[c].values())]
                for c in channel_categories
            ],
        ),
        timeliness_chart=bar_svg(timeliness_rows, "--series-1", unit=" hrs"),
        verify_complete_count=timeliness["verifyCompleteCount"],
        human_disease_chart=bar_svg(human_top, "--series-1"),
        human_disease_table=table_details(["Disease", "Count"], [[k, v] for k, v in human_top]),
        animal_disease_chart=bar_svg(animal_top, "--series-2"),
        animal_disease_table=table_details(["Disease", "Count"], [[k, v] for k, v in animal_top]),
        window_days=WINDOW_DAYS,
        one_health_table=one_health_html,
        locations_chart=bar_svg([(l, c) for l, c in locations], "--series-1"),
        locations_table=table_details(["Location", "Confirmed signals"], [[l, c] for l, c in locations]),
        negative_lag_flags=timeliness["negativeLagFlags"],
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html)
    print(f"\nSaved dashboard to {OUT_PATH}")
    print(f"  {total} signals - {confirmed} confirmed / {discarded} discarded / {open_pending} open")
    print(f"  {len(one_health)} One Health candidate pair(s)")


if __name__ == "__main__":
    main()
