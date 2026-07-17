"""
Configuration for the Hudl DVMS API and Snowflake connections.

DVMS API reference: https://dvms.premierleague.com/dvms/api-docs
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Explicit path (not a bare load_dotenv()) because this module is invoked as
# `python -m python.extract.dvms.load_dvms_fixtures` from the repo root, so
# cwd-relative dotenv discovery wouldn't find this directory's .env.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

# DVMS API configuration
DVMS_BASE_URL = os.getenv("DVMS_BASE_URL", "https://dvms.premierleague.com")
DVMS_USERNAME = os.getenv("DVMS_USERNAME")
DVMS_PASSWORD = os.getenv("DVMS_PASSWORD")

# Default target: EFL Championship, 2025-26 season. The competitionId is the
# Hudl id confirmed via GET /dvms/competitions; "season" is the API's own
# season key (the start year), confirmed against the DVMS web UI's season
# filter, which is what the club's DVMS account is scoped to today.
DVMS_COMPETITION_ID = os.getenv("DVMS_COMPETITION_ID", "5f282b8c8cdf2d0b082cc81c")
DVMS_SEASON = os.getenv("DVMS_SEASON", "2025")

# 100 confirmed working live; the API silently returns 0 fixtures for very
# large limits (1000 tested, returned nothing) rather than erroring, so stay
# conservative rather than guessing a higher ceiling.
FIXTURES_PAGE_LIMIT = int(os.getenv("DVMS_FIXTURES_PAGE_LIMIT", "100"))

# Snowflake schema this extractor writes to.
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "DVMS_RAW")

# Request settings
REQUEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 60

# Seconds to sleep before every asset download. A 2026-07-17 test run of
# ~3300 downloads with no pacing at all triggered DVMS anti-abuse protection
# severe enough to temporarily block the login endpoint itself. This value
# is a conservative default, NOT a confirmed-safe rate — DVMS's actual
# limits aren't published/known. Do not lower this without confirming real
# limits (e.g. from Hudl support or documented guidance); raise it if 403s
# recur even at this pace. Overridable for whoever eventually tunes it.
DOWNLOAD_DELAY_SECONDS = float(os.getenv("DVMS_DOWNLOAD_DELAY_SECONDS", "1.5"))

# Asset payloads at or above this size are aborted mid-download (metadata-only
# row, no RAW_PAYLOAD) rather than pulled inline — guards the 16MB
# unqualified-VARCHAR ceiling on DVMS_RAW.ASSETS.RAW_PAYLOAD with headroom.
# This is a streamed backstop checked via Content-Length / bytes-read during
# download — NOT gated on the fixtures response's info.sizeBytes, which comes
# back null for every Opta/SecondSpectrum asset (only video populates it).
MAX_INLINE_ASSET_BYTES = 15 * 1024 * 1024

# ASSET_SUBTYPE codes confirmed small enough to download inline (see DVMS API
# docs' DataSubType enum). Deliberately an allowlist, not a denylist of known-
# big types: video/panoramic/GeniusSports and the raw positional tracking
# feeds (38 SecondSpectrumDataJSON[L], 39 SecondSpectrumDataXML — confirmed
# ~420MB/match live) are excluded by omission, not by guessing their size.
#   20 OptaF24 (event data), 21 OptaF7 (team sheet)                — ~1MB, ~20KB
#   40 SecondSpectrumMetadataJSON, 41 SecondSpectrumMetadataXML    — <10KB
#   42 SecondSpectrumPhysicalSplits, 43 SecondSpectrumPhysicalSummary — <40KB
DOWNLOADABLE_ASSET_SUBTYPES = {20, 21, 40, 41, 42, 43}
