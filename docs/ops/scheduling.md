# Running `monitor_open_signals.py` every 2 minutes

The script is single-shot: one invocation queries the API once and exits.
An external scheduler is responsible for the "every 2 minutes" part. Two
options, for macOS (this machine) and Linux respectively.

## macOS: launchd (recommended on this Mac)

A ready-made job definition is at
[`scripts/launchd/com.sandtech.impuruza-signal-monitor.plist`](../../scripts/launchd/com.sandtech.impuruza-signal-monitor.plist).

**Install:**

```bash
cp scripts/launchd/com.sandtech.impuruza-signal-monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sandtech.impuruza-signal-monitor.plist
```

**Verify it's loaded and check status/last exit code:**

```bash
launchctl list | grep impuruza-signal-monitor
```

**Watch it run (first run happens up to 2 min after loading; `RunAtLoad` is
deliberately `false` so it doesn't fire immediately on every login):**

```bash
tail -f data/logs/open_signals_monitor.log
```

**Stop/uninstall:**

```bash
launchctl unload ~/Library/LaunchAgents/com.sandtech.impuruza-signal-monitor.plist
rm ~/Library/LaunchAgents/com.sandtech.impuruza-signal-monitor.plist
```

**If you move the project directory or change Python interpreters**, update
the two absolute paths in the `.plist` (`ProgramArguments` and
`WorkingDirectory`/log paths) and reload.

## Linux/server alternative: cron

```cron
*/2 * * * * cd /path/to/Impuruza && /usr/bin/python3 scripts/monitor_open_signals.py >> data/logs/cron.stdout.log 2>&1
```

## What to actually monitor

- **`data/state/latest_open_signals.json`** — always the most recent run's
  full report, atomically overwritten each run. **Contains real personal
  data** (each open signal is enriched with the reporting community health
  worker's name/phone/home location) - the file and its directory are
  chmod'd owner-only (0600/0700) on every write; don't widen that, and point
  any dashboard/alerting integration at it with the same access controls
  you'd apply to the source system.
- **`data/logs/open_signals_monitor.log`** — one summary line per run
  (`[scope] Scanned N events: M open (X just crossed Yh threshold, Z already
  stale)`), for a quick operational history and for catching failures
  (`ERROR` lines: bad credentials, network issues, missing metadata cache).
  No personal data in this file - safe to tail/aggregate freely.
- Exit code: `0` success, `1` config error (bad credentials, missing
  metadata cache, or an unrecognized `Province`/`PROVINCE` value - fix it
  and it'll resolve on the next scheduled run), `2` API/network error after
  retries (transient - the next run 2 minutes later will likely recover on
  its own; alert only if this persists across several consecutive runs).

## Alert-threshold and scope options

- `--stale-threshold-hours` (default 2): the age at which a still-open
  signal gets `isNew: true` in the report - see the module docstring in
  `monitor_open_signals.py` for the exact edge-triggered semantics (fires
  once per signal, not every run).
- `Province` in `.env` / `PROVINCE` env var (default `National`): restricts
  the scan to one province instead of the whole country. Useful if you want
  separate cron jobs per province with different on-call routing, or just
  to cut noise while testing.

## Next steps worth considering

- **Alerting**: right now this only writes files/logs; nothing pages anyone.
  Wiring a Slack/Teams/email ping off `isNew: true` entries is the natural
  next step now that `isNew` is edge-triggered (see above) rather than
  something that would re-fire every 2 minutes for the same backlog.
