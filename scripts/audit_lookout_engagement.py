"""
Lookout Engagement Audit - extension of fetch_tracked_entities.py.

Computes the Active Lookout Ratio per District: the % of registered
"Impuruza Person" tracked entities (lookouts) that have submitted at least
one signal event, vs. those registered but never reporting (see the
"most tracked entities have empty events: []" note in docs/API_NOTES.md).

PRIVACY: this script requests the same tracked-entity payload shape as
fetch_tracked_entities.py, which includes attributes[attribute,value] for
ALL attributes - DHIS2's field-filter syntax can't select individual
attributes by ID, only whole nested objects. It deliberately NEVER reads or
persists the "Lookout Names" (Yhh6dcSSvh3) or "Mobile number" (E7u9XdW24SP)
attribute values - only the District org-unit attribute and event
presence/count are extracted below. The output is aggregate counts per
district only; no per-lookout record is ever written to disk. Do not add
per-lookout fields to the report structures here without re-reading this
comment.

Usage:
    python3 scripts/audit_lookout_engagement.py --pages 50 --page-size 100   # quick sample
    python3 scripts/audit_lookout_engagement.py --all                       # full ~48k audit
    python3 scripts/audit_lookout_engagement.py --all --org-unit <uid>       # scope to one province/district org unit
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from dhis2_client import DHIS2Client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "reports"  # aggregate-only output - safe to commit, contains no PII

PROGRAM_ID = "oWvNtR6iP8p"       # Impuruza
NATIONAL_ORG_UNIT = "Hjw70Lodtf2"  # root org unit ("Rwanda")

DISTRICT_ATTR = "yvkYfTjxEJU"  # "District" tracked entity attribute (value = org unit UID)
# Deliberately NOT referencing Yhh6dcSSvh3 (Lookout Names) or E7u9XdW24SP
# (Mobile number) anywhere in this file - see module docstring.

# We still get every attribute back in the response (DHIS2 can't filter
# attributes by ID), but the parser below only ever reads DISTRICT_ATTR out
# of it, and only checks for event *presence* under enrollments, never their
# dataValues.
FIELDS = "trackedEntity,attributes[attribute,value],enrollments[events[event]]"


def resolve_org_unit_names(client: DHIS2Client, org_unit_ids: set[str]) -> dict[str, str]:
    """id -> name, one API call per unique district UID (~30 districts nationwide)."""
    names: dict[str, str] = {}
    for uid in org_unit_ids:
        try:
            names[uid] = client.org_unit(uid, fields="id,name").get("name", uid)
        except Exception:
            names[uid] = uid  # fall back to the raw UID if the lookup fails - still usable, just less readable
    return names


def audit(client: DHIS2Client, org_unit: str, page_size: int, max_pages: int | None) -> dict:
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"registered": 0, "active": 0})
    district_uids_seen: set[str] = set()
    unknown_district = 0
    total = 0

    for te in client.iter_tracked_entities(
        PROGRAM_ID, org_unit, page_size=page_size, max_pages=max_pages, fields=FIELDS
    ):
        total += 1
        district_uid = None
        for a in te.get("attributes", []):
            if a.get("attribute") == DISTRICT_ATTR:
                district_uid = a.get("value")
                break

        has_event = any(enr.get("events") for enr in te.get("enrollments", []))

        key = district_uid or "UNKNOWN"
        if district_uid:
            district_uids_seen.add(district_uid)
        else:
            unknown_district += 1

        stats[key]["registered"] += 1
        if has_event:
            stats[key]["active"] += 1

        if total % 2000 == 0:
            print(f"  ...{total} lookouts audited")

    return {
        "stats": stats,
        "district_uids": district_uids_seen,
        "unknown_district": unknown_district,
        "total": total,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=1, help="Number of pages to audit (ignored if --all)")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--all", action="store_true", help="Audit every registered lookout (~489 pages at size 100)")
    parser.add_argument("--org-unit", default=NATIONAL_ORG_UNIT)
    args = parser.parse_args()

    client = DHIS2Client()
    max_pages = None if args.all else args.pages

    print("Auditing lookouts (attributes[] is fetched but only District + event presence are ever read - see module docstring)...")
    result = audit(client, args.org_unit, args.page_size, max_pages)

    print(f"Resolving {len(result['district_uids'])} district org unit names...")
    names = resolve_org_unit_names(client, result["district_uids"])

    report = {"generated_from_records": result["total"], "districts": []}
    for uid, counts in sorted(result["stats"].items(), key=lambda kv: names.get(kv[0], kv[0])):
        district_name = "Unknown / not captured" if uid == "UNKNOWN" else names.get(uid, uid)
        registered = counts["registered"]
        active = counts["active"]
        report["districts"].append(
            {
                "district": district_name,
                "registered_lookouts": registered,
                "active_lookouts": active,
                "active_lookout_ratio_pct": round(100 * active / registered, 1) if registered else 0.0,
            }
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "lookout_engagement.json"
    out_path.write_text(json.dumps(report, indent=2))

    print(f"\nSaved aggregate-only report ({result['total']} lookouts, {len(report['districts'])} districts) to {out_path}")
    if result["unknown_district"]:
        print(f"  {result['unknown_district']} lookouts had no District attribute captured (bucketed as 'Unknown / not captured')")


if __name__ == "__main__":
    main()
