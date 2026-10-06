# RetailBank Customer Transaction Analytics — Team SkyForge

HCLTech AI-Cloud Data Engineering Hackathon. An AWS pipeline that turns the bank's messy raw files into a clean star schema and 13 KPIs, and applies a Day 2 incremental drop without duplicates.

**Design:** [docs/DESIGN.md](docs/DESIGN.md) · **Raw data profile:** [docs/DATA_PROFILE.md](docs/DATA_PROFILE.md)

## Architecture in one line

Raw files → **S3 (bronze)** → **Python/pandas on EC2 (silver: clean + validate, bad rows to quarantine)** → **RDS MySQL (gold: star schema, SCD2)** → **KPI SQL views** → **Streamlit dashboard**

## Project layout

```
data/raw/day1/      source files, exactly as received (Day 1)
data/raw/day2/      source files, exactly as received (Day 2 incremental)
pipeline/           Python pipeline code
  config.py         settings from environment variables (no secrets)
  profile_data.py   profiles raw files before cleaning
tests/              pytest tests
docs/               design document and data profile
```

More folders (`sql/`, `dashboard/`, `infra/`) are added in later phases.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                                    # run tests
.venv/bin/python -m pipeline.profile_data day1 day2 > docs/DATA_PROFILE.md
```

Copy `.env.example` to `.env` for local settings. Passwords are never stored in files: they are read from AWS SSM Parameter Store.

## AI use

We used Claude as an AI engineering assistant to speed up the build. The team reviewed the design, ran and checked every step, and can explain all the code.
