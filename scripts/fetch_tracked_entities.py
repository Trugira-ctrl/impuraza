"""
Pull a batch of tracked entities from the Impuruza program and decode the
attribute/dataElement/option-code IDs into human-readable labels using the
metadata cached by fetch_metadata.py.

Output goes to data/samples/ which is gitignored, because tracked entities
contain personal data (names, phone numbers, village of residence).

Usage:
    python3 scripts/fetch_metadata.py        # run once first, to build the decoder maps
    python3 scripts/fetch_tracked_entities.py --pages 1 --page-size 50
    python3 scripts/fetch_tracked_entities.py --all         # WARNING: ~489 pages at pageSize=100 (48k+ records)
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from dhis2_client import DHIS2Client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
METADATA_DIR = ROOT / "data" / "metadata"
OUT_DIR = ROOT / "data" / "samples"

PROGRAM_ID = "oWvNtR6iP8p"          # Impuruza
RWANDA_ORG_UNIT = "Hjw70Lodtf2"     # root org unit ("Rwanda"), orgUnitMode=DESCENDANTS walks the whole tree


def build_decoder_maps():
    """Build {attribute/dataElement id -> name} and {optionSet name -> {code: label}} from cached metadata."""
    program = json.loads((METADATA_DIR / "program.json").read_text())
    option_sets = json.loads((METADATA_DIR / "option_sets.json").read_text())

    field_names = {}       # id -> human name
    field_option_sets = {} # id -> optionSet name

    for a in program.get("programTrackedEntityAttributes", []):
        attr = a["trackedEntityAttribute"]
        field_names[attr["id"]] = attr["name"]
        if attr.get("optionSet"):
            field_option_sets[attr["id"]] = attr["optionSet"]["name"]

    for stage in program.get("programStages", []):
        for d in stage.get("programStageDataElements", []):
            de = d["dataElement"]
            field_names[de["id"]] = de["name"]
            if de.get("optionSet"):
                field_option_sets[de["id"]] = de["optionSet"]["name"]

    option_code_labels = {}  # optionSet name -> {code: label}
    for name, os_ in option_sets.items():
        option_code_labels[name] = {o["code"]: o["name"] for o in os_.get("options", [])}

    return field_names, field_option_sets, option_code_labels


def decode_value(field_id, raw_value, field_names, field_option_sets, option_code_labels):
    label = field_names.get(field_id, field_id)
    os_name = field_option_sets.get(field_id)
    if os_name:
        decoded = option_code_labels.get(os_name, {}).get(raw_value, raw_value)
        return label, decoded
    return label, raw_value


def decode_entity(te, field_names, field_option_sets, option_code_labels):
    out = {"trackedEntity": te["trackedEntity"], "orgUnit": te["orgUnit"], "attributes": {}, "enrollments": []}
    for a in te.get("attributes", []):
        label, val = decode_value(a["attribute"], a["value"], field_names, field_option_sets, option_code_labels)
        out["attributes"][label] = val
    for enr in te.get("enrollments", []):
        enr_out = {"enrollment": enr["enrollment"], "status": enr.get("status"), "events": []}
        for ev in enr.get("events", []):
            ev_out = {"event": ev["event"], "occurredAt": ev.get("occurredAt"), "dataValues": {}}
            for dv in ev.get("dataValues", []):
                label, val = decode_value(dv["dataElement"], dv["value"], field_names, field_option_sets, option_code_labels)
                ev_out["dataValues"][label] = val
            enr_out["events"].append(ev_out)
        out["enrollments"].append(enr_out)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=1, help="Number of pages to fetch (ignored if --all)")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--all", action="store_true", help="Fetch every page (48k+ records, ~489 pages at size 100)")
    parser.add_argument("--org-unit", default=RWANDA_ORG_UNIT)
    parser.add_argument("--raw", action="store_true", help="Save raw (non-decoded) API response instead")
    args = parser.parse_args()

    if not (METADATA_DIR / "program.json").exists():
        sys.exit("Run scripts/fetch_metadata.py first to build the decoder maps.")

    field_names, field_option_sets, option_code_labels = build_decoder_maps()
    client = DHIS2Client()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    max_pages = None if args.all else args.pages
    decoded_records = []
    raw_records = []
    for i, te in enumerate(
        client.iter_tracked_entities(PROGRAM_ID, args.org_unit, page_size=args.page_size, max_pages=max_pages)
    ):
        raw_records.append(te)
        decoded_records.append(decode_entity(te, field_names, field_option_sets, option_code_labels))
        if (i + 1) % 100 == 0:
            print(f"  ...{i + 1} records fetched")

    suffix = "raw" if args.raw else "decoded"
    out_path = OUT_DIR / f"tracked_entities.{suffix}.json"
    out_path.write_text(json.dumps(raw_records if args.raw else decoded_records, indent=2))
    print(f"\nSaved {len(decoded_records)} records to {out_path}")
    print("(this file is gitignored - contains personal data: names, phone numbers, addresses)")


if __name__ == "__main__":
    main()
