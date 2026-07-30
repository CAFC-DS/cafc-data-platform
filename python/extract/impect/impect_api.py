"""
Simple procedural functions to fetch data from Impect API
"""
import requests
import time
import json
import os
import threading
from datetime import datetime, timedelta
from urllib.parse import urlencode
from typing import Optional, Dict, Any
import config


_thread_local = threading.local()


def load_cached_token() -> Optional[Dict[str, Any]]:
    """
    Load cached token from file if it exists and is not expired

    Returns:
        Token data if valid, None otherwise
    """
    if not os.path.exists(config.TOKEN_CACHE_FILE):
        return None

    try:
        with open(config.TOKEN_CACHE_FILE, 'r') as f:
            token_data = json.load(f)

        # Check if token is expired
        expiry = datetime.fromisoformat(token_data['expiry'])
        if datetime.now() < expiry:
            return token_data
    except (json.JSONDecodeError, KeyError, ValueError):
        pass

    return None


def save_token_cache(token: str, expires_in: int):
    """
    Save token to cache file

    Args:
        token: OAuth2 access token
        expires_in: Token validity in seconds
    """
    # Use slightly shorter expiry to be safe (subtract 5 minutes)
    expiry = datetime.now() + timedelta(seconds=expires_in - 300)

    token_data = {
        'token': token,
        'expiry': expiry.isoformat()
    }

    with open(config.TOKEN_CACHE_FILE, 'w') as f:
        json.dump(token_data, f)


def get_auth_token() -> str:
    """
    Get OAuth2 access token (from cache or by requesting new one)

    Returns:
        OAuth2 access token
    """
    # Try to load from cache
    cached = load_cached_token()
    if cached:
        return cached['token']

    # Request new token
    if not config.IMPECT_USERNAME or not config.IMPECT_PASSWORD:
        raise Exception("IMPECT_USERNAME and IMPECT_PASSWORD must be set in environment")

    # Prepare form data (URL-encoded)
    data = {
        'client_id': 'api',
        'grant_type': 'password',
        'username': config.IMPECT_USERNAME,
        'password': config.IMPECT_PASSWORD
    }

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    try:
        response = requests.post(
            config.IMPECT_TOKEN_URL,
            data=urlencode(data),
            headers=headers,
            timeout=config.REQUEST_TIMEOUT
        )
        response.raise_for_status()
        token_response = response.json()

        access_token = token_response['access_token']
        expires_in = token_response.get('expires_in', 86400)  # Default 24 hours

        # Save to cache
        save_token_cache(access_token, expires_in)

        return access_token

    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to get authentication token: {str(e)}")


def get_auth_headers() -> Dict[str, str]:
    """
    Get authentication headers with Bearer token

    Returns:
        Dictionary with authentication headers
    """
    token = get_auth_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


def get_http_session() -> requests.Session:
    """
    Return a thread-local requests session so repeated API calls can reuse
    TCP/TLS connections safely across worker threads.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def force_token_refresh() -> None:
    """Clear the cached token so the next get_auth_token() forces a fresh login."""
    try:
        os.remove(config.TOKEN_CACHE_FILE)
    except FileNotFoundError:
        pass


# Cap on total attempts per request. Generous because long-running extracts
# can encounter sustained rate-limiting and we'd rather wait than fail a call.
DEFAULT_MAX_ATTEMPTS = 10
# 429 backoff schedule: doubles each attempt, capped at 60s.
MAX_429_SLEEP_SECONDS = 60


def make_request(endpoint: str, params: Optional[Dict] = None,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> Dict[str, Any]:
    """
    Make a GET request to the Impect API with token-refresh and exponential
    backoff. Raises on full exhaustion (does not silently return {} like the
    old version, which caused player-level loaders to silently lose data).

    Three failure modes handled:
        - 401: token expired. Clear cache, retry once with fresh token.
        - 429: rate limited. Exponential backoff (1, 2, 4, 8, 16, 32, 60s cap).
        - Network errors: exponential backoff with cap of 30s.

    Args:
        endpoint: API endpoint path (e.g., '/v5/customerapi/iterations')
        params: Optional query parameters
        max_attempts: Maximum retry budget (default 10)

    Returns:
        JSON response as dictionary
    """
    url = f"{config.IMPECT_BASE_URL}{endpoint}"
    session = get_http_session()

    rate_limit_backoff = 1.0
    refreshed_token_this_call = False

    for attempt in range(max_attempts):
        # Fetch headers INSIDE the loop so token refresh between retries
        # actually takes effect.
        headers = get_auth_headers()
        try:
            response = session.get(
                url,
                headers=headers,
                params=params,
                timeout=config.REQUEST_TIMEOUT,
            )

            # Handle rate limiting (429 Too Many Requests) with exponential backoff.
            if response.status_code == 429:
                sleep_s = min(rate_limit_backoff, MAX_429_SLEEP_SECONDS)
                # Quieter logging: only print every 3rd 429 to avoid drowning the log.
                if attempt % 3 == 0:
                    print(f"  rate limit (429) on {endpoint}, waiting {sleep_s:.0f}s (attempt {attempt + 1}/{max_attempts})")
                time.sleep(sleep_s)
                rate_limit_backoff *= 2
                continue

            # Handle expired token (401) with one-shot refresh.
            if response.status_code == 401 and not refreshed_token_this_call:
                print(f"  auth expired on {endpoint}, refreshing token")
                force_token_refresh()
                refreshed_token_this_call = True
                continue

            # Other 4xx errors are permanent (bad request, not found, etc.) --
            # confirmed live that Impect returns 400 with a real error message
            # (e.g. "Match does not have packing plus data" for matches
            # predating event-level tracking) rather than an empty payload.
            # Retrying these with backoff wastes minutes per call for no
            # benefit; raise immediately so callers can distinguish "this
            # match genuinely has no data" from a transient failure.
            if 400 <= response.status_code < 500 and response.status_code not in (401, 429):
                raise Exception(
                    f"{response.status_code} error from {endpoint}: {response.text[:300]}"
                )

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            if attempt == max_attempts - 1:
                raise Exception(f"Failed to fetch data from {endpoint}: {str(e)}")
            time.sleep(min(2 ** attempt, 30))

    raise Exception(
        f"Exhausted {max_attempts} attempts for {endpoint} "
        f"(likely sustained rate limiting; consider lowering concurrency)"
    )


def get_iterations(params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get all iterations

    Args:
        params: Optional query parameters

    Returns:
        Iterations data
    """
    return make_request("/v5/customerapi/iterations", params)


def get_matches(iteration_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get matches for a specific iteration

    Args:
        iteration_id: Iteration ID
        params: Optional query parameters

    Returns:
        Matches data
    """
    return make_request(f"/v5/customerapi/iterations/{iteration_id}/matches", params)


def get_match_info(match_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get detailed metadata for a specific match

    Args:
        match_id: Match ID
        params: Optional query parameters

    Returns:
        Match info payload
    """
    return make_request(f"/v5/customerapi/matches/{match_id}", params)


def get_match_player_kpis(match_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get player KPI data for a specific match

    Args:
        match_id: Match ID
        params: Optional query parameters

    Returns:
        Player KPI payload
    """
    return make_request(f"/v5/customerapi/matches/{match_id}/player-kpis", params)


def get_match_events(match_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get raw match events for a specific match.

    Args:
        match_id: Match ID
        params: Optional query parameters

    Returns:
        Event payload (list of event dicts under "data").

    Raises:
        Exception (via make_request): confirmed live that matches predating
        Impect's event-level tracking return HTTP 400 with
        {"message": "Match does not have packing plus data"} rather than an
        empty list -- callers should treat a 400 here as "no event data for
        this match", not a transient failure worth retrying at a higher level.
    """
    return make_request(f"/v5/customerapi/matches/{match_id}/events", params)


def get_match_event_kpis(match_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get stand-alone KPIs computed at event level (GET /matches/{id}/event-kpis).

    Confirmed live: this is a *separate* endpoint from get_match_events, not
    a field on the event itself -- it returns long-format rows
    {eventId, position, playerId, kpiId, value}, roughly 10 rows per event
    (multiple players/positions get a KPI attributed to the same event, e.g.
    a shooter's SHOT_XG alongside every outfield player's DEF_PXT_SHOT for
    that same shot). kpiId needs get_event_kpi_dictionary() to resolve to a
    name (e.g. SHOT_XG, PACKING_XG) -- this is where event-level xG lives;
    it is NOT present on the plain /events payload at all.

    Args:
        match_id: Match ID
        params: Optional query parameters

    Returns:
        Scoring rows under "data".
    """
    return make_request(f"/v5/customerapi/matches/{match_id}/event-kpis", params)


def get_event_kpi_dictionary(params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get the full list of event-level KPI definitions (GET /kpis/event).

    Confirmed live: 103 KPIs, id->name (e.g. {"id": 1406, "name": "SHOT_XG"}).
    Small and effectively static -- fetch once per run/process, don't refetch
    per match.

    Returns:
        KPI definitions under "data".
    """
    return make_request("/v5/customerapi/kpis/event", params)


def get_match_squad_kpis(match_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get squad KPI data for a specific match

    Args:
        match_id: Match ID
        params: Optional query parameters

    Returns:
        Squad KPI payload
    """
    return make_request(f"/v5/customerapi/matches/{match_id}/squad-kpis", params)


def get_squads(iteration_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get squads for a specific iteration

    Args:
        iteration_id: Iteration ID
        params: Optional query parameters

    Returns:
        Squads data
    """
    return make_request(f"/v5/customerapi/iterations/{iteration_id}/squads", params)


def get_squad_ratings(iteration_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get squad ratings for a specific iteration

    Args:
        iteration_id: Iteration ID
        params: Optional query parameters

    Returns:
        Squad ratings data
    """
    return make_request(f"/v5/customerapi/iterations/{iteration_id}/squads/ratings", params)


def get_players(iteration_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get players for a specific iteration

    Args:
        iteration_id: Iteration ID
        params: Optional query parameters

    Returns:
        Players data
    """
    return make_request(f"/v5/customerapi/iterations/{iteration_id}/players", params)


def get_coaches(iteration_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get coaches for a specific iteration

    Args:
        iteration_id: Iteration ID
        params: Optional query parameters

    Returns:
        Coaches data
    """
    return make_request(f"/v5/customerapi/iterations/{iteration_id}/coaches", params)


def get_stadiums(iteration_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get stadiums for a specific iteration

    Args:
        iteration_id: Iteration ID
        params: Optional query parameters

    Returns:
        Stadiums data
    """
    return make_request(f"/v5/customerapi/iterations/{iteration_id}/stadiums", params)


def get_countries(params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get all countries

    Args:
        params: Optional query parameters

    Returns:
        Countries data
    """
    return make_request("/v5/customerapi/countries", params)


# =============================================================================
#  Iteration-level (season aggregate) endpoints.
#  Added in plan §2.6+ alongside the cross-iteration extract work. These give
#  per-season averages, standardized scores, and positional profile scores —
#  the data that match-level KPIs aggregate up to.
# =============================================================================

def get_iteration_squad_kpis(iteration_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get squad-level KPI averages for a single iteration.

    Args:
        iteration_id: Iteration ID
        params: Optional query parameters

    Returns:
        One row per (squad, kpi) for the iteration. Source-of-truth for
        IMPECT_RAW.ITERATION_SQUAD_KPIS.
    """
    return make_request(f"/v5/customerapi/iterations/{iteration_id}/squad-kpis", params)


def get_iteration_squad_scores(iteration_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get squad-level standardized scores for a single iteration.

    Args:
        iteration_id: Iteration ID
        params: Optional query parameters

    Returns:
        One row per (squad, kpi) with z-scores / percentiles relative to
        IMPECT's cross-league comparison population. Not recomputable from
        match-level data alone. Source-of-truth for IMPECT_RAW.ITERATION_SQUAD_SCORES.
    """
    return make_request(f"/v5/customerapi/iterations/{iteration_id}/squad-scores", params)


def get_iteration_player_kpis(iteration_id: int, squad_id: int,
                              params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get player-level KPI averages for a single (iteration, squad).

    Args:
        iteration_id: Iteration ID
        squad_id: Squad ID (scoped to a single squad's players in this iteration)
        params: Optional query parameters

    Returns:
        One row per (player, kpi). Source-of-truth for IMPECT_RAW.ITERATION_PLAYER_KPIS.
    """
    return make_request(
        f"/v5/customerapi/iterations/{iteration_id}/squads/{squad_id}/player-kpis",
        params,
    )


def get_iteration_player_scores(iteration_id: int, squad_id: int,
                                params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get player-level standardized scores for a single (iteration, squad).

    Args:
        iteration_id: Iteration ID
        squad_id: Squad ID
        params: Optional query parameters

    Returns:
        One row per (player, kpi) with standardized scores. Source-of-truth for
        IMPECT_RAW.ITERATION_PLAYER_SCORES.
    """
    return make_request(
        f"/v5/customerapi/iterations/{iteration_id}/squads/{squad_id}/player-scores",
        params,
    )


def get_iteration_player_profile_scores(iteration_id: int, squad_id: int,
                                        positions: str,
                                        params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get player profile scores filtered to position(s) for a (iteration, squad).

    Args:
        iteration_id: Iteration ID
        squad_id: Squad ID
        positions: IMPECT position code(s). Single code (e.g. "CB") or
                   comma-separated list. Exact codes are TBD — call with a known
                   code to validate before scripting bulk extraction.
        params: Optional query parameters

    Returns:
        One row per (player, profile_score). These are IMPECT's positional
        fitness scores — how well a player's stat distribution matches the
        typical "excellent" profile for those positions. Source-of-truth for
        IMPECT_RAW.ITERATION_PLAYER_PROFILE_SCORES.
    """
    return make_request(
        f"/v5/customerapi/iterations/{iteration_id}/squads/{squad_id}"
        f"/positions/{positions}/player-profile-scores",
        params,
    )


def get_iteration_player_scores_by_position(iteration_id: int, squad_id: int,
                                            positions: str,
                                            params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Get player standardized scores filtered to position(s) for a (iteration, squad).

    Args:
        iteration_id: Iteration ID
        squad_id: Squad ID
        positions: IMPECT position code(s). See get_iteration_player_profile_scores.
        params: Optional query parameters

    Returns:
        One row per (player, kpi) with scores standardized within the position
        comparison population. Source-of-truth for IMPECT_RAW.ITERATION_PLAYER_POSITION_SCORES.
    """
    return make_request(
        f"/v5/customerapi/iterations/{iteration_id}/squads/{squad_id}"
        f"/positions/{positions}/player-scores",
        params,
    )
