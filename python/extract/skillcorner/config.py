"""
Configuration for the SkillCorner API and Snowflake connection.

SkillCorner API reference: https://skillcorner.com/api/docs/ (page itself is
behind an OAuth login wall; auth scheme below was determined empirically —
see the module docstring in skillcorner_api.py).
"""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)

SKILLCORNER_BASE_URL = os.getenv("SKILLCORNER_BASE_URL", "https://skillcorner.com/api")
SKILLCORNER_API_KEY = os.getenv("SKILLCORNER_API_KEY")

# The account's real match access is NOT scoped to Charlton's own competition
# -- confirmed empirically it spans 7 (competition, edition) pairs across 6
# different competitions (England Championship/League One/League Two, plus
# partial coverage of Eredivisie, Allsvenskan, and Scottish Premiership),
# 3,005 matches total, none before 2023/24. This looks like whatever
# SkillCorner subscription tier the club has, not a Charlton-specific
# entitlement. Every edition listed in /competition_editions/ going back to
# 2019/2020 that ISN'T one of these 7 returns zero matches for this account.
# Use skillcorner_api.get_available_competition_editions() to discover the
# current list live rather than hardcoding it -- what's licensed can change.
CHAMPIONSHIP_COMPETITION_ID = int(os.getenv("SKILLCORNER_COMPETITION_ID", "31"))

# Snowflake database/schema this extractor writes to.
SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "CAFC_DB")
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "SKILLCORNER_RAW")

REQUEST_TIMEOUT = 30
