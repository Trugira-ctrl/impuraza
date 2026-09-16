"""
FastAPI service: given an Impuruza event ID, returns that event fully decoded
as JSON - data element IDs resolved to human-readable field names, option
codes resolved to labels (src/decode.py, the same decode logic other tools in
this repo use). Fetches a single event directly (DHIS2Client.get_event()),
not the full-program sweep monitor_open_signals.py does.

ACCESS CONTROL - defense in depth, keep all layers:
  1. Run uvicorn bound to a specific interface (127.0.0.1 for strictly local,
     or a single Docker bridge gateway IP such as 172.18.0.1 if it needs to be
     reachable from sibling containers on that bridge network). Never bind
     0.0.0.0 - that exposes it on every interface the host has, including any
     public one.
  2. The middleware below independently rejects any request whose client IP
     isn't in the trusted set, regardless of how the process was started, so
     a future "just for testing" `--host 0.0.0.0` doesn't silently open this
     to the network.
  Caveat: the middleware trusts request.client.host, i.e. the ASGI server's
  view of the TCP peer. That's correct run standalone as instructed above, but
  if this is ever put behind a reverse proxy, request.client.host becomes the
  proxy's own address and this check stops meaning anything - you'd need real
  trusted-proxy / X-Forwarded-For handling instead. Don't put a proxy in front
  of this without revisiting it.

TRUSTED NETWORKS - configurable, with guardrails:
  Loopback (127.0.0.1, ::1) is always trusted and cannot be disabled via
  config - a blank or broken .env can only ever be as permissive as "localhost
  only", never fully open.

  Additional trusted networks come from the ALLOWED_HOSTS environment
  variable (comma-separated IPs or CIDR ranges), e.g.:
      ALLOWED_HOSTS=172.18.0.0/16
  If unset or empty, only loopback is trusted (the original behaviour).

  Every entry is parsed as a real network (via `ipaddress`), so a subnet like
  172.18.0.0/16 stays valid even as individual container IPs change across
  restarts - the previous version of this file hardcoded specific container
  IPs (172.18.0.1, 172.18.0.5), which silently breaks the day Docker assigns
  a container a new address.

  Security note: if an attacker can edit .env, they can already read whatever
  credentials live in it (DHIS2 auth, etc.) - this check was never going to
  stop that. What it protects against is *accidental* over-exposure: unless
  ALLOW_PUBLIC_HOSTS=true is also set, startup fails outright if ALLOWED_HOSTS
  contains anything outside private/loopback/link-local ranges. A misconfigured
  ".env" should crash loudly, not quietly put a health-surveillance endpoint on
  the open internet.

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

    # Strictly local (default - no .env changes needed):
    uvicorn api.main:app --host 127.0.0.1 --port 8000
    curl http://127.0.0.1:8000/events/<event_id>

    # Reachable from sibling containers on a Docker bridge network:
    #   .env: ALLOWED_HOSTS=172.18.0.0/16
    uvicorn api.main:app --host 172.18.0.1 --port 8000
    curl http://172.18.0.1:8000/events/<event_id>   # from a container on that bridge
"""

from __future__ import annotations

import ipaddress
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import requests as requests_lib
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from decode import build_field_maps, build_program_names, decode_event  # noqa: E402
from dhis2_client import DHIS2Client  # noqa: E402

load_dotenv()

PROGRAM_ID = "oWvNtR6iP8p"

# Loopback is always trusted and can never be removed by config - the
# permissive floor for this service is "localhost only", not "nothing".
_BASELINE_NETWORKS = [
    ipaddress.ip_network("127.0.0.1/32"),
    ipaddress.ip_network("::1/128"),
]


def _load_trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse ALLOWED_HOSTS into a list of ip_network objects, always including
    the loopback baseline. Fails startup loudly (rather than ignoring bad
    entries) on malformed CIDR/IP strings, and refuses to start with a
    non-private network configured unless ALLOW_PUBLIC_HOSTS=true - a
    misconfigured .env should crash, not quietly expose this on the internet.
    """
    networks = list(_BASELINE_NETWORKS)
    raw = os.environ.get("ALLOWED_HOSTS", "").strip()
    if not raw:
        return networks

    allow_public = os.environ.get("ALLOW_PUBLIC_HOSTS", "").strip().lower() in ("1", "true", "yes")

    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as e:
            raise RuntimeError(
                f"ALLOWED_HOSTS entry {entry!r} is not a valid IP or CIDR range: {e}"
            ) from e

        if not (network.is_private or network.is_loopback or network.is_link_local) and not allow_public:
            raise RuntimeError(
                f"ALLOWED_HOSTS entry {entry!r} is not a private/loopback/link-local range. "
                "This service is not meant to be internet-reachable. If you really mean to "
                "allow this, set ALLOW_PUBLIC_HOSTS=true explicitly."
            )
        networks.append(network)

    return networks


TRUSTED_NETWORKS = _load_trusted_networks()

_client: DHIS2Client | None = None
_field_maps: tuple[dict, dict, dict] | None = None
_program_names: dict | None = None


def _is_trusted(client_host: str | None) -> bool:
    if not client_host:
        return False
    try:
        addr = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(addr in network for network in TRUSTED_NETWORKS)


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
    description="Event Lookup API for Impuruza program events",
    lifespan=lifespan,
)


@app.middleware("http")
async def restrict_to_trusted_hosts(request: Request, call_next):
    client_host = request.client.host if request.client else None
    if not _is_trusted(client_host):
        return JSONResponse(
            status_code=403,
            content={"detail": f"This service does not accept requests from {client_host!r}."},
        )
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

    # DHIS2 omits a dataValue entirely when it's never been set - it does not send it
    # as null. For "Signal Verification Outcome" specifically, that means a still-open
    # signal (never Confirmed or Discarded) has NO key at all in decode_event's output,
    # rather than the key being present with a null value. A consumer checking this
    # field to decide ticket status needs it to always be there, so it can tell "still
    # open" (null) apart from "the DHIS2 response changed shape" (missing key). Force it
    # to appear, defaulting to null when absent - same reasoning DHIS2 itself doesn't
    # apply, so we apply it here instead.
    decoded.setdefault("Signal Verification Outcome", None)

    # orgUnit name needs a live lookup (not in the local metadata cache) - if it fails,
    # degrade to just the UID rather than failing the whole request over a display detail.
    org_unit_id = decoded.get("orgUnit")
    if org_unit_id:
        try:
            decoded["orgUnitName"] = _client.org_unit(org_unit_id, fields="id,name").get("name")
        except requests_lib.exceptions.RequestException:
            decoded["orgUnitName"] = None

    return decoded
