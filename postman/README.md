# Postman collection for cbs2.moh.gov.rw

Mirrors everything explored in this project (see [`../docs/API_NOTES.md`](../docs/API_NOTES.md))
as manual, runnable requests, organized into 4 folders:

1. **Auth & System** - confirm login works, check DHIS2 version/clock
2. **Program Metadata** - Impuruza program schema, option sets, org unit levels
3. **Tracker Data** - the original `trackedEntities` endpoint, plus `tracker/events`
   (what `monitor_open_signals.py` actually uses) and the batch reporter-lookup endpoint
4. **Signal Routing Investigation** - the requests that answered "can we see which CEHO
   was supposed to receive the signal?" (short answer: no such field/route exists in
   this program - see the folder's request descriptions for the evidence trail)

## Setup

1. Import both files into Postman:
   - `Impuruza_DHIS2.postman_collection.json`
   - `Impuruza.postman_environment.json`
2. Select the **"Impuruza (cbs2.moh.gov.rw)"** environment (top-right environment picker)
3. Open the environment and fill in `username`/`password` with the same credentials
   from the project's `.env` file. `password` is set to Postman's "secret" type so it
   stays masked in the UI - **do not** change it back to "default"/plaintext.
4. Run any request. Auth is inherited from the collection level (Basic Auth using
   `{{username}}`/`{{password}}`), so you don't need to configure it per-request.

## Notes

- The environment file you fill in locally contains real credentials and is
  **gitignored** - the copy in this repo stays a blank template. If you re-export
  from Postman, it'll overwrite the local file (still gitignored, still fine) - just
  don't force-add it to git.
- A few requests need variables filled in before they're useful (see each request's
  description): `trackedEntityIdsCsv`, `orgUnitIdsCsv`, `updatedAfter`. Defaults for
  the rest (`programId`, `orgUnitId`, `programStageId`, etc.) are pre-filled from
  what we already discovered.
- Two real gotchas are called out directly in request descriptions because they're
  easy to hit and silent when wrong:
  - `tracker/trackedEntities?trackedEntities=...` defaults to `pageSize=50` and
    **silently truncates** longer id lists - always set `pageSize` explicitly.
  - `system/info.json`'s `serverDate` is in **Rwanda local time (UTC+2), not UTC**.
