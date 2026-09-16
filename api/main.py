"""
FastAPI service: given an Impuruza event ID, returns that event fully decoded
as JSON - data element IDs resolved to human-readable field names, option
codes resolved to labels (src/decode.py, the same decode logic other tools in
this repo use). Fetches a single event directly (DHIS2Client.get_event()),
not the full-program sweep monitor_open_signals.py does.

LOCALHOST ONLY - enforced two ways, keep both:
  1. Run uvicorn bound to 127.0.0.1, never 0.0.0.0 (see Usage below).
  2. The middleware below rejects any request whose client IP isn't a loopback
     address, regardless of how the process was started, so a future "just for
     testing" `--host 0.0.0.0` doesn't silently open this to the network.
  Caveat: the middleware trusts request.client.host, i.e. the ASGI server's
  view of the TCP peer. That's correct run standalone as instructed above, but
  if this is ever put behind a reverse proxy, request.client.host becomes the
  proxy's own address (which IS loopback) and this check stops meaning
  anything - you'd need real trusted-proxy / X-Forwarded-For handling instead
  of this check. Don't put a proxy in front of this without revisiting it.

Scope: only serves events belonging to the Impuruza program (PROGRAM_ID) -
this is not a generic DHIS2 event proxy. An event ID from a different program
on the same DHIS2 instance returns 404, same as an ID that doesn't exist.

Privacy: an event's own dataValues never include the reporting lookout's
name/phone/address - that PII lives on the tracked entity's attributes, which
this endpoint never fetches. If a future change adds tracked-entity lookups
here, revisit this note and docs/API_NOTES.md's Privacy section.

Usage:
    pip install -r requirements.txt
    python3 scripts/fetch_metadata.py          # once, if data/metadata/ isn't already cached
    uvicorn api.main:app --host 127.0.0.1 --port 8000

    curl http://127.0.0.1:8000/events/<event_id>
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import requests as requests_lib
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from decode import build_field_maps, build_program_names, decode_event  # noqa: E402
from dhis2_client import DHIS2Client  # noqa: E402

PROGRAM_ID = "oWvNtR6iP8p"  # Impuruza - see docs/API_NOTES.md
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}

_client: DHIS2Client | None = None
_field_maps: tuple[dict, dict, dict] | None = None
_program_names: dict | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the DHIS2 client and metadata decoder maps once at startup, not per
    request - matches how every other script in this repo uses DHIS2Client (one
    instance, reused), and avoids re-reading data/metadata/*.json on every hit.
    A missing/bad .env or missing metadata cache fails startup outright, rather
    than accepting requests this service could never actually serve."""
    global _client, _field_maps, _program_names
    _client = DHIS2Client()
    _field_maps = build_field_maps()
    _program_names = build_program_names()
    yield


app = FastAPI(
    title="Impuruza Event Lookup",
    description="Localhost-only lookup of one Impuruza event by ID. See module docstring.",
    lifespan=lifespan,
)


@app.middleware("http")
async def restrict_to_localhost(request: Request, call_next):
    client_host = request.client.host if request.client else None
    if client_host not in LOOPBACK_HOSTS:
        return JSONResponse(status_code=403, content={"detail": "This service only accepts requests from localhost."})
    return await call_next(request)


@app.get("/events/{event_id}")
def get_event(event_id: str) -> dict:
    """Fetch one Impuruza event by ID and return it fully decoded."""
    try:
        event = _client.get_event(event_id)
    except requests_lib.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        # DHIS2 returns 404 for a well-formed but nonexistent event ID, and 400 for a
        # malformed one (wrong length/charset for a UID) - callers of this endpoint
        # shouldn't need to know DHIS2's UID validation rules to get a sensible answer,
        # so both read as "not found" here rather than exposing the 400 as a server error.
        if status in (400, 404):
            raise HTTPException(status_code=404, detail=f"No event found with ID {event_id!r}") from e
        raise HTTPException(status_code=502, detail=f"DHIS2 request failed: {e}") from e
    except requests_lib.exceptions.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Could not reach DHIS2: {e}") from e

    if event.get("program") != PROGRAM_ID:
        # Don't distinguish "wrong program" from "doesn't exist" - this endpoint has no
        # business confirming that other programs' event IDs on this instance are valid.
        raise HTTPException(status_code=404, detail=f"No event found with ID {event_id!r}")

    field_names, field_option_sets, option_code_labels = _field_maps
    decoded = decode_event(event, field_names, field_option_sets, option_code_labels, _program_names)

    # orgUnit name needs a live lookup (not in the local metadata cache) - if it fails,
    # degrade to just the UID rather than failing the whole request over a display detail.
    org_unit_id = decoded.get("orgUnit")
    if org_unit_id:
        try:
            decoded["orgUnitName"] = _client.org_unit(org_unit_id, fields="id,name").get("name")
        except requests_lib.exceptions.RequestException:
            decoded["orgUnitName"] = None

    return decoded
