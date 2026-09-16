"""
Decodes raw DHIS2 data element IDs and option-set codes into human-readable
field names and labels, using the schema cached by fetch_metadata.py.

This logic used to live only in the (now-removed) fetch_tracked_entities.py
explorer script. Re-added here as a shared module since api/main.py needs it
too - if another script needs the same decoding, import from here rather than
re-implementing it a third time.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
METADATA_DIR = REPO_ROOT / "data" / "metadata"


def build_program_names(metadata_dir: Path = METADATA_DIR) -> dict[str, str]:
    """Returns {program UID: name, programStage UID: name} - both in one dict since
    UIDs are globally unique, so there's no collision risk merging them. Cached locally
    (data/metadata/program.json), so resolving these costs no extra API call, unlike
    org unit names below."""
    program = json.loads((metadata_dir / "program.json").read_text())
    names = {program["id"]: program["name"]}
    for stage in program.get("programStages", []):
        names[stage["id"]] = stage["name"]
    return names


def build_field_maps(metadata_dir: Path = METADATA_DIR) -> tuple[dict, dict, dict]:
    """Returns (field_names, field_option_sets, option_code_labels):
    - field_names: data element/attribute ID -> human-readable name
    - field_option_sets: data element/attribute ID -> its option set's name (if any)
    - option_code_labels: option set name -> {code: label}
    """
    program = json.loads((metadata_dir / "program.json").read_text())
    option_sets = json.loads((metadata_dir / "option_sets.json").read_text())

    field_names: dict[str, str] = {}
    field_option_sets: dict[str, str] = {}

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

    option_code_labels = {
        name: {o["code"]: o["name"] for o in os_.get("options", [])} for name, os_ in option_sets.items()
    }
    return field_names, field_option_sets, option_code_labels


def decode_value(field_id: str, raw_value, field_names: dict, field_option_sets: dict, option_code_labels: dict):
    """Returns (label, decoded_value) for one data element/attribute ID + raw value."""
    label = field_names.get(field_id, field_id)
    os_name = field_option_sets.get(field_id)
    if os_name:
        decoded = option_code_labels.get(os_name, {}).get(raw_value, raw_value)
        return label, decoded
    return label, raw_value


def decode_event(
    event: dict,
    field_names: dict,
    field_option_sets: dict,
    option_code_labels: dict,
    program_names: dict | None = None,
) -> dict:
    """Flattens one /api/tracker/events/{id} response into a single dict: event-level
    metadata (event UID, org unit, status, timestamps) plus every dataValue decoded to
    {human-readable field name: decoded value}. All keys the event actually carries end
    up as top-level keys in the returned dict - a field with no value on this particular
    event simply doesn't appear (DHIS2 omits empty dataValues rather than sending nulls).

    program/programStage are resolved to names via program_names (falls back to the raw
    UID if the map is missing or doesn't cover it, rather than raising). event and
    trackedEntity are deliberately left as UIDs - `event` is the record's own primary
    key, and trackedEntity's human-readable name lives on PII this decoder never fetches
    (see api/main.py's Privacy note). orgUnit is left as a UID too - resolving it to a
    name needs a live API call this pure metadata-cache function can't make; see
    api/main.py's get_event(), which adds an `orgUnitName` key after calling this."""
    program_names = program_names or {}
    out = {
        "event": event.get("event"),
        "program": program_names.get(event.get("program"), event.get("program")),
        "programStage": program_names.get(event.get("programStage"), event.get("programStage")),
        "trackedEntity": event.get("trackedEntity"),
        "orgUnit": event.get("orgUnit"),
        "status": event.get("status"),
        "occurredAt": event.get("occurredAt"),
        "updatedAt": event.get("updatedAt"),
    }
    for dv in event.get("dataValues", []):
        label, val = decode_value(dv["dataElement"], dv["value"], field_names, field_option_sets, option_code_labels)
        out[label] = val
    return out
