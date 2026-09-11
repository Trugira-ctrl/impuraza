"""
One Health Cross-Signal Correlation - Pandas equivalent of
sql/one_health_correlation.sql, for teams running the analytics engine
outside Postgres (e.g. straight off a fetch_tracked_entities.py export) or
who want to iterate on the risk-scoring formula in a notebook before
committing it to SQL.

Input: a DataFrame shaped like the ecbs_signal_events table (see the SQL
file's DDL for column definitions) - PII-free by construction. Load it from
the DHIS2 decoded export via `load_signal_events()` below, which reuses
fetch_tracked_entities.py's decoder maps but only ever extracts geography/
disease/date/count fields - never Lookout Names or Mobile number.

Usage:
    python3 scripts/fetch_tracked_entities.py --all       # produces the decoded export this reads
    python3 scripts/one_health_correlation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))  # fetch_tracked_entities.py lives alongside this file
from fetch_tracked_entities import build_decoder_maps  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = ROOT / "data" / "samples"
METADATA_DIR = ROOT / "data" / "metadata"

WINDOW_DAYS_DEFAULT = 14
LOW_CONFIDENCE_DEFAULT_WEIGHT = 0.15

# Same reference pairs as sql/one_health_correlation.sql's zoonotic_disease_map -
# keep the two in sync if you edit one. species=None means "any species".
ZOONOTIC_DISEASE_MAP = pd.DataFrame(
    [
        ("Anthrax", "Anthrax", "Cattle", 1.00),
        ("Anthrax", "Anthrax", "Goat", 0.90),
        ("Anthrax", "Anthrax", "Sheep", 0.85),
        ("Human Rabies", "Rabies", "Dog", 1.00),
        ("Human Rabies", "Rabies", "Cat", 0.60),
        ("Rift Valley Fever", "Rift Valley Fever", "Cattle", 0.95),
        ("Rift Valley Fever", "Rift Valley Fever", "Goat", 0.85),
        ("Rift Valley Fever", "Rift Valley Fever", "Sheep", 0.85),
        ("Human influenza due to a new subtype", "High Pathogenic Influenza virus", "Chicken", 0.90),
        ("Human influenza due to a new subtype", "High Pathogenic Influenza virus", "Duck", 0.85),
        ("Monkey  pox", "Monkeypox", None, 0.75),
        ("Zika", "Zika", None, 0.55),
        ("Viral hemorrhagic fever", "Crimean Congo hemorrhagic fever", "Cattle", 0.70),
        ("Viral hemorrhagic fever", "Crimean Congo hemorrhagic fever", "Goat", 0.65),
        ("Foodborne illnesses", "Salmonellosis", "Chicken", 0.60),
        ("Foodborne illnesses", "Campylobacter", "Cattle", 0.55),
    ],
    columns=["human_disease", "animal_disease", "species", "base_risk_weight"],
)

DISEASE_DE = "BHaSNB8TwzI"          # "Disease eCBS" (human)
ANIMAL_DISEASE_DE = "OZ7Cd2q0CKV"   # "Impuruza_Animal Disease"
SPECIES_DE = "gx8WIi4FZ55"          # "Impuruza_Type of Species"
CASE_COUNT_DE = "gRi3KDFSAeO"       # "# CEB Cases"
ANIMAL_AFFECTED_DE = "V3pskbLq3tm"  # "Impuruza_Number of affected Animals"
DEATHS_DE = "HTsBBamgqWw"           # "Impuruza_Number of Deaths"


def load_signal_events(decoded_path: Path = SAMPLES_DIR / "tracked_entities.decoded.json") -> pd.DataFrame:
    """
    Flatten fetch_tracked_entities.py's decoded export into one row per
    (event, domain) where domain in {HUMAN, ANIMAL} depending on which
    disease field is populated on that event. NEVER reads the decoded
    "Lookout Names" / "Mobile number" attribute keys - only orgUnit,
    occurredAt and the disease/species/count dataValues.
    """
    field_names, _, _ = build_decoder_maps()
    disease_label = field_names.get(DISEASE_DE, "Disease eCBS")
    animal_disease_label = field_names.get(ANIMAL_DISEASE_DE, "Impuruza_Animal Disease")
    species_label = field_names.get(SPECIES_DE, "Impuruza_Type of Species")
    case_count_label = field_names.get(CASE_COUNT_DE, "# CEB Cases")
    animal_affected_label = field_names.get(ANIMAL_AFFECTED_DE, "Impuruza_Number of affected Animals")
    deaths_label = field_names.get(DEATHS_DE, "Impuruza_Number of Deaths")

    records = json.loads(decoded_path.read_text())
    rows = []
    for te in records:
        sector_id = te.get("orgUnit")  # event's assigned org unit, not the reporter's residence attribute
        for enr in te.get("enrollments", []):
            for ev in enr.get("events", []):
                dv = ev.get("dataValues", {})
                human_disease = dv.get(disease_label)
                animal_disease = dv.get(animal_disease_label)
                if not human_disease and not animal_disease:
                    continue  # no disease captured on this event - not usable for correlation
                occurred_at = pd.to_datetime(ev.get("occurredAt"), errors="coerce")
                if pd.isna(occurred_at):
                    continue
                if human_disease:
                    rows.append(
                        {
                            "event_id": ev["event"],
                            "sector_id": sector_id,
                            "occurred_at": occurred_at,
                            "domain": "HUMAN",
                            "disease": human_disease,
                            "species": None,
                            "case_count": pd.to_numeric(dv.get(case_count_label), errors="coerce"),
                            "death_count": pd.to_numeric(dv.get(deaths_label), errors="coerce"),
                        }
                    )
                if animal_disease:
                    rows.append(
                        {
                            "event_id": ev["event"],
                            "sector_id": sector_id,
                            "occurred_at": occurred_at,
                            "domain": "ANIMAL",
                            "disease": animal_disease,
                            "species": dv.get(species_label),
                            "case_count": pd.to_numeric(dv.get(animal_affected_label), errors="coerce"),
                            "death_count": None,
                        }
                    )
    return pd.DataFrame(rows)


def one_health_correlation(
    df: pd.DataFrame,
    zoonotic_map: pd.DataFrame = ZOONOTIC_DISEASE_MAP,
    window_days: int = WINDOW_DAYS_DEFAULT,
) -> pd.DataFrame:
    """
    df columns: event_id, sector_id, occurred_at (datetime64), domain
    ('HUMAN'/'ANIMAL'), disease, species, case_count, death_count.

    Edge cases: rows with a missing occurred_at or disease are dropped up
    front (can't be temporally or diagnostically matched); missing counts
    are treated as 0 rather than dropped, since a signal with an unfilled
    case-count field is still a real, matchable event.
    """
    df = df.dropna(subset=["occurred_at", "disease"]).copy()

    human = df[df["domain"] == "HUMAN"].rename(
        columns={"disease": "human_disease", "case_count": "human_cases", "death_count": "human_deaths"}
    )
    animal = df[df["domain"] == "ANIMAL"].rename(columns={"disease": "animal_disease", "case_count": "animal_cases"})

    if human.empty or animal.empty:
        return pd.DataFrame(
            columns=["sector_id", "Human_Disease", "Animal_Disease", "Animal_Species", "Time_Delta_Days", "Spillover_Risk_Score"]
        )

    # Sector-keyed join first (cheap), THEN filter to the time window - avoids
    # a full nationwide cross product before narrowing down.
    merged = human.merge(animal, on="sector_id", suffixes=("_h", "_a"))
    delta_days = (merged["occurred_at_h"] - merged["occurred_at_a"]).abs().dt.total_seconds() / 86400.0
    merged["time_delta_days"] = delta_days.round(1)
    merged = merged[merged["time_delta_days"] <= window_days]

    merged = merged.merge(zoonotic_map, on=["human_disease", "animal_disease"], how="left", suffixes=("", "_map"))
    species_match = merged["species_map"].isna() | (merged["species_map"] == merged["species"])
    merged["base_risk_weight"] = np.where(species_match, merged["base_risk_weight"], np.nan)
    merged["base_risk_weight"] = merged["base_risk_weight"].fillna(LOW_CONFIDENCE_DEFAULT_WEIGHT)

    human_cases = merged["human_cases"].fillna(0)
    human_deaths = merged["human_deaths"].fillna(0)
    animal_cases = merged["animal_cases"].fillna(0)

    decay = np.exp(-merged["time_delta_days"] / 14.0)
    magnitude = np.log1p(human_cases + human_deaths * 3 + animal_cases)
    merged["spillover_risk_score"] = (100 * merged["base_risk_weight"] * decay * magnitude).clip(upper=100).round(2)

    return (
        merged[["sector_id", "human_disease", "animal_disease", "species", "time_delta_days", "spillover_risk_score"]]
        .rename(
            columns={
                "human_disease": "Human_Disease",
                "animal_disease": "Animal_Disease",
                "species": "Animal_Species",
                "time_delta_days": "Time_Delta_Days",
                "spillover_risk_score": "Spillover_Risk_Score",
            }
        )
        .sort_values("Spillover_Risk_Score", ascending=False)
        .reset_index(drop=True)
    )


if __name__ == "__main__":
    events = load_signal_events()
    result = one_health_correlation(events)
    print(result.to_string(index=False))
