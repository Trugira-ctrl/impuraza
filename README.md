# cbs2.moh.gov.rw DHIS2 Tracker Explorer

A small reusable toolkit for exploring the DHIS2 Tracker API at
`https://cbs2.moh.gov.rw` (Rwanda MoH). See [`docs/API_NOTES.md`](docs/API_NOTES.md)
for what was found: this is the **"Impuruza"** community event-based
surveillance (CEBS) program, not an NCD program — it tracks lookout/reporter
registrations and the outbreak/health-event signals they report.

## Setup

```bash
pip install -r requirements.txt
```

Put credentials in `.env` (already gitignored):

```
Username=your_username
Password=your_password
```

## Usage

```bash
# 1. Cache program schema, option sets, org unit levels (safe to commit — no personal data)
python3 scripts/fetch_metadata.py

# 2. Pull tracked entities and decode IDs -> human labels (writes to data/samples/, gitignored - contains PII)
python3 scripts/fetch_tracked_entities.py --pages 1 --page-size 50

# Fetch everything (48k+ records, ~489 pages at page-size 100) - slow, be deliberate
python3 scripts/fetch_tracked_entities.py --all
```

Or use the client directly:

```python
from src.dhis2_client import DHIS2Client

client = DHIS2Client()
print(client.system_info())

for te in client.iter_tracked_entities(
    program="oWvNtR6iP8p", org_units="Hjw70Lodtf2", max_pages=1
):
    print(te)
```

## Monitoring open (unconfirmed) signals

`scripts/monitor_open_signals.py` checks every event in the program and
reports which ones have no "Signal Verification Outcome" yet (neither
Confirmed nor Discarded). It's meant to run every 5 minutes via an external
scheduler - see [`docs/ops/scheduling.md`](docs/ops/scheduling.md) for the
ready-made macOS launchd job (and a cron one-liner for Linux).

```bash
python3 scripts/monitor_open_signals.py                            # nationwide, default 2h staleness threshold
python3 scripts/monitor_open_signals.py --stale-threshold-hours 6   # alert at 6h old instead of 2h
PROVINCE=South python3 scripts/monitor_open_signals.py              # scope to one province only
```

Each open signal carries `hoursOpen` (its current age) and `isNew` -
`isNew` is **edge-triggered**: true only in the single run whose 5-minute
cycle catches the exact moment a still-open signal crosses
`--stale-threshold-hours` (default 2h). It's false both before that (too
young to alert on) and after (already alerted on an earlier run) - so
wiring an alert off `isNew` fires once per signal, not every 5 minutes
forever for the same backlog.

Org unit scope defaults to nationwide. Set `Province=<name>` in `.env` (or a
`PROVINCE` process env var, which takes precedence) to restrict to one
province - accepts either real org unit names (East/Kigali City/North/South/
West) or the CR_Province-style labels (Eastern/Kigali/Northern/Southern/
Western), case-insensitive. Requires `provinces.json` from
`fetch_metadata.py` to already be cached.

Output: `data/state/latest_open_signals.json` (always the current snapshot)
and a one-line-per-run summary in `data/logs/open_signals_monitor.log`.

## Trying it in Postman

If you'd rather explore/test the API by hand than via the Python scripts, see
[`postman/README.md`](postman/README.md) - a ready-made collection covering
auth, program metadata, tracker data, and the signal-routing investigation,
plus notes on two real gotchas (silent pagination truncation, server
timezone) found while building this project.

## Layout

```
src/dhis2_client.py             reusable API client (.env-based auth)
scripts/fetch_metadata.py       caches program/option-set/org-unit schema -> data/metadata/
scripts/fetch_tracked_entities.py   pulls + decodes tracker records -> data/samples/ (gitignored)
scripts/monitor_open_signals.py     every-5-min check for unconfirmed/undiscarded signals
scripts/launchd/                 macOS launchd job definition for the monitor
data/metadata/                  cached schema JSON (safe to commit)
data/samples/                   raw/decoded tracked-entity pulls (gitignored - contains PII)
data/state/, data/logs/         monitor's runtime output (gitignored)
docs/API_NOTES.md               full write-up of the program structure, option sets, scale, endpoints
docs/ops/scheduling.md          how to schedule the monitor every 5 minutes
```

## Privacy

Tracked-entity records contain real names, phone numbers, and addresses of
community disease-surveillance reporters. Never commit `data/samples/` or
paste raw records into chat/tickets — see the Privacy section in
[`docs/API_NOTES.md`](docs/API_NOTES.md).
