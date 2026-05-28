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


def make_request(endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Make a GET request to the Impect API

    Args:
        endpoint: API endpoint path (e.g., '/v5/customerapi/iterations')
        params: Optional query parameters

    Returns:
        JSON response as dictionary
    """
    url = f"{config.IMPECT_BASE_URL}{endpoint}"
    headers = get_auth_headers()
    session = get_http_session()

    for attempt in range(config.MAX_RETRIES):
        try:
            response = session.get(
                url,
                headers=headers,
                params=params,
                timeout=config.REQUEST_TIMEOUT
            )

            # Handle rate limiting (429 Too Many Requests)
            if response.status_code == 429:
                print("Rate limit hit (429), waiting 1 second...")
                time.sleep(1)
                continue

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            if attempt == config.MAX_RETRIES - 1:
                raise Exception(f"Failed to fetch data from {endpoint}: {str(e)}")
            time.sleep(2 ** attempt)  # Exponential backoff

    return {}


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
