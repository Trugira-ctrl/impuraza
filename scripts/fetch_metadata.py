"""
Cache the Impuruza program's metadata (program structure, tracked entity
attributes, data elements, option sets, org unit levels) to data/metadata/.

This is *schema* information (no patient/person data), so it's safe to
commit and reuse without re-hitting the API every time.

Usage:
    python3 scripts/fetch_metadata.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from dhis2_client import DHIS2Client  # noqa: E402

PROGRAM_ID = "oWvNtR6iP8p"  # Impuruza
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "metadata"


def main():
    client = DHIS2Client()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching system info...")
    (OUT_DIR / "system_info.json").write_text(json.dumps(client.system_info(), indent=2))

    print(f"Fetching program metadata for {PROGRAM_ID} (Impuruza)...")
    program = client.program_metadata(PROGRAM_ID)
    (OUT_DIR / "program.json").write_text(json.dumps(program, indent=2))

    print("Fetching org unit levels...")
    (OUT_DIR / "org_unit_levels.json").write_text(json.dumps(client.org_unit_levels(), indent=2))

    print("Fetching province org units (for monitor_open_signals.py's Province/PROVINCE filter)...")
    provinces_data = client.get(
        "/api/organisationUnits.json", {"filter": "level:eq:2", "fields": "id,name", "paging": "false"}
    )
    provinces = {ou["name"]: ou["id"] for ou in provinces_data.get("organisationUnits", [])}
    (OUT_DIR / "provinces.json").write_text(json.dumps(provinces, indent=2))

    # Collect every optionSet referenced by the program's attributes/data elements
    option_set_ids = {}
    for a in program.get("programTrackedEntityAttributes", []):
        os_ = a["trackedEntityAttribute"].get("optionSet")
        if os_:
            option_set_ids[os_["id"]] = os_["name"]
    for stage in program.get("programStages", []):
        for d in stage.get("programStageDataElements", []):
            os_ = d["dataElement"].get("optionSet")
            if os_:
                option_set_ids[os_["id"]] = os_["name"]

    print(f"Fetching {len(option_set_ids)} option sets...")
    option_sets = {}
    for os_id, os_name in option_set_ids.items():
        option_sets[os_name] = client.option_set(os_id)
    (OUT_DIR / "option_sets.json").write_text(json.dumps(option_sets, indent=2))

    print(f"\nDone. Metadata cached in {OUT_DIR}/")
    print("  - system_info.json")
    print("  - program.json          (attributes + data elements)")
    print("  - org_unit_levels.json  (National > Province > District > Sub District > Sector > Facilities > Villages)")
    print(f"  - option_sets.json      ({len(option_sets)} option sets: {', '.join(option_sets)})")
    print(f"  - provinces.json        ({len(provinces)} provinces: {', '.join(provinces)})")


if __name__ == "__main__":
    main()
