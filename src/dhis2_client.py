"""
Minimal DHIS2 API client for cbs2.moh.gov.rw (Rwanda MoH "Impuruza" tracker instance).

Reads credentials from a .env file (simple KEY=VALUE lines, whitespace around
'=' and values is tolerated). Does not depend on python-dotenv so the project
has zero extra install requirements beyond `requests`.

Usage:
    from dhis2_client import DHIS2Client
    client = DHIS2Client()
    info = client.get("/api/system/info.json")
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import requests

DEFAULT_BASE_URL = "https://cbs2.moh.gov.rw"
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(env_path: Path | str = REPO_ROOT / ".env") -> dict[str, str]:
    """Parse a .env file into a dict, tolerating spaces around '=' and values."""
    env_path = Path(env_path)
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


class DHIS2Client:
    """Thin wrapper around the DHIS2 REST/Tracker API with Basic Auth."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        username: str | None = None,
        password: str | None = None,
        env_path: Path | str = REPO_ROOT / ".env",
        timeout: int = 60,
    ):
        env = load_env(env_path)
        self.base_url = base_url.rstrip("/")
        self.username = username or env.get("Username") or os.environ.get("DHIS2_USERNAME")
        self.password = password or env.get("Password") or os.environ.get("DHIS2_PASSWORD")
        if not self.username or not self.password:
            raise RuntimeError(
                f"No credentials found. Expected Username=/Password= in {env_path} "
                "or DHIS2_USERNAME/DHIS2_PASSWORD env vars."
            )
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (self.username, self.password)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET an API path (e.g. '/api/system/info.json') and return parsed JSON."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def whoami(self) -> dict:
        return self.get("/api/me.json", {"fields": "id,username,name,authorities,organisationUnits[id,name]"})

    def system_info(self) -> dict:
        return self.get("/api/system/info.json", {"fields": "version,revision,serverDate,contextPath"})

    def program_metadata(self, program_id: str) -> dict:
        fields = (
            "id,name,programType,trackedEntityType[id,name],"
            "programTrackedEntityAttributes[mandatory,displayInList,sortOrder,"
            "trackedEntityAttribute[id,name,valueType,optionSet[id,name]]],"
            "programStages[id,name,programStageDataElements["
            "compulsory,dataElement[id,name,valueType,optionSet[id,name]]]]"
        )
        return self.get(f"/api/programs/{program_id}.json", {"fields": fields})

    def option_set(self, option_set_id: str) -> dict:
        return self.get(f"/api/optionSets/{option_set_id}.json", {"fields": "id,name,options[code,name]"})

    def org_unit_levels(self) -> dict:
        return self.get("/api/organisationUnitLevels.json", {"fields": "id,name,level", "order": "level:asc", "paging": "false"})

    def org_unit(self, org_unit_id: str, fields: str = "id,name,level,path,parent[id,name]") -> dict:
        return self.get(f"/api/organisationUnits/{org_unit_id}.json", {"fields": fields})

    def get_event(
        self,
        event_id: str,
        fields: str = (
            "event,program,programStage,trackedEntity,orgUnit,status,occurredAt,updatedAt,"
            "dataValues[dataElement,value]"
        ),
    ) -> dict:
        """One event by its own UID, via the tracker API's single-event path (not a
        paginated /api/tracker/events?event= filter - this hits the resource directly).
        Raises requests.exceptions.HTTPError (404) if the ID doesn't exist."""
        return self.get(f"/api/tracker/events/{event_id}", {"fields": fields})

    def tracked_entities_page(
        self,
        program: str,
        org_units: str,
        org_unit_mode: str = "DESCENDANTS",
        page: int = 1,
        page_size: int = 100,
        fields: str = (
            "trackedEntity,orgUnit,attributes[attribute,value],"
            "enrollments[enrollment,program,status,enrollmentDate,"
            "events[event,status,occurredAt,dataValues[dataElement,value]]]"
        ),
        total_pages: bool = False,
    ) -> dict:
        """One page of the tracker/trackedEntities endpoint (Tracker API v41+)."""
        params = {
            "program": program,
            "orgUnits": org_units,
            "orgUnitMode": org_unit_mode,
            "page": page,
            "pageSize": page_size,
            "fields": fields,
        }
        if total_pages:
            params["totalPages"] = "true"
        return self.get("/api/41/tracker/trackedEntities", params)

    def iter_tracked_entities(
        self,
        program: str,
        org_units: str,
        org_unit_mode: str = "DESCENDANTS",
        page_size: int = 100,
        max_pages: int | None = None,
        **kwargs: Any,
    ) -> Iterator[dict]:
        """Yield tracked entity records across all pages (or up to max_pages)."""
        page = 1
        while True:
            data = self.tracked_entities_page(
                program, org_units, org_unit_mode=org_unit_mode, page=page, page_size=page_size, **kwargs
            )
            entities = data.get("trackedEntities", [])
            if not entities:
                return
            yield from entities
            if max_pages is not None and page >= max_pages:
                return
            page += 1


if __name__ == "__main__":
    client = DHIS2Client()
    print(json.dumps(client.system_info(), indent=2))
