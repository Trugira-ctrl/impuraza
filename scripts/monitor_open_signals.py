#!/usr/bin/env python3
"""
Monitor for Impuruza signals (events) that have NOT been marked Confirmed or
Discarded yet - i.e. still "open" and awaiting verification.

Intended to run every 2 minutes via an external scheduler (cron/launchd -
see docs/ops/scheduling.md). Each run:

  1. Pulls ALL events for the Impuruza program from /api/tracker/events.
     This is currently cheap (~900 events total, sub-second query - see
     "Scale" note in docs/API_NOTES.md). If the program grows into the tens
     of thousands of events, switch to incremental sync via `updatedAfter`
     + a local state file instead of a full sweep - see NOTE at the bottom
     of this file.
  2. Filters to events whose "Signal Verification Outcome" is neither
     Confirmed nor Discarded ("open").
  3. Flags each open signal "isNew" if it just crossed the staleness
     threshold (--stale-threshold-hours, default 2) during THIS run's
     lookback window - i.e. it was created, has remained unconfirmed and
     undiscarded, and turned e.g. 2 hours old sometime in the last
     --lookback-minutes. This is edge-triggered: a signal is isNew=true in
     exactly one run (the run whose window contains the moment it crossed
     the threshold), then isNew=false in every run after that - so an
     alert fed by this field fires once per signal, not every 2 minutes
     forever. `hoursOpen` (on every open signal, always) is what tells you
     its actual current age regardless of isNew.
  4. Prints a JSON report to stdout and logs a one-line summary.

Org unit scope: defaults to nationwide (all of Rwanda). Set a `Province`
value in .env (or a `PROVINCE` process env var, which takes precedence) to
restrict to one province instead - accepts either the real org unit names
(East/Kigali City/North/South/West) or the CR_Province attribute option set's
different labels (Eastern/Kigali/Northern/Southern/Western), case-insensitive.
"National" (the default) means the whole country. See resolve_org_unit_scope().

Timestamps: DHIS2 returns occurredAt/updatedAt in the server's own local
clock (confirmed: this instance reports Africa/Kigali time, UTC+2 - NOT
UTC). To avoid clock-skew bugs between whatever host runs this cron job and
the DHIS2 server, "now" is taken from the server's own /api/system/info
response each run, not from the local machine clock.

Privacy: each open signal is enriched with the reporting community health
worker's registration details (name, phone number, home province/district/
sector/village) - i.e. this output contains REAL PERSONAL DATA by design.
`data/state/` and its contents are gitignored, and this script chmods the
state directory/file to owner-only (0700/0600) on every write. Do not widen
those permissions, commit the state file, paste raw output into tickets/
chat beyond what's needed, or point any log aggregation at it without the
same access controls you'd apply to the source system.

Exit codes:
  0 = ran successfully (regardless of whether open signals were found)
  1 = configuration error (missing metadata cache or credentials)
  2 = API/network error that persisted after retries
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from dhis2_client import DHIS2Client, load_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
METADATA_DIR = ROOT / "data" / "metadata"
LOG_DIR = ROOT / "data" / "logs"
STATE_DIR = ROOT / "data" / "state"
LATEST_SNAPSHOT_PATH = STATE_DIR / "latest_open_signals.json"

PROGRAM_ID = "oWvNtR6iP8p"                # Impuruza
NATIONAL_ORG_UNIT = "Hjw70Lodtf2"         # Rwanda (root); ouMode=DESCENDANTS walks the whole tree

# Real province-level org unit names differ from the CR_Province *option set* labels used
# elsewhere in this program (a reporter's own registered address) - accept both spellings,
# case-insensitively, for the Province filter so it isn't picky about which convention is used.
PROVINCE_ALIASES = {
    "east": "East", "eastern": "East",
    "kigali city": "Kigali City", "kigali": "Kigali City",
    "north": "North", "northern": "North",
    "south": "South", "southern": "South",
    "west": "West", "western": "West",
}

# Data element IDs (see data/metadata/program.json for the full list)
VERIFICATION_OUTCOME_DE = "bqqqkWc73MG"   # "Signal Verification Outcome" -> Confirmed / Discarded
EBS_TYPE_DE = "OEVkQ1ds77r"               # "Impuruza_EBS Type" -> reporting channel
SIGNAL_TRIGGER_DE = "xm3RTn3tkw1"         # "Impuruza Signal Notified" -> signal code (e.g. C08)
AUTO_EVENT_ID_DE = "te3YoG4XmCO"          # "Auto Event ID"
DISEASE_DE = "BHaSNB8TwzI"                # "Disease eCBS"

# Tracked entity (reporter/"lookout") attribute IDs - this is where the personal data lives
REPORTER_NAME_ATTR = "Yhh6dcSSvh3"        # "Lookout Names"
REPORTER_PHONE_ATTR = "E7u9XdW24SP"       # "Mobile number"
REPORTER_PROVINCE_ATTR = "RS5UJYqho6y"    # "Province" (value is an org unit id)
REPORTER_DISTRICT_ATTR = "yvkYfTjxEJU"    # "District" (value is an org unit id)
REPORTER_SECTOR_ATTR = "iBB5ejHjJbC"      # "Sector (Residence)" (value is an org unit id)
REPORTER_VILLAGE_ATTR = "v24me96F6XA"     # "Village/Address" (free text)

CLOSED_OUTCOMES = {"confirmed", "discarded"}  # normalized (stripped + lowercased) - option codes
# have inconsistent casing/trailing whitespace in this instance (e.g. "Confirmed ")

LOOKBACK_MINUTES_DEFAULT = 3   # meant to run every 2 min; 3-min lookback = 1-min overlap buffer
STALE_THRESHOLD_HOURS_DEFAULT = 2  # age at which an open signal is flagged "isNew" (just went stale)
PAGE_SIZE = 200
BATCH_SIZE = 100               # for comma-separated trackedEntities= / id:in:[] lookups
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

logger = logging.getLogger("open_signals_monitor")


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "open_signals_monitor.log"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def load_option_labels(option_set_name: str) -> dict:
    """code -> label map for a cached option set (empty dict if not cached)."""
    path = METADATA_DIR / "option_sets.json"
    if not path.exists():
        return {}
    option_sets = json.loads(path.read_text())
    return {o["code"]: o["name"] for o in option_sets.get(option_set_name, {}).get("options", [])}


def get_with_retries(client: DHIS2Client, path: str, params: dict) -> dict:
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.get(path, params)
        except requests.exceptions.RequestException as e:
            last_err = e
            logger.warning("GET %s failed (attempt %d/%d): %s", path, attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)  # linear backoff: 2s, 4s
    raise RuntimeError(f"GET {path} failed after {MAX_RETRIES} attempts") from last_err


def server_now(client: DHIS2Client) -> datetime:
    """Authoritative 'now', taken from the DHIS2 server's own clock (not the local host's)."""
    info = get_with_retries(client, "/api/system/info.json", {"fields": "serverDate"})
    return parse_dhis2_datetime(info["serverDate"])


def parse_dhis2_datetime(value: str) -> datetime:
    # DHIS2 timestamps have no timezone suffix; they're already in the server's own local
    # clock, matching occurredAt/updatedAt on events, so we keep everything naive/comparable
    # rather than mixing in an assumed UTC or local-machine offset.
    return datetime.fromisoformat(value)


def resolve_org_unit_scope() -> tuple[str, str]:
    """Determine which org unit subtree to scan, from a `Province` value in .env or a
    `PROVINCE` process env var (which takes precedence, e.g. for launchd EnvironmentVariables
    without touching .env). Returns (org_unit_id, human_readable_scope_label).

    "National" (the default, case-insensitive, also used when unset) scans the whole
    country. Otherwise the value must match one of the 5 real provinces - either their
    actual org unit names (East/Kigali City/North/South/West) or the CR_Province option
    set's different labels (Eastern/Kigali/Northern/Southern/Western) - looked up from the
    cache built by fetch_metadata.py.
    """
    raw = os.environ.get("PROVINCE") or load_env().get("Province") or "National"
    raw = raw.strip()
    if raw.lower() == "national":
        return NATIONAL_ORG_UNIT, "National"

    canonical_name = PROVINCE_ALIASES.get(raw.lower())
    if canonical_name is None:
        valid = "National, " + ", ".join(sorted({v for v in PROVINCE_ALIASES.values()}))
        raise RuntimeError(f"Unrecognized Province/PROVINCE value {raw!r}. Valid options: {valid}")

    provinces_path = METADATA_DIR / "provinces.json"
    if not provinces_path.exists():
        raise RuntimeError("data/metadata/provinces.json missing. Run scripts/fetch_metadata.py first.")
    provinces = json.loads(provinces_path.read_text())
    org_unit_id = provinces.get(canonical_name)
    if not org_unit_id:
        raise RuntimeError(f"Province {canonical_name!r} not found in data/metadata/provinces.json.")
    return org_unit_id, canonical_name

def fetch_all_events(client: DHIS2Client, org_unit: str, occurred_after: datetime | None = None) -> list[dict]:
    """Pull every event in the program under the given org unit subtree (paginated).
    If occurred_after is given, only events occurring on/after that date are fetched
    (filtered server-side by DHIS2, not in Python) — see module docstring re: scale."""
    fields = "event,trackedEntity,orgUnit,status,occurredAt,updatedAt,dataValues[dataElement,value]"
    params = {
        "program": PROGRAM_ID,
        "orgUnit": org_unit,
        "ouMode": "DESCENDANTS",
        "fields": fields,
        "pageSize": PAGE_SIZE,
    }
    if occurred_after is not None:
        params["occurredAfter"] = occurred_after.strftime("%Y-%m-%d")

    events = []
    page = 1
    while True:
        data = get_with_retries(client, "/api/tracker/events", {**params, "page": page})
        batch = data.get("events", [])
        events.extend(batch)
        logger.debug("Fetched page %d: %d events", page, len(batch))
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return events

# def fetch_all_events(client: DHIS2Client, org_unit: str) -> list[dict]:
#     """Pull every event in the program under the given org unit subtree (paginated).
#     See module docstring re: scale."""
#     fields = "event,trackedEntity,orgUnit,status,occurredAt,updatedAt,dataValues[dataElement,value]"
#     events = []
#     page = 1
#     while True:
#         data = get_with_retries(
#             client,
#             "/api/tracker/events",
#             {
#                 "program": PROGRAM_ID,
#                 "orgUnit": org_unit,
#                 "ouMode": "DESCENDANTS",
#                 "fields": fields,
#                 "pageSize": PAGE_SIZE,
#                 "page": page,
#             },
#         )
#         batch = data.get("events", [])
#         events.extend(batch)
#         logger.debug("Fetched page %d: %d events", page, len(batch))
#         if len(batch) < PAGE_SIZE:
#             break
#         page += 1
#     return events


def fetch_reporter_attributes(client: DHIS2Client, tracked_entity_ids: list[str]) -> dict[str, dict]:
    """Batch-fetch registration attributes (name, phone, home location) for a set of
    tracked entities (the community health workers / "lookouts" who reported each open
    signal). Returns {trackedEntity_id: {attribute_id: value}}. Batches requests (rather
    than one call per tracked entity) to keep this cheap even for hundreds of open signals."""
    raw_by_te: dict[str, dict] = {}
    unique_ids = sorted(set(tracked_entity_ids))
    for i in range(0, len(unique_ids), BATCH_SIZE):
        batch = unique_ids[i : i + BATCH_SIZE]
        data = get_with_retries(
            client,
            "/api/tracker/trackedEntities",
            {
                "trackedEntities": ",".join(batch),
                "program": PROGRAM_ID,
                "fields": "trackedEntity,attributes[attribute,value]",
                # This endpoint defaults to pageSize=50 and silently truncates even when
                # given an explicit, longer id list - must set pageSize >= len(batch).
                "pageSize": len(batch),
            },
        )
        for te in data.get("trackedEntities", []):
            raw_by_te[te["trackedEntity"]] = {a["attribute"]: a["value"] for a in te.get("attributes", [])}
    return raw_by_te


def fetch_org_unit_names(client: DHIS2Client, org_unit_ids: set[str]) -> dict[str, str]:
    """Batch-resolve org unit ids -> names. Needed for both the event's own orgUnit and
    the reporter's registered province/district/sector, which DHIS2 stores as org unit
    ids rather than plain text."""
    names: dict[str, str] = {}
    unique_ids = sorted(oid for oid in org_unit_ids if oid)
    for i in range(0, len(unique_ids), BATCH_SIZE):
        batch = unique_ids[i : i + BATCH_SIZE]
        data = get_with_retries(
            client,
            "/api/organisationUnits.json",
            {"filter": f"id:in:[{','.join(batch)}]", "fields": "id,name", "paging": "false"},
        )
        for ou in data.get("organisationUnits", []):
            names[ou["id"]] = ou["name"]
    return names


def enrich_with_reporter_details(open_signals: list[dict], client: DHIS2Client) -> None:
    """Mutates open_signals in place: adds a 'reporter' dict (name, phone, home
    province/district/sector/village) per signal and resolves each signal's orgUnit id
    to a human-readable name. Pulls real personal data - see module docstring Privacy note."""
    tracked_entity_ids = [s["trackedEntity"] for s in open_signals if s.get("trackedEntity")]
    reporter_raw = fetch_reporter_attributes(client, tracked_entity_ids)

    org_unit_ids = {s.get("orgUnit") for s in open_signals if s.get("orgUnit")}
    for raw in reporter_raw.values():
        org_unit_ids.update(
            raw[a]
            for a in (REPORTER_PROVINCE_ATTR, REPORTER_DISTRICT_ATTR, REPORTER_SECTOR_ATTR)
            if raw.get(a)
        )
    org_unit_names = fetch_org_unit_names(client, org_unit_ids)

    for signal in open_signals:
        signal["orgUnitName"] = org_unit_names.get(signal.get("orgUnit"))
        raw = reporter_raw.get(signal.get("trackedEntity"), {})
        signal["reporter"] = {
            "name": raw.get(REPORTER_NAME_ATTR),
            "mobileNumber": raw.get(REPORTER_PHONE_ATTR),
            "province": org_unit_names.get(raw.get(REPORTER_PROVINCE_ATTR)),
            "district": org_unit_names.get(raw.get(REPORTER_DISTRICT_ATTR)),
            "sector": org_unit_names.get(raw.get(REPORTER_SECTOR_ATTR)),
            "villageOrAddress": raw.get(REPORTER_VILLAGE_ATTR),
        }


def analyze(events: list[dict], now: datetime, lookback_minutes: int, stale_threshold_hours: int) -> dict:
    ebs_type_labels = load_option_labels("EBS Type")
    disease_labels = load_option_labels("Disease")
    lookback_cutoff = now - timedelta(minutes=lookback_minutes)
    threshold = timedelta(hours=stale_threshold_hours)

    open_signals = []
    for ev in events:
        dvs = {dv["dataElement"]: dv["value"] for dv in ev.get("dataValues", [])}
        outcome_raw = dvs.get(VERIFICATION_OUTCOME_DE)
        outcome_norm = (outcome_raw or "").strip().lower()
        if outcome_norm in CLOSED_OUTCOMES:
            continue  # confirmed or discarded -> not "open"

        updated_at = parse_dhis2_datetime(ev["updatedAt"])
        occurred_at = parse_dhis2_datetime(ev["occurredAt"]) if ev.get("occurredAt") else updated_at
        hours_open = round((now - occurred_at).total_seconds() / 3600, 1)

        # Edge-triggered: true only in the run whose lookback window contains the exact
        # moment this still-open signal turned `stale_threshold_hours` old. False before
        # that moment (too young to alert on yet) and false after (already alerted on a
        # previous run - hoursOpen still shows it's over threshold, just not "new" anymore).
        stale_crossing_at = occurred_at + threshold
        is_new = lookback_cutoff <= stale_crossing_at <= now

        open_signals.append(
            {
                "event": ev["event"],
                "trackedEntity": ev.get("trackedEntity"),
                "orgUnit": ev.get("orgUnit"),
                "ebsType": ebs_type_labels.get(dvs.get(EBS_TYPE_DE), dvs.get(EBS_TYPE_DE)),
                "signalTrigger": dvs.get(SIGNAL_TRIGGER_DE),
                "autoEventId": dvs.get(AUTO_EVENT_ID_DE),
                "disease": disease_labels.get(dvs.get(DISEASE_DE), dvs.get(DISEASE_DE)),
                "occurredAt": ev.get("occurredAt"),
                "updatedAt": ev.get("updatedAt"),
                "hoursOpen": hours_open,
                "verificationOutcome": outcome_raw,  # None if the field was never set
                "isNew": is_new,
            }
        )

    open_signals.sort(key=lambda s: s["hoursOpen"], reverse=True)
    return {
        "runAt": now.isoformat(),
        "lookbackMinutes": lookback_minutes,
        "staleThresholdHours": stale_threshold_hours,
        "totalEventsScanned": len(events),
        "totalOpen": len(open_signals),
        "newlyOpened": sum(1 for s in open_signals if s["isNew"]),
        "staleOpen": sum(1 for s in open_signals if not s["isNew"]),
        "openSignals": open_signals,
    }

def default_since_cutoff(reference: datetime | None = None) -> datetime:
    """Most recent August 1st on/before the reference date."""
    reference = reference or datetime.now()
    year = reference.year if reference.month >= 8 else reference.year - 1
    return datetime(year, 8, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lookback-minutes", type=int, default=LOOKBACK_MINUTES_DEFAULT)
    parser.add_argument("--stale-threshold-hours", type=int, default=STALE_THRESHOLD_HOURS_DEFAULT)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--since-date",
        type=str,
        default=None,
        help="Only include events on/after this date (YYYY-MM-DD). Defaults to the most recent August 1st.",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    if not (METADATA_DIR / "program.json").exists():
        logger.error("Metadata cache missing. Run scripts/fetch_metadata.py once first.")
        sys.exit(1)

    try:
        org_unit, scope_label = resolve_org_unit_scope()
    except RuntimeError as e:
        logger.error("Config error: %s", e)
        sys.exit(1)

    try:
        client = DHIS2Client()
    except RuntimeError as e:
        logger.error("Credential error: %s", e)
        sys.exit(1)

    if args.since_date:
        try:
            since_cutoff = datetime.strptime(args.since_date, "%Y-%m-%d")
        except ValueError:
            logger.error("Invalid --since-date, expected YYYY-MM-DD")
            sys.exit(1)
    else:
        since_cutoff = default_since_cutoff()

    try:
        now = server_now(client)
        events = fetch_all_events(client, org_unit, occurred_after=since_cutoff)
    except RuntimeError as e:
        logger.error("Giving up: %s", e)
        sys.exit(2)

    logger.info("Fetched %d events occurring on/after %s", len(events), since_cutoff.date())
    report = analyze(events, now, args.lookback_minutes, args.stale_threshold_hours)

    # try:
    #     now = server_now(client)
    #     events = fetch_all_events(client, org_unit)
    # except RuntimeError as e:
    #     logger.error("Giving up: %s", e)
    #     sys.exit(2)

    # report = analyze(events, now, args.lookback_minutes, args.stale_threshold_hours)
    report["scope"] = scope_label
    logger.info(
        "[%s] Scanned %d events: %d open (%d just crossed %dh threshold, %d already stale)",
        scope_label,
        report["totalEventsScanned"],
        report["totalOpen"],
        report["newlyOpened"],
        args.stale_threshold_hours,
        report["staleOpen"],
    )

    try:
        enrich_with_reporter_details(report["openSignals"], client)
    except RuntimeError as e:
        logger.error("Giving up fetching reporter details: %s", e)
        sys.exit(2)

    write_latest_snapshot(report)
    print(json.dumps(report, indent=2))

    try:
        send_report(report)
    except RuntimeError as e:
        logger.error("Giving up sending report: %s", e)
        sys.exit(2)


def write_latest_snapshot(report: dict) -> None:
    """Atomically overwrite the 'current state' file each run (safe for concurrent readers,
    unlike appending to stdout/a log file, which would produce a growing stream of JSON
    blobs rather than one clean current snapshot). Contains real personal data (see module
    docstring Privacy note), so the directory/file are chmod'd owner-only on every write."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    tmp_path = LATEST_SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(report, indent=2))
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(LATEST_SNAPSHOT_PATH)


def send_report(report: dict, timeout: int = 15) -> None:
    """POST the report to the webhook URL configured in .env / the environment."""
    load_dotenv()
    url = os.getenv("INTEGRATION_URL")
    if not url:
        raise RuntimeError("INTEGRATION_URL is not set in the environment/.env file")

    try:
        response = requests.post(url, json=report, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to send report to {url}: {e}") from e

    logger.info("Report sent to %s (status %d)", url, response.status_code)


if __name__ == "__main__":
    main()

# NOTE on scaling past a full sweep:
# At ~900 events a full pull is sub-second and simplest/most correct (nothing
# can silently fall through the cracks). If this grows large enough that a
# full sweep every 2 minutes becomes expensive, switch to:
#   1. Persist {event_id: last_seen_outcome} to a local state file after each run.
#   2. Query only `updatedAfter=<last run's server_now>` each run (still using
#      server_now(), not the local clock, to avoid drift) for *changed* events.
#   3. Merge changes into the persisted state, and still keep a much slower
#      (e.g. hourly) full sweep as a correctness backstop in case any update
#      notification was ever missed.
