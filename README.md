# cafc-data-platform

Canonical data platform for CAFC. Owns raw landing zones, identity resolution, KPI fact tables, and `APP_COMPAT` views in `CAFC_DB`. Deploys on a different lifecycle and Snowflake role than the recruitment-platform application repo — see `cafc-data-platform-plan.md` for the full rationale.

## Layout (WIP)

```
cafc-data-platform/
├── python/
│   ├── extract/
│   │   └── impect/       # vendored from cafc_utils; IMPECT API → IMPECT_RAW.*
│   └── identity/         # (empty) matcher / mint / candidates / merge
├── dbt/                  # (empty) staging → canonical → app_compat models
├── snowflake/
│   ├── ddl/              # (empty) schemas, grants, operational tables
│   └── tasks/            # (empty) scheduled jobs, file-drop ingest
└── .github/workflows/    # (empty) PR + nightly CI
```

Only `python/extract/impect/` is populated so far. The dbt project, identity loader, orchestrator, Snowflake DDL, and CI workflows land in subsequent slices.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

The IMPECT extractor reads credentials from `python/extract/impect/.env` (gitignored). A template lives at `python/extract/impect/.env.example`.
