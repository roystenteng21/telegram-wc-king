"""
Shared state, helpers, and the Application object.
Imported by bot.py, commands_player.py, commands_admin.py, katerina.py.
"""
import logging
import pytz
from collections import deque
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application

from config import (
    BOT_TOKEN, ADMIN_TELEGRAM_ID, SGT, UTC,
    TEAM_ALIASES, FUZZY_THRESHOLD, TEAM_DISPLAY,
    SESSION_EXPIRY, BET_LOCK_BUFFER, PRIZE_PLAYER_COUNT
)
from rapidfuzz import process, fuzz
import sheet

logger = logging.getLogger(__name__)

# ── Application object ────────────────────────────────────────────────────────
application = Application.builder().token(BOT_TOKEN).build()

# ── Shared state ──────────────────────────────────────────────────────────────
_group_chat_id: int | None = None

_sessions: dict[int, dict] = {}
_pending_bets: dict[int, dict] = {}
_admin_pending: dict[int, dict] = {}
_chat_history: deque = deque(maxlen=50)


# ── Group chat ID ─────────────────────────────────────────────────────────────
def get_group_chat_id() -> int | None:
    return _group_chat_id

def set_group_chat_id(gid: int):
    global _group_chat_id
    _group_chat_id = gid


# ── Admin DM ──────────────────────────────────────────────────────────────────
async def dm_admin(message: str):
    try:
        await application.bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=message)
    except Exception as e:
        logger.error(f"Failed to DM admin: {e}")


# ── Silent hours ──────────────────────────────────────────────────────────────
def is_silent_hours() -> bool:
    """Returns True if current SGT time is in silent hours AND toggle is on."""
    if sheet.cache.get("silent_hours_disabled", False):
        return False
    now_sgt = datetime.now(SGT)
    start = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now_sgt.replace(hour=7, minute=30, second=0, microsecond=0)
    return start <= now_sgt < end


# ── Send confirmation (DM during silent hours) ────────────────────────────────
async def send_confirmation(update, message: str):
    user = update.effective_user
    if is_silent_hours():
        try:
            dm_message = message + "\n\n🔕 Sent here to minimise group notifications (12AM–7:30AM SGT)."
            await application.bot.send_message(chat_id=user.id, text=dm_message)
            return
        except Exception:
            pass
    await update.message.reply_text(message)


# ── Group check ───────────────────────────────────────────────────────────────
def is_group_message(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


# ── Display helpers ───────────────────────────────────────────────────────────
def get_display_name(user) -> str:
    return (user.first_name or user.username or "Unknown")[:10]


def format_team(name: str) -> str:
    if name in TEAM_DISPLAY:
        code, flag = TEAM_DISPLAY[name]
        return f"{flag} {code}"
    logger.warning(f"Team not in TEAM_DISPLAY: '{name}'")
    return name[:3].upper()


def format_match_teams(home: str, away: str) -> str:
    return f"{format_team(home)} vs {format_team(away)}"


def format_outcome_label(outcome: str, match: dict) -> str:
    if outcome == "draw":
        return "Draw"
    if outcome == "over":
        return "Over 2.5"
    if outcome == "under":
        return "Under 2.5"
    if outcome == "home":
        team = match["home"]
    elif outcome == "away":
        team = match["away"]
    else:
        return outcome.capitalize()
    if team in TEAM_DISPLAY:
        code, _ = TEAM_DISPLAY[team]
        return f"{code} Win"
    return f"{team[:3].upper()} Win"


def truncate(name: str, length: int = 10) -> str:
    return name[:length]


# ── Session helpers ───────────────────────────────────────────────────────────
def session_expired(session: dict) -> bool:
    return datetime.now(UTC) > session["expires"]

def clear_session(user_id: int):
    _sessions.pop(user_id, None)

def clear_pending_bet(user_id: int):
    _pending_bets.pop(user_id, None)

def clear_admin_pending():
    _admin_pending.pop(ADMIN_TELEGRAM_ID, None)


# ── User registration ─────────────────────────────────────────────────────────
async def ensure_registered(update: Update) -> dict | None:
    user = update.effective_user
    existing = await sheet.get_user(user.id)
    if not existing:
        existing = await sheet.register_user(
            user.id, user.username or "", user.first_name or "",
            is_admin=(user.id == ADMIN_TELEGRAM_ID),
            notify_fn=dm_admin
        )
    else:
        await sheet.refresh_display_name(user.id, user.username or "", user.first_name or "", notify_fn=dm_admin)
    return existing


# ── Team resolution ───────────────────────────────────────────────────────────
def resolve_team(team_input: str) -> tuple[str | None, bool]:
    normalised = team_input.lower().strip()
    if normalised in TEAM_ALIASES:
        resolved = TEAM_ALIASES[normalised]
        if resolved is None:
            return None, True
        return resolved, False
    known_teams = list(set(
        name
        for m in sheet.cache["matches"].values()
        for name in [m["home"], m["away"]]
        if name
    ))
    if not known_teams:
        return None, False
    result = process.extractOne(team_input, known_teams, scorer=fuzz.WRatio)
    if result and result[1] >= FUZZY_THRESHOLD:
        return result[0], False
    return None, False


def find_match_for_team(team_name: str) -> dict | None:
    now_utc = datetime.now(UTC)
    for m in sheet.cache["matches"].values():
        if team_name.lower() in (m["home"].lower(), m["away"].lower()):
            try:
                kickoff = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except Exception:
                continue
            lock_time = kickoff + timedelta(seconds=BET_LOCK_BUFFER)
            if now_utc < lock_time and m["status"] in ("SCHEDULED", "TIMED"):
                return m
    return None


def map_outcome_to_result(outcome: str, match: dict, team_name: str) -> str | None:
    outcome = outcome.lower()
    if outcome == "draw":
        return "draw"
    if outcome == "over":
        return "over"
    if outcome == "under":
        return "under"
    home_team = match["home"].lower()
    named_team = team_name.lower()
    if outcome == "win":
        return "home" if named_team == home_team else "away"
    if outcome == "loss":
        return "away" if named_team == home_team else "home"
    return None


def get_market_for_outcome(outcome: str) -> str:
    if outcome in ("over", "under"):
        return "ou"
    return "result"
