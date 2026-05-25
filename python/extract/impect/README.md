# CAFC Utils

Simple Python utilities for fetching data from Impect API and loading it into Snowflake.

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Create a `.env` file from the template:
```bash
cp .env.example .env
```

3. Fill in your Impect username/password and Snowflake connection details in `.env`

## Authentication

The Impect API uses OAuth2 authentication. The utility automatically handles token management:

- **First request**: Fetches an OAuth2 token using your username and password
- **Token caching**: Stores the token in `.impect_token_cache.json` (valid for 24 hours)
- **Automatic refresh**: Requests a new token when the cached one expires
- **Rate limiting**: Automatically handles 429 errors (rate limit exceeded) with 1-second delays

You don't need to manage tokens manually - just provide your username and password in `.env`.

## Running the Load Scripts

Each script runs independently and loads data into `CAFC_DB.IMPECT_RAW`:

```bash
python load_iterations.py   # Loads all iterations
python load_matches.py      # Loops all iterations, loads matches
python load_squads.py       # Loops all iterations, loads squads
python load_squad_ratings.py # Loops all iterations, loads squad ratings
python load_squad_ratings.py --iteration-ids 1410,1411,1412 # Loads only selected iterations
python load_players.py      # Loops all iterations, loads players
python load_coaches.py      # Loops all iterations, loads coaches
python load_stadiums.py     # Loops all iterations, loads stadiums
python load_countries.py    # Loads all countries
python load_championship_match_details.py # Downloads last 5 Championship seasons and loads IMPECT_RAW Championship tables
python load_championship_match_details.py --limit-matches 10 # Useful test run
python load_championship_match_details.py --skip-snowflake # Keep local files only
```

All scripts do a full replace (TRUNCATE + INSERT) on each run.

## Snowflake Tables

| Script | Table |
|---|---|
| `load_iterations.py` | `CAFC_DB.IMPECT_RAW.ITERATIONS` |
| `load_matches.py` | `CAFC_DB.IMPECT_RAW.MATCHES` |
| `load_squads.py` | `CAFC_DB.IMPECT_RAW.SQUADS` |
| `load_squad_ratings.py` | `CAFC_DB.IMPECT_RAW.SQUAD_RATINGS` |
| `load_players.py` | `CAFC_DB.IMPECT_RAW.PLAYERS` |
| `load_coaches.py` | `CAFC_DB.IMPECT_RAW.COACHES` |
| `load_stadiums.py` | `CAFC_DB.IMPECT_RAW.STADIUMS` |
| `load_countries.py` | `CAFC_DB.IMPECT_RAW.COUNTRIES` |
| `load_championship_match_details.py` | `CAFC_DB.IMPECT_RAW.CHAMPIONSHIP_MATCH_INDEX` |
| `load_championship_match_details.py` | `CAFC_DB.IMPECT_RAW.CHAMPIONSHIP_MATCH_INFO` |
| `load_championship_match_details.py` | `CAFC_DB.IMPECT_RAW.CHAMPIONSHIP_PLAYER_KPIS` |
| `load_championship_match_details.py` | `CAFC_DB.IMPECT_RAW.CHAMPIONSHIP_SQUAD_KPIS` |

Tables are auto-created on first run.

## Available API Functions

```python
from impect_api import get_iterations, get_matches, get_squads
from impect_api import get_squad_ratings, get_players, get_coaches
from impect_api import get_stadiums, get_countries
from impect_api import get_match_info, get_match_player_kpis, get_match_squad_kpis

# Non-iteration-scoped
iterations = get_iterations()
countries = get_countries()

# Iteration-scoped (require an explicit iteration_id)
matches = get_matches(iteration_id=1410)
squads = get_squads(iteration_id=1410)
squad_ratings = get_squad_ratings(iteration_id=1410)
players = get_players(iteration_id=1410)
coaches = get_coaches(iteration_id=1410)
stadiums = get_stadiums(iteration_id=1410)
match_info = get_match_info(match_id=206530)
player_kpis = get_match_player_kpis(match_id=206530)
squad_kpis = get_match_squad_kpis(match_id=206530)
```

## File Structure

```
cafc_utils/
├── config.py              # Configuration and environment variables
├── impect_api.py          # Functions to fetch data from Impect API
├── snowflake_loader.py    # Functions to load DataFrames to Snowflake
├── load_iterations.py     # ETL script: iterations
├── load_matches.py        # ETL script: matches (all iterations)
├── load_squads.py         # ETL script: squads (all iterations)
├── load_squad_ratings.py  # ETL script: squad ratings (all iterations)
├── load_players.py        # ETL script: players (all iterations)
├── load_coaches.py        # ETL script: coaches (all iterations)
├── load_stadiums.py       # ETL script: stadiums (all iterations)
├── load_countries.py      # ETL script: countries
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variables template
└── README.md              # This file
```
