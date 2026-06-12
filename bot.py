import logging
import asyncio
import pytz
from datetime import datetime, timedelta
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ChatMemberHandler, ContextTypes, filters
)
from rapidfuzz import process, fuzz

from config import (
    BOT_TOKEN, ADMIN_TELEGRAM_ID, BOT_VERSION,
    TEAM_ALIASES, FUZZY_THRESHOLD, TEAM_DISPLAY,
    RESULT_OUTCOMES, OU_OUTCOMES, ALL_OUTCOMES,
    SESSION_EXPIRY, SGT, UTC,
    DAILY_CREDITS, BET_LOCK_BUFFER
)
import sheet
import scheduler as sched
import api

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────
# Group chat ID — auto-locked on first message
_group_chat_id: int | None = None

# Per-user session state for multi-step flows
# { user_id: { "action": str, "data": dict, "expires": datetime } }
_sessions: dict[int, dict] = {}

# Pending fuzzy confirmations
# { user_id: { "team": str, "outcome": str, "amount": int, "expires": datetime } }
_pending_bets: dict[int, dict] = {}

# Admin confirmation state
# { admin_id: { "action": str, "data": dict, "expires": datetime } }
_admin_pending: dict[int, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────
async def dm_admin(message: str):
    try:
        await application.bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=message)
    except Exception as e:
        logger.error(f"Failed to DM admin: {e}")


def is_silent_hours() -> bool:
    now_sgt = datetime.now(SGT)
    start = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now_sgt.replace(hour=7, minute=30, second=0, microsecond=0)
    return start <= now_sgt < end


def is_group_message(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


def get_display_name(user) -> str:
    name = user.first_name or user.username or "Unknown"
    return name[:10]


def format_team(name: str) -> str:
    """Returns 'FLAG CODE' e.g. '🇲🇽 MEX' for display."""
    if name in TEAM_DISPLAY:
        code, flag = TEAM_DISPLAY[name]
        return f"{flag} {code}"
    return name[:3].upper()


def format_match_teams(home: str, away: str) -> str:
    """Returns 'FLAG CODE vs FLAG CODE' e.g. '🇲🇽 MEX vs 🇿🇦 RSA'."""
    return f"{format_team(home)} vs {format_team(away)}"


def format_outcome_label(outcome: str, match: dict) -> str:
    """Convert internal outcome to display label e.g. 'MEX Win', 'Draw', 'Over 2.5'."""
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


def session_expired(session: dict) -> bool:
    return datetime.now(UTC) > session["expires"]


def clear_session(user_id: int):
    _sessions.pop(user_id, None)


def clear_pending_bet(user_id: int):
    _pending_bets.pop(user_id, None)


def clear_admin_pending():
    _admin_pending.pop(ADMIN_TELEGRAM_ID, None)


async def ensure_registered(update: Update) -> dict | None:
    """Register user if not exists. Returns user dict."""
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


def resolve_team(team_input: str) -> tuple[str | None, bool]:
    """
    Resolve team name from input.
    Returns (team_name, is_ambiguous).
    team_name is None if no match found.
    is_ambiguous is True if hardcoded alias is ambiguous.
    """
    normalised = team_input.lower().strip()

    # Check hardcoded aliases first
    if normalised in TEAM_ALIASES:
        resolved = TEAM_ALIASES[normalised]
        if resolved is None:
            return None, True  # ambiguous (Guinea / Congo)
        return resolved, False

    # Get all known team names from today's matches
    known_teams = list(set(
        name
        for m in sheet.cache["matches"].values()
        for name in [m["home"], m["away"]]
        if name
    ))

    if not known_teams:
        return None, False

    # Fuzzy match
    result = process.extractOne(team_input, known_teams, scorer=fuzz.WRatio)
    if result and result[1] >= FUZZY_THRESHOLD:
        return result[0], False

    return None, False


def find_match_for_team(team_name: str) -> dict | None:
    """Find the upcoming/active match for a given team name."""
    now_utc = datetime.now(UTC)
    for m in sheet.cache["matches"].values():
        if team_name.lower() in (m["home"].lower(), m["away"].lower()):
            try:
                kickoff = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except Exception:
                continue
            # Match is upcoming or within bet lock window
            lock_time = kickoff + timedelta(seconds=BET_LOCK_BUFFER)
            if now_utc < lock_time and m["status"] in ("SCHEDULED", "TIMED"):
                return m
    return None


def map_outcome_to_result(outcome: str, match: dict, team_name: str) -> str | None:
    """
    Map user-facing outcome (win/loss/draw/over/under) to internal outcome.
    win/loss are relative to the team the user named.
    """
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


# ── Auto-lock group chat ID ───────────────────────────────────────────────────
async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _group_chat_id
    if update.effective_chat.type in ("group", "supergroup"):
        if _group_chat_id is None:
            _group_chat_id = update.effective_chat.id
            sched.init(context.bot, _group_chat_id)
            logger.info(f"Group chat ID locked: {_group_chat_id}")
            await dm_admin(f"✅ Degen locked to group chat ID: {_group_chat_id}")
            # Save to sheet for persistence across restarts
            try:
                ws = sheet.get_sheet("users")
                # Store in a reserved row or use a separate config mechanism
                # For now just log it — admin should set GROUP_CHAT_ID env var after first run
            except Exception:
                pass
        elif update.effective_chat.id != _group_chat_id:
            return  # ignore other chats


# ── Welcome new members ───────────────────────────────────────────────────────
async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != _group_chat_id:
        return

    result = update.chat_member
    new_member = result.new_chat_member.user

    if new_member.is_bot:
        return

    await sheet.register_user(
        new_member.id,
        new_member.username or "",
        new_member.first_name or "",
        is_admin=(new_member.id == ADMIN_TELEGRAM_ID),
        notify_fn=dm_admin
    )

    name = truncate(new_member.first_name or new_member.username or "there")
    welcome = (
        f"👋 Welcome to WC Kings 2026, {name}!\n\n"
        f"You've been registered with {DAILY_CREDITS} credits. "
        f"Type /help to see all commands.\n\n"
        f"📩 One thing — start a private chat with me so I can send you "
        f"bet confirmations during quiet hours. Tap my name above → Start.\n\n"
        f"Good luck! 🍀"
    )
    await context.bot.send_message(chat_id=_group_chat_id, text=welcome)


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_group_message(update):
        return  # ignore /start in group

    # Register user if not exists (handles case where they DM before joining group)
    await sheet.register_user(
        user.id, user.username or "", user.first_name or "",
        is_admin=(user.id == ADMIN_TELEGRAM_ID),
        notify_fn=dm_admin
    )
    await update.message.reply_text(
        f"👋 Hey {truncate(user.first_name or user.username or 'there')}! Private chat activated.\n\n"
        f"How to bet:\n"
        f"/bet [team] [outcome] [amount]\n\n"
        f"Outcomes:\n"
        f"• win / loss / draw\n"
        f"• over / under (2.5 goals)\n\n"
        f"Example: /bet mexico win 50\n\n"
        f"Payouts are 1:1. Win 50c, get 100c back.\n"
        f"You start with 100 credits, and get 100 more every day after the last match. Unused credits roll over.\n\n"
        f"During quiet hours (12AM–7:30AM SGT), I'll send your bet confirmations here instead of the group.\n\n"
        f"Good luck! 🍀"
    )


# ── /help ─────────────────────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 Degen — WC Kings 2026\n\n"
        "/matches — Today's matches + kickoff times\n"
        "/bet [team] [win|loss|draw|over|under] [amount] — Place a bet\n"
        "/mybets — Your open bets\n"
        "/cancel — Cancel an open bet\n"
        "/balance — Your current credits\n"
        "/leaderboard — Full standings\n"
        "/help — This message\n\n"
        "Bets lock at kickoff. All times in SGT."
    )
    await update.message.reply_text(text)


# ── /matches ──────────────────────────────────────────────────────────────────
async def cmd_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    CT = pytz.timezone("America/Chicago")
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")

    # Fetch today and tomorrow in UTC to capture late US kickoffs crossing UTC midnight
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

    # Filter by CT date — include all statuses
    day_matches = []
    for m in all_matches:
        try:
            kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today_ct:
                day_matches.append(m)
        except Exception:
            continue

    if not day_matches:
        await update.message.reply_text("No matches today.")
        return

    lines = ["📅 Today's Matches\n"]
    for m in sorted(day_matches, key=lambda x: x["kickoff_utc"]):
        kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        kickoff_sgt = kickoff_utc_dt.astimezone(SGT)
        time_str = kickoff_sgt.strftime("%I:%M %p SGT").lstrip("0")

        status = m.get("status", "")
        if status == "FINISHED":
            hs = m.get("home_score", "")
            as_ = m.get("away_score", "")
            status_str = f"FINISHED • {hs}-{as_}"
        elif status in ("SCHEDULED", "TIMED"):
            now = datetime.now(UTC)
            diff = kickoff_utc_dt - now
            mins = int(diff.total_seconds() // 60)
            if mins > 60:
                hrs = mins // 60
                status_str = f"Kickoff in {hrs}h"
            elif mins > 0:
                status_str = f"Kickoff in {mins}m"
            else:
                status_str = "Starting soon"
        else:
            status_str = status

        lines.append(f"{format_match_teams(m['home'], m['away'])}")
        lines.append(f"🕙 {time_str} • {status_str}")

        # Bets for this match
        match_bets = await sheet.get_bets_for_match(m["match_id"])
        open_or_settled = [b for b in match_bets if b["status"] in ("open", "won", "lost")]
        if open_or_settled:
            lines.append("Bets:")
            for b in open_or_settled:
                user = sheet.cache["users"].get(b["user_id"])
                name = truncate(user.get("first_name") or user.get("username") or "?") if user else "?"
                outcome_label = format_outcome_label(b["outcome"], m)
                if b["status"] == "won":
                    icon = " ✅"
                elif b["status"] == "lost":
                    icon = " ❌"
                else:
                    icon = ""
                lines.append(f"• {name} — {outcome_label} — {b['amount']}c{icon}")

        lines.append("")  # blank line between matches

    await update.message.reply_text("\n".join(lines))


# ── /balance ──────────────────────────────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await ensure_registered(update)
    credits = user_data["credits"]
    name = truncate(update.effective_user.first_name or "")
    await update.message.reply_text(f"💰 {name}, your balance: {credits} credits")


# ── /leaderboard ──────────────────────────────────────────────────────────────
async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    standings = sheet.get_standings()
    if not standings:
        await update.message.reply_text("No players yet.")
        return

    lines = ["🏆 Leaderboard\n"]
    for i, user in enumerate(standings, 1):
        name = truncate(user.get("first_name") or user.get("username") or "Unknown")
        badge = " 🏆" if i == 1 else ""
        lines.append(f"{i}. {name}{badge} — {user['credits']}c")

    await update.message.reply_text("\n".join(lines))


# ── /mybets ───────────────────────────────────────────────────────────────────
async def cmd_mybets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    user_id = update.effective_user.id
    open_bets = await sheet.get_user_open_bets(user_id)

    if not open_bets:
        await update.message.reply_text("You have no open bets.")
        return

    lines = ["📋 Your open bets:\n"]
    for i, bet in enumerate(open_bets, 1):
        match = await sheet.get_match_by_id(bet["match_id"])
        if match:
            outcome_label = format_outcome_label(bet["outcome"], match)
            if bet["outcome"] in ("home", "away"):
                team = match["home"] if bet["outcome"] == "home" else match["away"]
                flag = TEAM_DISPLAY[team][1] if team in TEAM_DISPLAY else ""
                label = f"{flag} {outcome_label}"
            else:
                home = format_team(match["home"])
                away = format_team(match["away"])
                label = f"{home} vs {away} — {outcome_label}"
        else:
            label = f"Match {bet['match_id']} — {bet['outcome'].capitalize()}"
        lines.append(f"{i}. {label} — {bet['amount']}c")

    lines.append("\nUse /cancel to cancel a bet.")
    await update.message.reply_text("\n".join(lines))


# ── /bet ──────────────────────────────────────────────────────────────────────
async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_message(update):
        await update.message.reply_text("Please use this command in the group.")
        return

    user = update.effective_user
    user_data = await ensure_registered(update)
    args = context.args

    if len(args) < 3:
        await update.message.reply_text("Usage: /bet [team] [win|loss|draw|over|under] [amount]")
        return

    team_input = args[0]
    outcome_input = args[1].lower()
    amount_input = args[2].lower().replace("c", "")

    # Validate outcome
    if outcome_input not in ALL_OUTCOMES:
        await update.message.reply_text(
            f"Invalid outcome. Use: win, loss, draw, over, under"
        )
        return

    # Validate amount
    try:
        amount = int(float(amount_input))  # int(float()) handles "50.7" → 50
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid amount (e.g. /bet brazil win 50)")
        return

    # Check credits
    if user_data["credits"] < amount:
        await update.message.reply_text(
            f"Insufficient credits. Your balance: {user_data['credits']}c"
        )
        return

    # Resolve team
    team_name, is_ambiguous = resolve_team(team_input)

    if is_ambiguous:
        await update.message.reply_text(
            f"'{team_input}' could be multiple teams. Please be more specific (e.g. 'Guinea-Bissau' or 'Guinea')."
        )
        return

    if not team_name:
        await update.message.reply_text(
            f"Couldn't find '{team_input}'. Check /matches for today's teams."
        )
        return

    # Find match
    match = find_match_for_team(team_name)
    if not match:
        await update.message.reply_text(
            f"No upcoming match found for {team_name}, or betting is already closed."
        )
        return

    # Map outcome to internal value
    internal_outcome = map_outcome_to_result(outcome_input, match, team_name)
    if not internal_outcome:
        await update.message.reply_text("Invalid outcome for this match.")
        return

    market = get_market_for_outcome(internal_outcome)

    # Check if team name was fuzzy matched (below exact) — no extra confirm needed above threshold
    # Already confirmed by resolve_team returning a value above FUZZY_THRESHOLD
    # Place bet
    try:
        bet_id = await sheet.place_bet(
            user_id=user.id,
            match_id=match["match_id"],
            market=market,
            outcome=internal_outcome,
            amount=amount,
            notify_fn=dm_admin
        )

        home = format_team(match["home"])
        away = format_team(match["away"])
        outcome_label = outcome_input.capitalize()
        new_balance = sheet.cache["users"][user.id]["credits"]

        confirm_msg = (
            f"✅ Bet placed!\n"
            f"{home} vs {away}\n"
            f"{outcome_label} — {amount}c\n"
            f"Balance: {new_balance}c"
        )

        # During silent hours — DM only
        if is_silent_hours():
            try:
                await application.bot.send_message(chat_id=user.id, text=confirm_msg)
            except Exception:
                # Can't DM — reply in group quietly
                await update.message.reply_text(confirm_msg)
        else:
            await update.message.reply_text(confirm_msg)

    except ValueError as e:
        await update.message.reply_text(str(e))
    except Exception as e:
        logger.error(f"Bet placement failed for {user.id}: {e}")
        await update.message.reply_text("Something went wrong placing your bet. Please try again.")
        await dm_admin(f"⚠️ Bet placement failed for user {user.id}: {e}")


# ── /cancelbet ────────────────────────────────────────────────────────────────
async def cmd_cancelbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    user_id = update.effective_user.id
    args = context.args

    open_bets = await sheet.get_user_open_bets(user_id)
    if not open_bets:
        await update.message.reply_text("You have no open bets to cancel.")
        return

    # /cancel [number] — cancel specific bet
    if args:
        # Check for existing session
        session = _sessions.get(user_id)
        if not session or session_expired(session) or session.get("action") != "cancelbet":
            await update.message.reply_text("Please run /cancel first to see your bets.")
            return

        clear_session(user_id)

        try:
            index = int(args[0]) - 1
            if index < 0 or index >= len(session["data"]["bets"]):
                await update.message.reply_text("Invalid number. Run /cancel to see your bets.")
                return
        except ValueError:
            await update.message.reply_text("Usage: /cancel [number]")
            return

        bet = session["data"]["bets"][index]

        # Check bet still open and match not locked
        match = await sheet.get_match_by_id(bet["match_id"])
        if match:
            try:
                kickoff = datetime.strptime(match["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                lock_time = kickoff + timedelta(seconds=BET_LOCK_BUFFER)
                if datetime.now(UTC) >= lock_time:
                    await update.message.reply_text("Betting for this match is closed. Bet cannot be cancelled.")
                    return
            except Exception:
                pass

        try:
            await sheet.cancel_bet(bet["bet_id"], user_id, notify_fn=dm_admin)
            new_balance = sheet.cache["users"][user_id]["credits"]
            if match:
                outcome_label = format_outcome_label(bet["outcome"], match)
                if bet["outcome"] in ("home", "away"):
                    team = match["home"] if bet["outcome"] == "home" else match["away"]
                    flag = TEAM_DISPLAY[team][1] if team in TEAM_DISPLAY else ""
                    match_line = f"{flag} {outcome_label}"
                else:
                    home = format_team(match["home"])
                    away = format_team(match["away"])
                    match_line = f"{home} vs {away} — {outcome_label}"
            else:
                outcome_label = bet["outcome"].capitalize()
                match_line = f"Match {bet['match_id']} — {outcome_label}"
            await update.message.reply_text(
                f"✅ Bet cancelled.\n"
                f"{match_line} — {bet['amount']}c refunded.\n"
                f"Balance: {new_balance}c"
            )
        except Exception as e:
            await update.message.reply_text("Failed to cancel bet. Please try again.")
            await dm_admin(f"⚠️ Cancel bet failed for user {user_id}: {e}")
        return

    # /cancel with no args — list open bets
    lines = ["📋 Your open bets:\n"]
    for i, bet in enumerate(open_bets, 1):
        match = await sheet.get_match_by_id(bet["match_id"])
        if match:
            outcome_label = format_outcome_label(bet["outcome"], match)
            if bet["outcome"] in ("home", "away"):
                team = match["home"] if bet["outcome"] == "home" else match["away"]
                flag = TEAM_DISPLAY[team][1] if team in TEAM_DISPLAY else ""
                label = f"{flag} {outcome_label}"
            else:
                home = format_team(match["home"])
                away = format_team(match["away"])
                label = f"{home} vs {away} — {outcome_label}"
        else:
            label = f"Match {bet['match_id']} — {bet['outcome'].capitalize()}"
        lines.append(f"{i}. {label} — {bet['amount']}c")

    lines.append("\nReply /cancel [number] to cancel.")

    # Store session
    _sessions[user_id] = {
        "action": "cancelbet",
        "data": {"bets": open_bets},
        "expires": datetime.now(UTC) + timedelta(seconds=SESSION_EXPIRY)
    }

    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_status ──────────────────────────────────────────────────────
async def cmd_admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    jobs = sched.scheduler.get_jobs()
    last_refresh = sheet.cache.get("last_refresh")
    refresh_str = last_refresh.strftime("%H:%M:%S UTC") if last_refresh else "Never"

    text = (
        f"✅ Degen v{BOT_VERSION} is running\n"
        f"Sheet: Connected\n"
        f"Cache: {len(sheet.cache['users'])} users, "
        f"{len(sheet.cache['matches'])} matches, "
        f"{len(sheet.cache['bets'])} bets\n"
        f"Scheduler: {len(jobs)} active jobs\n"
        f"Last cache refresh: {refresh_str}\n"
        f"Group chat ID: {_group_chat_id}"
    )
    await update.message.reply_text(text)


# ── Admin: /admin_refresh ─────────────────────────────────────────────────────
async def cmd_admin_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    await update.message.reply_text("Refreshing fixtures from API...")
    try:
        matches = api.fetch_today_matches()
        for m in matches:
            await sheet.upsert_match(m, notify_fn=dm_admin)
        await sheet.refresh_cache(notify_fn=dm_admin)
        sched.register_match_jobs(matches)
        await update.message.reply_text(f"✅ Done. {len(matches)} matches loaded.")
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ API error: {e}")


# ── Admin: /admin_result ──────────────────────────────────────────────────────
async def cmd_admin_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args

    # Step 1: /admin_result — list pending matches
    if not args:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        matches = await sheet.get_matches_for_date(today)
        pending = [m for m in matches if m["status"] != "FINISHED"]
        if not pending:
            await update.message.reply_text("No pending matches today.")
            return

        lines = ["Which match to update?\n"]
        for i, m in enumerate(pending, 1):
            home = format_team(m["home"])
            away = format_team(m["away"])
            lines.append(f"{i}. {home} vs {away} — {m['status']}")
        lines.append("\nReply /admin_result [number]")

        _admin_pending[ADMIN_TELEGRAM_ID] = {
            "action": "result_select",
            "data": {"matches": pending},
            "expires": datetime.now(UTC) + timedelta(seconds=120)
        }
        await update.message.reply_text("\n".join(lines))
        return

    # Step 2: /admin_result [number] — select match
    pending = _admin_pending.get(ADMIN_TELEGRAM_ID)
    if pending and pending["action"] == "result_select" and not session_expired(pending):
        try:
            index = int(args[0]) - 1
            matches = pending["data"]["matches"]
            if index < 0 or index >= len(matches):
                await update.message.reply_text("Invalid number.")
                return
            selected = matches[index]
            home = format_team(selected["home"])
            away = format_team(selected["away"])
            _admin_pending[ADMIN_TELEGRAM_ID] = {
                "action": "result_score",
                "data": {"match": selected},
                "expires": datetime.now(UTC) + timedelta(seconds=120)
            }
            await update.message.reply_text(
                f"Enter score for {home} vs {away}\n"
                f"Format: /admin_result [home_score] [away_score]\n"
                f"Example: /admin_result 2 0"
            )
            return
        except ValueError:
            pass

    # Step 3: /admin_result [home] [away] — enter score
    if pending and pending["action"] == "result_score" and not session_expired(pending):
        try:
            home_score = int(args[0])
            away_score = int(args[1]) if len(args) > 1 else None
            if away_score is None:
                raise ValueError
        except (ValueError, IndexError):
            await update.message.reply_text("Invalid scores. Format: /admin_result [home_score] [away_score]")
            return

        match = pending["data"]["match"]
        home = format_team(match["home"])
        away = format_team(match["away"])

        total = home_score + away_score
        if home_score > away_score:
            result_label = f"{home} Win"
        elif away_score > home_score:
            result_label = f"{away} Win"
        else:
            result_label = "Draw"
        ou_label = "Over 2.5" if total > 2 else "Under 2.5"

        _admin_pending[ADMIN_TELEGRAM_ID] = {
            "action": "result_confirm",
            "data": {
                "match": match,
                "home_score": home_score,
                "away_score": away_score
            },
            "expires": datetime.now(UTC) + timedelta(seconds=120)
        }
        await update.message.reply_text(
            f"Confirm: {home} vs {away} — {home_score}–{away_score}\n"
            f"Result: {result_label} · {ou_label}\n\n"
            f"Settle all bets? /confirm_admin or /cancel_admin"
        )
        return

    await update.message.reply_text("Run /admin_result to start.")


# ── Admin: /admin_cancel_match ────────────────────────────────────────────────
async def cmd_admin_cancel_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args

    if not args:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        matches = await sheet.get_matches_for_date(today)
        active = [m for m in matches if m["status"] in ("SCHEDULED", "TIMED")]
        if not active:
            await update.message.reply_text("No scheduled matches to cancel.")
            return

        lines = ["Which match to cancel/postpone?\n"]
        for i, m in enumerate(active, 1):
            home = format_team(m["home"])
            away = format_team(m["away"])
            lines.append(f"{i}. {home} vs {away}")
        lines.append("\nReply /admin_cancel_match [number]")

        _admin_pending[ADMIN_TELEGRAM_ID] = {
            "action": "cancel_select",
            "data": {"matches": active},
            "expires": datetime.now(UTC) + timedelta(seconds=120)
        }
        await update.message.reply_text("\n".join(lines))
        return

    pending = _admin_pending.get(ADMIN_TELEGRAM_ID)
    if pending and pending["action"] == "cancel_select" and not session_expired(pending):
        try:
            index = int(args[0]) - 1
            matches = pending["data"]["matches"]
            if index < 0 or index >= len(matches):
                await update.message.reply_text("Invalid number.")
                return
            selected = matches[index]
            home = format_team(selected["home"])
            away = format_team(selected["away"])

            _admin_pending[ADMIN_TELEGRAM_ID] = {
                "action": "cancel_confirm",
                "data": {"match": selected},
                "expires": datetime.now(UTC) + timedelta(seconds=120)
            }
            await update.message.reply_text(
                f"Confirm: Cancel {home} vs {away}?\n"
                f"All bets will be voided and credits refunded.\n\n"
                f"/confirm_admin or /cancel_admin"
            )
        except ValueError:
            await update.message.reply_text("Invalid number.")
    else:
        await update.message.reply_text("Run /admin_cancel_match to start.")


# ── Admin: /admin_credits ─────────────────────────────────────────────────────
async def cmd_admin_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args

    if not args:
        standings = sheet.get_standings()
        if not standings:
            await update.message.reply_text("No players registered.")
            return

        lines = ["Which player to adjust?\n"]
        for i, user in enumerate(standings, 1):
            name = truncate(user.get("first_name") or user.get("username") or "Unknown")
            lines.append(f"{i}. {name} — {user['credits']}c")
        lines.append("\nReply /admin_credits [number] [amount]\nPositive to add, negative to deduct.")

        _admin_pending[ADMIN_TELEGRAM_ID] = {
            "action": "credits_select",
            "data": {"users": standings},
            "expires": datetime.now(UTC) + timedelta(seconds=120)
        }
        await update.message.reply_text("\n".join(lines))
        return

    pending = _admin_pending.get(ADMIN_TELEGRAM_ID)
    if pending and pending["action"] == "credits_select" and not session_expired(pending):
        try:
            index = int(args[0]) - 1
            amount = int(args[1]) if len(args) > 1 else None
            if amount is None:
                raise ValueError
            users = pending["data"]["users"]
            if index < 0 or index >= len(users):
                await update.message.reply_text("Invalid number.")
                return
            selected_user = users[index]
            name = truncate(selected_user.get("first_name") or selected_user.get("username") or "Unknown")
            old = selected_user["credits"]
            new = max(0, old + amount)
            direction = "Add" if amount >= 0 else "Deduct"

            _admin_pending[ADMIN_TELEGRAM_ID] = {
                "action": "credits_confirm",
                "data": {
                    "user": selected_user,
                    "amount": amount,
                    "new_credits": new
                },
                "expires": datetime.now(UTC) + timedelta(seconds=120)
            }
            await update.message.reply_text(
                f"Confirm: {direction} {abs(amount)}c to {name}? ({old} → {new})\n\n"
                f"/confirm_admin or /cancel_admin"
            )
        except (ValueError, IndexError):
            await update.message.reply_text("Format: /admin_credits [number] [amount]")
    else:
        await update.message.reply_text("Run /admin_credits to start.")


# ── Admin: /confirm_admin ─────────────────────────────────────────────────────
async def cmd_confirm_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    pending = _admin_pending.get(ADMIN_TELEGRAM_ID)
    if not pending or session_expired(pending):
        await update.message.reply_text("No pending admin action. Session may have expired.")
        return

    action = pending["action"]
    data = pending["data"]
    clear_admin_pending()

    # Settle match result
    if action == "result_confirm":
        match = data["match"]
        home_score = data["home_score"]
        away_score = data["away_score"]
        try:
            result, ou_result = await sheet.update_match_result(
                match["match_id"], home_score, away_score, notify_fn=dm_admin
            )
            settlements = await sheet.settle_bets_for_match(
                match["match_id"], result, ou_result, notify_fn=dm_admin
            )
            updated_match = await sheet.get_match_by_id(match["match_id"])
            result_msg = sched.format_result_message(updated_match, settlements)

            if not sched.is_silent_hours():
                await sched.send_group(result_msg)

            await update.message.reply_text(f"✅ Done. {len(settlements)} bets settled.")
            await sched.check_all_matches_done()
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to settle: {e}")

    # Cancel match
    elif action == "cancel_confirm":
        match = data["match"]
        try:
            count = await sheet.void_all_bets_for_match(match["match_id"], notify_fn=dm_admin)
            home = format_team(match["home"])
            away = format_team(match["away"])
            await sched.send_group(f"⚠️ {home} vs {away} has been cancelled. All bets refunded.")
            await update.message.reply_text(f"✅ Done. {count} bets voided, credits refunded.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to cancel match: {e}")

    # Adjust credits
    elif action == "credits_confirm":
        user = data["user"]
        new_credits = data["new_credits"]
        amount = data["amount"]
        try:
            await sheet.update_user_credits(user["user_id"], new_credits, notify_fn=dm_admin)
            await sheet.append_ledger(
                user["user_id"], "admin_adjustment", amount, new_credits,
                "Admin manual adjustment", notify_fn=dm_admin
            )
            name = truncate(user.get("first_name") or user.get("username") or "Unknown")
            await update.message.reply_text(f"✅ Done. {name}: {new_credits}c")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to adjust credits: {e}")

    else:
        await update.message.reply_text("Unknown pending action.")


# ── Admin: /cancel_admin ──────────────────────────────────────────────────────
async def cmd_cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    clear_admin_pending()
    await update.message.reply_text("Admin action cancelled.")


# ── Admin: /admin_endtournament ───────────────────────────────────────────────
async def cmd_admin_endtournament(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    pending = _admin_pending.get(ADMIN_TELEGRAM_ID)
    if not pending or pending.get("action") != "endtournament_confirm":
        _admin_pending[ADMIN_TELEGRAM_ID] = {
            "action": "endtournament_confirm",
            "data": {},
            "expires": datetime.now(UTC) + timedelta(seconds=120)
        }
        await update.message.reply_text(
            "Confirm: Post final tournament standings and end WC Kings 2026?\n\n"
            "/confirm_admin or /cancel_admin"
        )
        return

    if session_expired(pending):
        await update.message.reply_text("Session expired. Run /admin_endtournament again.")
        return

    clear_admin_pending()
    standings = sheet.get_standings()
    if not standings:
        await update.message.reply_text("No players found.")
        return

    lines = ["🏆 WC Kings 2026 — Final Standings\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, user in enumerate(standings, 1):
        name = truncate(user.get("first_name") or user.get("username") or "Unknown")
        medal = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{medal} {name} — {user['credits']}c")

    winner = truncate(standings[0].get("first_name") or standings[0].get("username") or "Unknown")
    lines.append(f"\nCongratulations {winner}! 🎉")
    lines.append("Thanks for playing WC Kings 2026! ⚽")

    await sched.send_group("\n".join(lines))
    await update.message.reply_text("✅ Final standings posted.")


# ── Application setup ─────────────────────────────────────────────────────────
application = Application.builder().token(BOT_TOKEN).build()


def setup_handlers():
    # Group message listener (for chat ID lock)
    # Group chat ID lock — runs in parallel group so it never blocks commands
    application.add_handler(MessageHandler(filters.ALL, handle_any_message), group=1)

    # New member
    application.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))

    # Commands — group 0 (default, higher priority)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("matches", cmd_matches))
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    application.add_handler(CommandHandler("mybets", cmd_mybets))
    application.add_handler(CommandHandler("bet", cmd_bet))
    application.add_handler(CommandHandler("cancel", cmd_cancelbet))

    # Admin commands
    application.add_handler(CommandHandler("admin_status", cmd_admin_status))
    application.add_handler(CommandHandler("admin_refresh", cmd_admin_refresh))
    application.add_handler(CommandHandler("admin_result", cmd_admin_result))
    application.add_handler(CommandHandler("admin_cancel_match", cmd_admin_cancel_match))
    application.add_handler(CommandHandler("admin_credits", cmd_admin_credits))
    application.add_handler(CommandHandler("admin_endtournament", cmd_admin_endtournament))
    application.add_handler(CommandHandler("confirm_admin", cmd_confirm_admin))
    application.add_handler(CommandHandler("cancel_admin", cmd_cancel_admin))


async def post_init(app):
    """Runs after bot starts — triggers startup sequence."""
    await app.bot.set_my_commands([
        ("matches", "Today's matches + kickoff times"),
        ("bet", "Place a bet"),
        ("mybets", "Your open bets"),
        ("cancel", "Cancel an open bet"),
        ("balance", "Your current credits"),
        ("leaderboard", "Full standings"),
        ("help", "Help"),
    ])
    await sched.on_startup(notify_fn=dm_admin)


if __name__ == "__main__":
    setup_handlers()
    application.post_init = post_init
    application.run_webhook(
        listen="0.0.0.0",
        port=8080,
        webhook_url="https://telegram-wc-king-production.up.railway.app",
        allowed_updates=Update.ALL_TYPES
    )
