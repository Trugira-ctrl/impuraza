-- One Health Cross-Signal Correlation
-- ============================================================
-- Spatio-temporal join between human-disease eCBS signals and animal-health
-- eCBS signals sharing a sector within a rolling 14-day window, scored for
-- zoonotic spillover plausibility.
--
-- PRIVACY: ecbs_signal_events carries ZERO PII by design - no lookout name,
-- no phone number, no free-text village. It is populated by
-- scripts/one_health_correlation.py's loader (or an equivalent ETL step)
-- from the decoded tracker payload, keeping only geography/disease/date/
-- count fields. Do not add name/mobile_number columns to this table.
-- ============================================================

CREATE TABLE IF NOT EXISTS ecbs_signal_events (
    event_id              TEXT PRIMARY KEY,
    sector_id             TEXT NOT NULL,       -- org unit UID, hierarchy level 5
    sector_name           TEXT,
    district_id           TEXT,
    occurred_at           TIMESTAMP NOT NULL,  -- V{event_date} - when the signal was reported/captured
    onset_at              TIMESTAMP,           -- Impuruza_When the event started?
    detected_at           TIMESTAMP,           -- Impuruza_when the event was detected?
    verification_outcome  TEXT,                -- 'Confirmed' / 'Discarded' / NULL (still open)
    domain                TEXT NOT NULL CHECK (domain IN ('HUMAN', 'ANIMAL')),
    disease               TEXT,                -- decoded option NAME (not raw code) - Disease eCBS (HUMAN)
                                                 -- or Impuruza_Animal Disease (ANIMAL)
    species               TEXT,                -- Impuruza_Type of Species - ANIMAL rows only, else NULL
    case_count            INTEGER DEFAULT 0,   -- # CEB Cases (HUMAN) or Number of affected Animals (ANIMAL)
    death_count           INTEGER DEFAULT 0    -- Impuruza_Number of Deaths - HUMAN rows only, else 0
);

CREATE INDEX IF NOT EXISTS idx_ecbs_sector_time ON ecbs_signal_events (sector_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ecbs_domain       ON ecbs_signal_events (domain);

-- ------------------------------------------------------------
-- Zoonotic plausibility reference table.
--
-- Restricts the correlation to biologically plausible human<->animal disease
-- pairs using this instance's REAL decoded option-set labels (see
-- data/metadata/option_sets.json -> "Disease" and "OH AH CC List Of
-- Diseases"). Without this filter, a naive cross join of all 44 human x 54
-- animal disease options sharing a sector/time window would be dominated by
-- epidemiologically meaningless noise (e.g. Cholera co-occurring with Foot
-- and Mouth Disease is not a spillover signal).
--
-- NOT EXHAUSTIVE. Seeded with well-established zoonoses as a starting point
-- for review - have RBC/MOH veterinary epidemiology validate and extend this
-- list before treating its output as authoritative.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zoonotic_disease_map (
    human_disease    TEXT NOT NULL,
    animal_disease   TEXT NOT NULL,
    species          TEXT,             -- NULL = applies regardless of species
    base_risk_weight NUMERIC(3,2) NOT NULL CHECK (base_risk_weight BETWEEN 0 AND 1),
    PRIMARY KEY (human_disease, animal_disease, species)
);

INSERT INTO zoonotic_disease_map (human_disease, animal_disease, species, base_risk_weight) VALUES
    ('Anthrax',                             'Anthrax',                          'Cattle',  1.00),
    ('Anthrax',                             'Anthrax',                          'Goat',    0.90),
    ('Anthrax',                             'Anthrax',                          'Sheep',   0.85),
    ('Human Rabies',                        'Rabies',                           'Dog',     1.00),
    ('Human Rabies',                        'Rabies',                           'Cat',     0.60),
    ('Rift Valley Fever',                   'Rift Valley Fever',                'Cattle',  0.95),
    ('Rift Valley Fever',                   'Rift Valley Fever',                'Goat',    0.85),
    ('Rift Valley Fever',                   'Rift Valley Fever',                'Sheep',   0.85),
    ('Human influenza due to a new subtype','High Pathogenic Influenza virus',  'Chicken', 0.90),
    ('Human influenza due to a new subtype','High Pathogenic Influenza virus',  'Duck',    0.85),
    ('Monkey  pox',                         'Monkeypox',                        NULL,      0.75),
    ('Zika',                                'Zika',                             NULL,      0.55),
    ('Viral hemorrhagic fever',             'Crimean Congo hemorrhagic fever',  'Cattle',  0.70),
    ('Viral hemorrhagic fever',             'Crimean Congo hemorrhagic fever',  'Goat',    0.65),
    ('Foodborne illnesses',                 'Salmonellosis',                    'Chicken', 0.60),
    ('Foodborne illnesses',                 'Campylobacter',                    'Cattle',  0.55)
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------
-- The correlation query.
--
-- Output schema: sector_id, human_disease, animal_disease, animal_species,
--                time_delta_days, spillover_risk_score
--
-- Edge cases handled:
--   - Negative / reversed time lags: the join window is symmetric
--     (BETWEEN -14 AND +14 days) and the score's decay term uses ABS(),
--     so it doesn't matter whether the human or animal signal came first.
--   - Missing dates: occurred_at is NOT NULL in the schema; the WHERE
--     guards below are a defensive backstop in case the loader is ever run
--     against a table without that constraint enforced.
--   - Missing/undocumented disease pairs: LEFT JOIN + COALESCE gives them a
--     low-confidence default weight (0.15) instead of dropping them or
--     raising an error, so genuinely novel co-occurrences are still visible
--     (ranked low) rather than silently discarded.
--   - Zero case/death counts: LN(1 + ...) with COALESCE(...,0) guarantees
--     the log argument is always >= 1, never zero or negative - no domain
--     error, and a report of "0 cases" still produces a valid (low) score.
-- ------------------------------------------------------------
WITH human AS (
    SELECT event_id, sector_id, sector_name, occurred_at,
           disease AS human_disease,
           case_count AS human_cases,
           death_count AS human_deaths
    FROM ecbs_signal_events
    WHERE domain = 'HUMAN'
      AND disease IS NOT NULL
      AND occurred_at IS NOT NULL
),
animal AS (
    SELECT event_id, sector_id, occurred_at,
           disease AS animal_disease,
           species,
           case_count AS animal_cases
    FROM ecbs_signal_events
    WHERE domain = 'ANIMAL'
      AND disease IS NOT NULL
      AND occurred_at IS NOT NULL
)
SELECT
    h.sector_id,
    h.sector_name,
    h.human_disease                                                          AS "Human_Disease",
    a.animal_disease                                                         AS "Animal_Disease",
    a.species                                                                AS "Animal_Species",
    ROUND(
        ABS(EXTRACT(EPOCH FROM (h.occurred_at - a.occurred_at))) / 86400.0, 1
    )                                                                        AS "Time_Delta_Days",
    ROUND(
        LEAST(
            100,
            100
            * COALESCE(z.base_risk_weight, 0.15)  -- 0.15 = low-confidence default for pairs not in the reference map
            * EXP(
                -ABS(EXTRACT(EPOCH FROM (h.occurred_at - a.occurred_at))) / 86400.0 / 14.0
              )                                    -- temporal decay: ~halves every ~10 days apart
            * LN(
                1
                + COALESCE(h.human_cases, 0)
                + COALESCE(h.human_deaths, 0) * 3   -- deaths weighted 3x a case for severity
                + COALESCE(a.animal_cases, 0)
              )                                     -- magnitude, log-dampened so one huge outbreak
                                                      -- doesn't blow the score past the LEAST(100, ...) cap
        )::NUMERIC,
        2
    )                                                                        AS "Spillover_Risk_Score"
FROM human h
JOIN animal a
    ON a.sector_id = h.sector_id
   AND a.occurred_at BETWEEN h.occurred_at - INTERVAL '14 days'
                         AND h.occurred_at + INTERVAL '14 days'
LEFT JOIN zoonotic_disease_map z
    ON z.human_disease  = h.human_disease
   AND z.animal_disease = a.animal_disease
   AND (z.species IS NULL OR z.species = a.species)
ORDER BY "Spillover_Risk_Score" DESC;
