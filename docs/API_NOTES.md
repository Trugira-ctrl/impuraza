# cbs2.moh.gov.rw — API Exploration Notes

Findings from exploring the DHIS2 Tracker API on `https://cbs2.moh.gov.rw`
using the `SandTechPheoc` account.

## System

| | |
|---|---|
| DHIS2 version | 2.41.8 (revision `aa461ab`) |
| Auth | HTTP Basic (`Username`/`Password` in `.env`) |
| Account org unit scope | `Hjw70Lodtf2` = **Rwanda** (root org unit) |
| Account name | SandTech PHEOC (Public Health Emergency Operations Center) |

## What program `oWvNtR6iP8p` actually is

It's **not** an NCD (non-communicable disease) program despite this
project's folder name. It's **"Impuruza"** ("alarm/alert" in Kinyarwanda) —
Rwanda's **Community Event-Based Surveillance (CEBS)** program: a
signal/outbreak-reporting system for detecting and triaging potential
public health events (disease outbreaks, animal health events, environmental
signals) reported from community lookouts, health facilities, hotlines,
media scanning, points of entry, mining sites, schools, etc.

- Program type: `WITH_REGISTRATION` (has tracked entities + enrollments + events)
- Tracked entity type: **"Impuruza Person"** (`ZKs80w0rj8p`) — i.e. each
  tracked entity is a *lookout/reporter*, not a patient.
- One program stage: **"Impuruza CEBS"** (`AXUtIJGHw0H`) with 43 data elements
  — each event = one reported signal.

## Scale

Querying `program=oWvNtR6iP8p&orgUnits=Hjw70Lodtf2&orgUnitMode=DESCENDANTS`:

- **48,826** tracked entities (lookouts) nationwide
- pageCount = 16,276 at `pageSize=3` → **~489 pages** at `pageSize=100`
- Org unit hierarchy (levels, national → local):
  `National (1) > Province (2) > District (3) > Sub District (4) > Sector (5) > Facilities (6) > Villages (7)`

## Tracked Entity Attributes (registration-level, on the "Impuruza Person")

| id | name | valueType | mandatory | optionSet |
|---|---|---|---|---|
| `Yhh6dcSSvh3` | Lookout Names | TEXT | yes | |
| `E7u9XdW24SP` | Mobile number | PHONE_NUMBER | yes | |
| `RS5UJYqho6y` | Province | ORGANISATION_UNIT | no | |
| `yvkYfTjxEJU` | District | ORGANISATION_UNIT | no | |
| `iBB5ejHjJbC` | Sector (Residence) | ORGANISATION_UNIT | no | |
| `v24me96F6XA` | Village/Address | TEXT | no | |

⚠️ **This is personal data** (name + phone number of the community reporter).
See [Privacy / PII](#privacy--pii) below.

## Program Stage Data Elements ("Impuruza CEBS" — one per reported signal/event)

43 data elements total; full list with IDs and value types is in
[`data/metadata/program.json`](../data/metadata/program.json) after running
`fetch_metadata.py`. Highlights:

- **Signal intake**: `Impuruza_EBS Type` (channel: Community / Health Facility
  / Hotline / Laboratory / Media Scanning / Schools / PoE / Mining Sites /
  Pharmacy / Environmental Facility / Animal Health), `Impuruza_Source of Information`,
  `Impuruza Signal Notified` (a trigger code, e.g. `C16`, `PH01`, `HL12` — 103
  codes total, matching Rwanda's signal-list taxonomy).
- **What/where/when**: `Impuruza_Signal Description`, `Impuruza_Describe the event`,
  `Impuruza_Province/District/Sector/Cell/Village`, `Impuruza_Country`,
  `Impuruza_When the event started?`, `Impuruza_when the event was detected?`.
- **Disease/animal-health classification**: `Disease eCBS` (44 human diseases,
  e.g. Cholera, Anthrax, Chikungunya, Dengue), `Impuruza_Animal Disease` (54
  animal diseases via "OH AH CC List Of Diseases" — this is a One Health /
  Animal Health cross-cutting list), `Impuruza_Type of Species` (20 animal
  species).
- **Verification workflow**: `Signal Verification Outcome` (Confirmed /
  Discarded), `Impuruza_Does the information meet a pre-defined signal?`,
  `Impuruza_Is the signal a duplicate?`, `Impuruza_Is the signal a rumor?`,
  `Impuruza_Date of signal verification`, `Impuruza_Actual Date of signal
  verification`, `Duplication Status`, `Triage`, `CEB Status` (Kinyarwanda
  status labels: Indwara ikekwa / Indwara yemejwe / Impuruza itariyo / etc.).
- **Impact counts**: `# CEB Cases`, `# Malaria Cases`, `Impuruza_Number of
  Deaths`, `Impuruza_Number of affected People`, `Impuruza_Number of affected
  Animals`.
- **Response**: `Impuruza_What action has been taken?`, `Impuruza_Immediate
  Action`, `Risk of stock out` / `Stock out` (Yego/Oya — Kinyarwanda yes/no).
- Housekeeping: `Auto Event ID`, `Last Date Updated`, `Duration (Hrs)`.

## Option sets (decoded to `data/metadata/option_sets.json`)

| Option set | # options | Example codes → labels |
|---|---|---|
| CEB Status | 5 | Kinyarwanda status phrases |
| Gender | 2 | Male / Female |
| Disease | 44 | Cholera, Anthrax, Dengue, ... |
| Risk of stock out / Stock out | 2 each | `1`→Yego, `0`→Oya |
| Signal Verification | 2 | Confirmed / Discarded |
| CR_Province | 5 | `10`→KIGALI, `20`→SOUTHERN, `30`→WESTERN, `40`→NORTHERN, `50`→EASTERN |
| OH AH ATT Species | 20 | Cattle, Goat, Dog, Chicken, ... |
| EBS Type | 11 | Community, Hotline, Laboratory, Mining Sites, ... |
| YesNo | 2 | Yes / No |
| countries | 246 | ISO3 code → country name |
| OH AH CC List Of Diseases | 54 | Animal health disease list |
| Signal Trigger | 103 | Numeric/alpha signal codes (`C01`…`C16`, `HL##`, `PH01`, ...) — labels equal the codes themselves in this instance |

## Example decoded record

```json
{
  "trackedEntity": "Aj0r7yiVZEp",
  "orgUnit": "ngQOef6XCCa",
  "attributes": {
    "Mobile number": "250782207399",
    "Lookout Names": "Anastase Ntwali"
  },
  "enrollments": [
    {
      "enrollment": "VLLqpkcBiHu",
      "status": "ACTIVE",
      "events": [
        {
          "event": "Kctxxgbq6lS",
          "occurredAt": "2026-07-17T13:09:35.115",
          "dataValues": {
            "Impuruza_EBS Type": "Mining Sites",
            "Auto Event ID": "MS0226575113",
            "Impuruza Signal Notified": "MS02",
            "Last Date Updated": "2026-07-17"
          }
        }
      ]
    }
  ]
}
```

Note most tracked entities have **empty `events: []`** — i.e. the person is
registered as a lookout but hasn't (yet) reported a signal event. Only a
minority of records in the sampled pages had populated events.

## Endpoints exercised

| Endpoint | Purpose |
|---|---|
| `GET /api/me.json` | confirm auth + user's org unit scope |
| `GET /api/system/info.json` | DHIS2 version |
| `GET /api/programs/{id}.json` | program schema: tracked entity type, attributes, stages, data elements |
| `GET /api/optionSets/{id}.json` | option set code→label maps |
| `GET /api/organisationUnitLevels.json` | org unit hierarchy depth/names |
| `GET /api/41/tracker/trackedEntities` | the actual tracker data (paged) |

Relevant tracker query params (per the [DHIS2 tracker docs](https://docs.dhis2.org/en/develop/using-the-api/dhis-core-version-master/tracker.html)):
`program`, `orgUnits`, `orgUnitMode` (`DESCENDANTS` walks the whole subtree
under the given org unit), `page`, `pageSize`, `totalPages=true` (adds
`total`/`pageCount` to the response), `fields` (DHIS2 field-selector syntax
for nested expansion, e.g. `attributes[attribute,value]`).

## Privacy / PII

The `trackedEntities` payload contains **real people's names and phone
numbers** (community lookout reporters) plus village-level location.
Treat this as sensitive personal data:

- Raw/decoded pulls are written to `data/samples/`, which is **gitignored**.
- Metadata (schema, option sets) has no personal data and is safe to commit.
- Don't paste raw tracked-entity records into chat, tickets, or docs beyond
  what's needed to illustrate structure (as done above with one already-public-looking example).
