# Impuruza signal monitor (DHIS2 → Frappe)

Monitors the **Impuruza** community event-based surveillance (CEBS) program on
`https://cbs2.moh.gov.rw` (Rwanda MoH DHIS2) for signals that have been sitting
**open/pending for more than 2 hours** — i.e. reported by a community lookout but
not yet marked Confirmed or Discarded.

This repo is deliberately scoped to that job. It is the DHIS2-side half of a
planned integration: the monitor identifies stale open signals, and a Frappe
system (to be built) turns them into tickets and keeps them in sync. See
[Planned Frappe integration](#planned-frappe-integration) for the contract
between the two.

See [`docs/API_NOTES.md`](docs/API_NOTES.md) for the full write-up of what this
program actually is — tracked entities are lookouts/reporters, not patients, and
the folder name "NCD" it was originally found under is misleading.

## Setup

```bash
pip install -r requirements.txt
```

Credentials in `.env` (gitignored):

```
Username=your_username
Password=your_password
```

Then build the metadata cache — **required once before the monitor will run**
(it reads option-set labels and province org unit IDs from here):

```bash
python3 scripts/fetch_metadata.py
```

## Running the monitor

```bash
python3 scripts/monitor_open_signals.py                            # nationwide, 2h staleness threshold
python3 scripts/monitor_open_signals.py --stale-threshold-hours 6   # alert at 6h instead
python3 scripts/monitor_open_signals.py --verbose                   # debug logging
PROVINCE=South python3 scripts/monitor_open_signals.py              # scope to one province
```

Each run pulls every event in the program (~870 today, sub-second), filters to
those with no Signal Verification Outcome, and writes a report.

**`isNew` is edge-triggered** — it's true only in the single run whose lookback
window catches the exact moment a still-open signal crosses
`--stale-threshold-hours` (default 2h). False before (too young) and false after
(already fired). That's what makes it safe to create a Frappe ticket off
`isNew: true` without generating a duplicate every 2 minutes for the same
backlog. `hoursOpen` carries the signal's actual current age regardless.

Output:
- `data/state/latest_open_signals.json` — current snapshot, overwritten each run.
  **Contains real personal data** (reporter name/phone/location), chmod 0600.
- `data/logs/open_signals_monitor.log` — one summary line per run, no PII.

Exit codes: `0` ok · `1` config error (bad credentials, missing metadata cache,
unrecognized province) · `2` API/network error after retries.

## Scheduling (every 2 minutes)

The script is single-shot — one invocation, one query, exit. An external
scheduler provides the cadence. A ready-made macOS launchd job (set to 120s) is
at [`scripts/launchd/`](scripts/launchd/); a cron one-liner for Linux and the
install/verify/uninstall steps are in
[`docs/ops/scheduling.md`](docs/ops/scheduling.md).

If you change the interval, change `--lookback-minutes` to match — it must stay
slightly **longer** than the interval so consecutive runs overlap and no signal
slips through the gap between them. Current defaults: 120s interval, 3-minute
lookback.

## Planned Frappe integration

The intended flow, not yet built:

1. Monitor runs every 2 minutes (above).
2. Signals flagged `isNew: true` (open >2h) → **create a ticket in Frappe**.
3. Subsequent runs detect changes to those signals → **update the Frappe ticket**.
4. A signal that gets Confirmed or Discarded → **close the Frappe ticket**.

**The contract.** Each entry in `openSignals[]` carries:

| Field | Use in Frappe |
|---|---|
| `event` | DHIS2 event UID — **use as the ticket's external/idempotency key** so re-runs don't duplicate tickets |
| `isNew` | Create-ticket trigger (fires exactly once per signal) |
| `hoursOpen` | Current age, for SLA/escalation display |
| `updatedAt` | DHIS2-side last-modified, for change detection |
| `occurredAt` | When the signal was reported |
| `ebsType`, `signalTrigger`, `disease`, `orgUnitName` | Ticket body/classification |
| `reporter{name, mobileNumber, province, district, sector, villageOrAddress}` | Who to contact — **PII, see Privacy below** |

**Three gaps to close before this works end-to-end** (none are built yet):

- **The existing backlog will never fire `isNew`.** As of the last run there are
  **752 open signals, and `isNew` is true for zero of them** — the youngest has
  been open 25.9 hours, the oldest 8,123 (~11 months). They all crossed the 2h
  threshold *before* the monitor was watching, so by the edge-triggered
  definition they never "cross" it again. A Frappe sync built only on
  `isNew: true` would create **no tickets at all** and silently ignore the
  entire backlog. It needs a separate one-off backfill (create tickets for
  everything currently in `openSignals[]`), with `isNew` handling only new
  arrivals from then on.
- **Closing tickets.** The monitor only reports what's currently *open*. A
  signal that gets verified simply disappears from `openSignals[]` — there's no
  "these just closed" list. The sync will need to either diff against the
  previous run or have Frappe reconcile its open tickets against the current
  snapshot by `event` ID.
- **Detecting updates.** `latest_open_signals.json` is overwritten each run with
  no history, so there's currently nothing to diff `updatedAt` against. Either
  keep a prior-run copy, or let Frappe compare against the `updatedAt` it last
  stored per ticket.

Nothing in this repo writes to DHIS2 — `src/dhis2_client.py` only implements
`GET`. Note that the `SandTechPheoc` account nonetheless *holds* write and
cascade-delete authorities on the live instance; worth scoping that down to
read-only with the DHIS2 administrators, since this tooling never needs them.

## Layout

```
src/dhis2_client.py               reusable API client (.env auth, pagination, GET only)
scripts/fetch_metadata.py         caches program/option-set/org-unit schema -> data/metadata/
scripts/monitor_open_signals.py   the monitor: open signals >2h, edge-triggered isNew
scripts/launchd/                  macOS launchd job (120s interval)
data/metadata/                    cached schema JSON (committed - no personal data)
data/state/, data/logs/           monitor runtime output (gitignored; state contains PII)
docs/API_NOTES.md                 program structure, option sets, scale, endpoints, gotchas
docs/ops/scheduling.md            how to schedule the monitor every 2 minutes
```

## Privacy

Tracked-entity records — and therefore `data/state/latest_open_signals.json` and
anything the Frappe integration carries downstream — contain **real names, phone
numbers and addresses of community disease-surveillance reporters**. Never commit
`data/state/`, never paste raw records into chat/tickets beyond what's needed, and
apply the same access controls to any Frappe ticket, log aggregator or alerting
integration that you'd apply to the source system. See the Privacy section in
[`docs/API_NOTES.md`](docs/API_NOTES.md).
