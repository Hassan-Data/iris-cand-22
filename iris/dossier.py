"""Render two-page prospecting dossiers as Markdown.

Content is deterministic: it depends only on the data, never on the run time, so an
unchanged input produces byte-identical files on rerun. Run timestamps live in index.json.
"""
from __future__ import annotations

DISCLAIMER = (
    "Preliminary prospecting material. Figures, eco-point estimates and site suitability are "
    "indicative and based on available source data and commercial screening assumptions. The "
    "8 eco-points/m2 factor is the current commercial baseline, not certified compensation. "
    "Ownership, planning, grid capacity, environmental eligibility and transferability remain "
    "subject to project-specific verification. No permit, reservation or construction readiness "
    "is represented."
)

PAGE_BREAK = '\n<div style="page-break-after: always"></div>\n\n'


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "not available"
    if isinstance(v, int):
        v = f"{v:,}".replace(",", " ")
    return f"{v}{unit}"


def _table(rows: list[tuple[str, str]]) -> str:
    lines = ["| Field | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def _page_two(extra: list[str]) -> str:
    bullets = "\n".join(f"- {e}" for e in extra)
    return (f"## Page 2: Assumptions and limitations\n\n{bullets}\n"
            f"- Geometry CRS: EPSG:25832 (ETRS89 / UTM 32N); areas and distances in metres.\n"
            f"- Missing attributes are shown as \"not available\" and are never estimated.\n\n"
            f"> {DISCLAIMER}\n")


def render_bess(r: dict) -> str:
    head = f"# BESS prospecting dossier: {r['name']}\n\n"
    page1 = "## Page 1: Site summary\n\n" + _table([
        ("Country", r["country_code"]),
        ("Site ID", r["source_id"]),
        ("Parcel area", _fmt(r["area_m2"], " m2")),
        ("Nearest substation (same country)", _fmt(r["nearest_substation_name"])),
        ("Substation voltage", _fmt(r["nearest_substation_kv"], " kV")),
        ("Distance to substation", _fmt(r["substation_distance_m"], " m")),
        ("Screening band", r["screening_band"]),
        ("Screening reason", r["screening_reason"]),
        ("Source", f"{r['source_name']} ({r['source_date']})"),
        ("Data uncertainty", r["uncertainty"]),
    ]) + "\n"
    page2 = _page_two([
        "Distance is straight-line from parcel boundary to substation, not a cable route.",
        "Grid capacity at the substation is not assessed in this screening.",
    ])
    return head + page1 + PAGE_BREAK + page2


def render_peatland(r: dict) -> str:
    head = f"# Peatland prospecting dossier: {r['name']}\n\n"
    eco = (_fmt(r["eco_points_indicative"], " eco-points (indicative)")
           if r["eco_points_per_m2"] is not None
           else f"not available (no eco-point factor configured for {r['country_code']})")
    page1 = "## Page 1: Site summary\n\n" + _table([
        ("Country", r["country_code"]),
        ("Site ID", r["source_id"]),
        ("Area", _fmt(r["area_m2"], " m2")),
        ("Peat depth", _fmt(r["peat_depth_m"], " m")),
        ("Eco-point factor", _fmt(r["eco_points_per_m2"], " eco-points/m2")),
        ("Eco-points", eco),
        ("Screening band", r["screening_band"]),
        ("Screening reason", r["screening_reason"]),
        ("Source", f"{r['source_name']} ({r['source_date']})"),
        ("Data uncertainty", r["uncertainty"]),
    ]) + "\n"
    page2 = _page_two([
        f"Eco-point basis: {r['eco_points_basis'] or 'none configured for this country'}.",
        "Rewetting feasibility and hydrology are not assessed in this screening.",
    ])
    return head + page1 + PAGE_BREAK + page2


RENDERERS = {"bess": render_bess, "peatland": render_peatland}