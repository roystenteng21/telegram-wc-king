import time
import requests
import logging
from datetime import datetime, timezone, timedelta
from config import (
    FOOTBALL_API_KEY, FOOTBALL_API_BASE, WC_COMPETITION,
    STATUS_SCHEDULED, STATUS_FINISHED, STATUS_CANCELLED, STATUS_POSTPONED,
    UTC
)

logger = logging.getLogger(__name__)

HEADERS = {"X-Auth-Token": FOOTBALL_API_KEY}


def _get(endpoint: str) -> dict | None:
    """Raw GET request. Returns JSON or None on failure."""
    url = f"{FOOTBALL_API_BASE}{endpoint}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 429:
            raise RuntimeError("API rate limit hit (429)")
        if response.status_code == 403:
            raise RuntimeError("API forbidden (403) — check API key")
        response.raise_for_status()
        return response.json()
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"API request failed for {endpoint}: {e}")
        raise RuntimeError(f"API request failed: {e}")


def fetch_matches_for_date(date_str: str) -> list:
    """
    Fetch all WC matches for a given date (YYYY-MM-DD).
    Returns list of normalised match dicts.
    Raises RuntimeError on API failure.
    """
    try:
        data = _get(f"/competitions/{WC_COMPETITION}/matches?dateFrom={date_str}&dateTo={date_str}")
        if not data or "matches" not in data:
            logger.warning(f"No matches data returned for {date_str}")
            return []

        matches = []
        for m in data["matches"]:
            kickoff_utc = m.get("utcDate", "")
            # Normalise to YYYY-MM-DD HH:MM:SS
            try:
                dt = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
                kickoff_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                kickoff_str = kickoff_utc

            status = m.get("status", STATUS_SCHEDULED)
            score = m.get("score", {})
            full_time = score.get("fullTime", {})
            home_score = full_time.get("home")
            away_score = full_time.get("away")

            # Derive result and ou_result if finished
            result = ""
            ou_result = ""
            if status == STATUS_FINISHED and home_score is not None and away_score is not None:
                if home_score > away_score:
                    result = "home"
                elif away_score > home_score:
                    result = "away"
                else:
                    result = "draw"
                total = home_score + away_score
                ou_result = "over" if total > 2 else "under"

            matches.append({
                "match_id": str(m["id"]),
                "home": m.get("homeTeam", {}).get("name", ""),
                "away": m.get("awayTeam", {}).get("name", ""),
                "kickoff_utc": kickoff_str,
                "status": status,
                "home_score": home_score if home_score is not None else "",
                "away_score": away_score if away_score is not None else "",
                "result": result,
                "ou_result": ou_result,
                "matchday": m.get("matchday", ""),
                "round": m.get("stage", "")
            })

        logger.info(f"Fetched {len(matches)} matches for {date_str}")
        return matches

    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"Failed to parse matches for {date_str}: {e}")
        raise RuntimeError(f"Failed to parse match data: {e}")


def fetch_match_result(match_id: str) -> dict | None:
    """
    Poll a single match for its result.
    Returns match dict if FINISHED, None if still in progress.
    Raises RuntimeError on API failure.
    """
    last_error = None
    for attempt in range(1, 4):
        try:
            data = _get(f"/matches/{match_id}")
            if not data:
                return None

            status = data.get("status", "")
            if status != STATUS_FINISHED:
                logger.info(f"Match {match_id} status: {status} — not yet finished")
                return None

            score = data.get("score", {})
            full_time = score.get("fullTime", {})
            home_score = full_time.get("home")
            away_score = full_time.get("away")

            if home_score is None or away_score is None:
                logger.warning(f"Match {match_id} finished but scores missing")
                return None

            if home_score > away_score:
                result = "home"
            elif away_score > home_score:
                result = "away"
            else:
                result = "draw"

            total = home_score + away_score
            ou_result = "over" if total > 2 else "under"

            logger.info(f"Match {match_id} finished: {home_score}-{away_score} ({result}, {ou_result})")
            return {
                "match_id": match_id,
                "home_score": home_score,
                "away_score": away_score,
                "result": result,
                "ou_result": ou_result,
                "status": STATUS_FINISHED
            }

        except RuntimeError as e:
            last_error = e
            if attempt < 3:
                logger.warning(f"fetch_match_result attempt {attempt} failed for {match_id}: {e} — retrying")
                time.sleep(3)
        except Exception as e:
            last_error = RuntimeError(f"Failed to fetch match result: {e}")
            if attempt < 3:
                logger.warning(f"fetch_match_result attempt {attempt} failed for {match_id}: {e} — retrying")
                time.sleep(3)
    raise last_error or RuntimeError(f"fetch_match_result failed after 3 attempts for {match_id}")



def fetch_today_matches() -> list:
    """Fetch today's matches in UTC."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return fetch_matches_for_date(today)


def fetch_standings(group_filter: list = None) -> list:
    """
    Fetch WC group stage standings.
    Returns list of group dicts: {group, table: [{position, team, points, won, draw, lost}]}
    Optionally filter to specific group names e.g. ["GROUP_A", "GROUP_B"].
    Raises RuntimeError on API failure.
    """
    try:
        data = _get(f"/competitions/{WC_COMPETITION}/standings")
        if not data or "standings" not in data:
            logger.warning("No standings data returned")
            return []

        groups = []
        for standing in data["standings"]:
            group = standing.get("group", "")
            if not group:
                continue
            if group_filter and group not in group_filter:
                continue
            table = []
            for row in standing.get("table", []):
                team_name = row.get("team", {}).get("name", "")
                table.append({
                    "position": row.get("position"),
                    "team": team_name,
                    "points": row.get("points", 0),
                    "won": row.get("won", 0),
                    "draw": row.get("draw", 0),
                    "lost": row.get("lost", 0),
                    "played": row.get("playedGames", 0),
                })
            groups.append({"group": group, "table": table})

        logger.info(f"Fetched standings for {len(groups)} groups")
        return groups

    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch standings: {e}")
        raise RuntimeError(f"Failed to fetch standings: {e}")
