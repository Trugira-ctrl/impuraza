# Impuruza Program Indicators

Deploys the "fast-track" native DHIS2 analytics for the eCBS dashboard suite
(signal timeliness cascade, geographic density, channel yield). See
`impuruza_program_indicators.json`.

## Import

```bash
curl -u "$Username:$Password" \
  -H "Content-Type: application/json" \
  -X POST "https://cbs2.moh.gov.rw/api/metadata" \
  -d @impuruza_program_indicators.json
```

One caveat with the payload as committed: the `eCBS Channel Confirmation
Yield (%)` indicator's `indicatorType` is a placeholder
(`<PERCENTAGE_INDICATOR_TYPE_UID>`) - indicator types are instance-specific
metadata, not something this repo's API exploration captured. Resolve the
real UID first and substitute it before importing:

```bash
curl -u "$Username:$Password" \
  "https://cbs2.moh.gov.rw/api/indicatorTypes.json?filter=factor:eq:100&fields=id,name"
```

## Known limitations (do not treat these as bugs to "fix" in the PI itself)

- **Hour lags are day-truncated.** `d2:daysBetween()` casts both operands to
  `DATE`, discarding time-of-day. The 4 timeliness PIs report
  day-granularity x 24, not true sub-day hours. For real hour precision,
  query the raw event table directly (SQL View, or the Python/SQL pipeline
  in `sql/one_health_correlation.sql`'s sibling tables) instead of the
  Program Indicator / analytics engine.
- **"Confirmed"/"Discarded" casing.** This DHIS2 instance has inconsistent
  casing/trailing whitespace on `Signal Verification Outcome` values (e.g.
  `"Confirmed "`), the same issue already flagged in
  `scripts/monitor_open_signals.py`. Program indicator expressions have no
  `trim()`/`lower()`; every PI here filtering on this field will undercount
  until the option values are cleaned at the source (DHIS2 Maintenance app),
  not patched around in expression syntax.
- **Sector geography comes from the event's assigned org unit, not the
  `Impuruza_Sector` data element.** That data element is free text and isn't
  joined to the org-unit hierarchy, so it can't drive a Pivot Table/GIS
  geography dimension. Confirm events are actually registered against the
  org unit where they occurred (not just the reporting facility) before
  trusting sector-level hotspot output.

## Query-time configuration (not baked into the PI expression)

DHIS2 Program Indicators don't encode a rolling window or a GROUP BY - both
are dimension choices made at query/report time:

- **"7-day rolling" density** (item 2): use the relative period
  `LAST_7_DAYS` and the org unit dimension `LEVEL-5` (Sector - see
  `docs/API_NOTES.md` org hierarchy: National(1) > Province(2) > District(3)
  > Sub District(4) > Sector(5) > Facility(6) > Village(7)):

  ```
  GET /api/analytics.json
      ?dimension=dx:eCBSConfCnt
      &dimension=ou:LEVEL-5;Hjw70Lodtf2
      &dimension=pe:LAST_7_DAYS
  ```

- **"By EBS Type / Source" breakdown** (item 3): add the data element as a
  Program Data Element dimension:

  ```
  GET /api/analytics.json
      ?dimension=dx:eCBSChanYld   (once the Indicator's real UID is known)
      &dimension=AXUtIJGHw0H.OEVkQ1ds77r
      &dimension=pe:THIS_YEAR
      &dimension=ou:Hjw70Lodtf2
  ```
