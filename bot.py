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
    GROUP_CHAT_ID as ENV_GROUP_CHAT_ID,
    TEAM_ALIASES, FUZZY_THRESHOLD, TEAM_DISPLAY,
    RESULT_OUTCOMES, OU_OUTCOMES, ALL_OUTCOMES,
    SESSION_EXPIRY, SGT, UTC,
    DAILY_CREDITS, BET_LOCK_BUFFER, PARLAY_MULTIPLIERS
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


async def send_confirmation(update, message: str):
    """Send confirmation — DM during silent hours, group reply otherwise."""
    user = update.effective_user
    if is_silent_hours():
        try:
            await application.bot.send_message(chat_id=user.id, text=message)
            return
        except Exception:
            pass
    await update.message.reply_text(message)


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

    # Admin announce — catch next DM message as announcement
    if update.effective_chat.type == "private" and update.effective_user.id == ADMIN_TELEGRAM_ID:
        session = _sessions.get(ADMIN_TELEGRAM_ID)
        if session and not session_expired(session) and session.get("action") == "announce":
            if update.message and update.message.text and not update.message.text.startswith("/"):
                clear_session(ADMIN_TELEGRAM_ID)
                await sched.send_group(update.message.text, parse_mode=None)
                await update.message.reply_text("✅ Announcement sent to group.")
                return

    if update.effective_chat.type in ("group", "supergroup"):
        if _group_chat_id is None:
            _group_chat_id = update.effective_chat.id
            sched.init(context.bot, _group_chat_id)
            logger.info(f"Group chat ID locked: {_group_chat_id}")
            await dm_admin(f"✅ Degen locked to group chat ID: {_group_chat_id}")
            try:
                ws = sheet.get_sheet("users")
            except Exception:
                pass
        elif update.effective_chat.id != _group_chat_id:
            return


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
        "/parlay [amount], [team] [win|draw|loss], ... — Place a parlay\n"
        "/mybets — Your open bets\n"
        "/cancel — Cancel an open bet\n"
        "/cancelparlay — Cancel an active parlay\n"
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

    # If all today's matches are done, show tomorrow's instead
    all_done = all(m.get("status") in ("FINISHED", "CANCELLED", "POSTPONED") for m in day_matches)
    if all_done:
        tomorrow_ct = (datetime.now(CT) + timedelta(days=1)).strftime("%Y-%m-%d")
        day_after_utc = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d")
        next_matches_raw = await sheet.get_matches_for_date(tomorrow_utc) + await sheet.get_matches_for_date(day_after_utc)
        next_matches = []
        for m in next_matches_raw:
            try:
                kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == tomorrow_ct:
                    next_matches.append(m)
            except Exception:
                continue
        if next_matches:
            day_matches = next_matches
            header = "📅 Tomorrow's Matches\n"
        else:
            await update.message.reply_text("No upcoming matches.")
            return
    else:
        header = "📅 Today's Matches\n"

    lines = [header]
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


# ── /groups ───────────────────────────────────────────────────────────────────
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    try:
        CT = pytz.timezone("America/Chicago")
        today_ct = datetime.now(CT).strftime("%Y-%m-%d")
        today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
        tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

        today_teams = set()
        for m in all_matches:
            try:
                kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today_ct:
                    today_teams.add(m["home"])
                    today_teams.add(m["away"])
            except Exception:
                continue

        all_standings = api.fetch_standings()
        if not all_standings:
            await update.message.reply_text("No standings available yet.")
            return

        lines = ["📊 Group Standings\n"]
        for group in all_standings:
            group_teams = {row["team"] for row in group["table"]}
            if today_teams and not today_teams.intersection(group_teams):
                continue
            group_name = group["group"].replace("GROUP_", "Group ")
            lines.append(f"── {group_name} ──")
            for row in group["table"]:
                team = row["team"]
                if team in TEAM_DISPLAY:
                    code, flag = TEAM_DISPLAY[team]
                    flag_code = f"{flag} {code}"
                else:
                    flag_code = team[:3].upper()
                w, d, l = row["won"], row["draw"], row["lost"]
                pts = row["points"]
                lines.append(f"{row['position']}. {flag_code} — {pts}pts ({w}W {d}D {l}L)")
            lines.append("")

        await update.message.reply_text("\n".join(lines))
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ Could not fetch standings: {e}")


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

    if len(args) < 2:
        await update.message.reply_text("Usage: /bet [team] [win|loss|draw|over|under] [amount]")
        return

    first_arg = args[0].lower()

    # ── Event bet: /bet event1 [option_number] [amount] ──
    if first_arg in sheet.cache["events"]:
        event_id = first_arg
        event = sheet.cache["events"][event_id]

        if event["status"] != "open":
            await update.message.reply_text(f"Event {event_id} is not open for betting.")
            return
        if event["is_free"]:
            await update.message.reply_text(f"This is a free prediction event. Use /predict {event_id} [number].")
            return
        if len(args) < 3:
            await update.message.reply_text(f"Usage: /bet {event_id} [option number] [amount]")
            return

        try:
            option_idx = int(args[1])
            if option_idx < 1 or option_idx > len(event["options"]):
                await update.message.reply_text(f"Invalid option. Choose 1–{len(event['options'])}.")
                return
        except ValueError:
            await update.message.reply_text(f"Usage: /bet {event_id} [option number] [amount]")
            return

        amount_input = args[2].lower().replace("c", "")
        try:
            amount = int(float(amount_input))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please enter a valid amount.")
            return

        if user_data["credits"] < amount:
            await update.message.reply_text(f"Insufficient credits. Balance: {user_data['credits']}c")
            return

        option_str = event["options"][option_idx - 1]
        flag_code = ""
        for k, (code, flag) in TEAM_DISPLAY.items():
            if code == option_str:
                flag_code = f"{flag} {code}"
                break
        if not flag_code:
            flag_code = option_str

        try:
            await sheet.place_event_bet(user.id, event_id, str(option_idx), amount, notify_fn=dm_admin)
            new_balance = sheet.cache["users"][user.id]["credits"]
            confirm_msg = (
                f"✅ Bet placed!\n"
                f"{event['question']}\n"
                f"{flag_code} — {amount}c\n"
                f"Balance: {new_balance}c"
            )
            await send_confirmation(update, confirm_msg)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to place bet: {e}")
        return

    # ── Regular match bet ──
    team_input = args[0]
    outcome_input = args[1].lower() if len(args) > 1 else ""
    amount_input = args[2].lower().replace("c", "") if len(args) > 2 else ""

    if len(args) < 3:
        await update.message.reply_text("Usage: /bet [team] [win|loss|draw|over|under] [amount]")
        return

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


# ── /parlay ───────────────────────────────────────────────────────────────────
async def cmd_parlay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_message(update):
        await update.message.reply_text("Please use this command in the group.")
        return

    user = update.effective_user
    user_data = await ensure_registered(update)

    # Parse: /parlay 50, mexico win, brazil draw, germany win
    raw = " ".join(context.args)
    if not raw:
        await update.message.reply_text(
            "Usage: /parlay [amount], [team] [win|draw|loss], ...\n"
            "Example: /parlay 50, mexico win, brazil draw\n\n"
            "Multipliers: 2 legs = 2.5x · 3 legs = 5x · 4 legs = 10x\n"
            "Result/draw bets only. All legs must win."
        )
        return

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 3:
        await update.message.reply_text(
            "Need at least: amount + 2 legs.\n"
            "Example: /parlay 50, mexico win, brazil draw"
        )
        return

    # First part is amount
    try:
        amount = int(float(parts[0].lower().replace("c", "")))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("First value must be the stake amount. Example: /parlay 50, mexico win, brazil draw")
        return

    leg_parts = parts[1:]
    if len(leg_parts) < 2:
        await update.message.reply_text("Need at least 2 legs.")
        return
    if len(leg_parts) > 4:
        await update.message.reply_text("Maximum 4 legs per parlay.")
        return

    if user_data["credits"] < amount:
        await update.message.reply_text(f"Insufficient credits. Balance: {user_data['credits']}c")
        return

    # Validate all legs before placing anything
    validated_legs = []
    for leg_str in leg_parts:
        tokens = leg_str.strip().split()
        if len(tokens) < 2:
            await update.message.reply_text(f"Invalid leg: '{leg_str}'. Format: [team] [win|draw|loss]")
            return

        team_input = tokens[0]
        outcome_input = tokens[1].lower()

        if outcome_input not in RESULT_OUTCOMES:
            await update.message.reply_text(
                f"'{outcome_input}' is not valid for parlays. Use: win, draw, loss (no over/under)."
            )
            return

        team_name, is_ambiguous = resolve_team(team_input)
        if is_ambiguous:
            await update.message.reply_text(f"'{team_input}' is ambiguous. Be more specific.")
            return
        if not team_name:
            await update.message.reply_text(f"Couldn't find team '{team_input}'. Check /matches.")
            return

        match = find_match_for_team(team_name)
        if not match:
            await update.message.reply_text(
                f"No open match for {team_name}, or betting is already closed."
            )
            return

        internal_outcome = map_outcome_to_result(outcome_input, match, team_name)
        if not internal_outcome:
            await update.message.reply_text(f"Invalid outcome for {team_name}.")
            return

        validated_legs.append({
            "team_name": team_name,
            "match": match,
            "outcome": internal_outcome,
            "outcome_display": outcome_input.capitalize()
        })

    # Check no duplicate matches
    match_ids_in_parlay = [l["match"]["match_id"] for l in validated_legs]
    if len(match_ids_in_parlay) != len(set(match_ids_in_parlay)):
        await update.message.reply_text("Duplicate match in parlay. Each leg must be a different match.")
        return

    # Check all legs are on the same CT match day
    CT = pytz.timezone("America/Chicago")
    leg_dates = set()
    for leg in validated_legs:
        try:
            kickoff_utc = datetime.strptime(leg["match"]["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            leg_dates.add(kickoff_utc.astimezone(CT).strftime("%Y-%m-%d"))
        except Exception:
            pass
    if len(leg_dates) > 1:
        await update.message.reply_text("All parlay legs must be on the same match day. These legs span different days.")
        return

    multiplier = PARLAY_MULTIPLIERS.get(len(validated_legs))
    potential_return = int(amount * multiplier)

    # Place all legs atomically — generate one parlay_id
    parlay_id = f"p_{user.id}_{datetime.now(UTC).strftime('%H%M%S%f')}"
    placed_bet_ids = []

    try:
        for i, leg in enumerate(validated_legs):
            bet_id = await sheet.place_bet(
                user_id=user.id,
                match_id=leg["match"]["match_id"],
                market="result",
                outcome=leg["outcome"],
                amount=amount,
                notify_fn=dm_admin,
                parlay_id=parlay_id,
                deduct_credits=(i == 0)  # only deduct on first leg
            )
            placed_bet_ids.append(bet_id)

        new_balance = sheet.cache["users"][user.id]["credits"]
        lines = [f"🎰 Parlay locked! ({len(validated_legs)} legs · {multiplier}x)\n"]
        for i, leg in enumerate(validated_legs, 1):
            home = format_team(leg["match"]["home"])
            away = format_team(leg["match"]["away"])
            lines.append(f"{i}. {home} vs {away} — {leg['outcome_display']}")
        lines.append(f"\nStake: {amount}c · Win all → {potential_return}c back")
        lines.append(f"Balance: {new_balance}c")

        confirm_msg = "\n".join(lines)
        if is_silent_hours():
            try:
                await application.bot.send_message(chat_id=user.id, text=confirm_msg)
            except Exception:
                await update.message.reply_text(confirm_msg)
        else:
            await update.message.reply_text(confirm_msg)

    except ValueError as e:
        # Rollback — cancel first leg only (only leg that deducted credits)
        if placed_bet_ids:
            try:
                await sheet.cancel_bet(placed_bet_ids[0], user.id)
            except Exception:
                pass
        await update.message.reply_text(str(e))
    except Exception as e:
        # Rollback — cancel first leg only
        if placed_bet_ids:
            try:
                await sheet.cancel_bet(placed_bet_ids[0], user.id)
            except Exception:
                pass
        logger.error(f"Parlay placement failed for {user.id}: {e}")
        await update.message.reply_text("Something went wrong placing your parlay. All legs rolled back.")
        await dm_admin(f"⚠️ Parlay placement failed for user {user.id}: {e}")


# ── /cancelparlay ─────────────────────────────────────────────────────────────
async def cmd_cancelparlay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    user_id = update.effective_user.id
    args = context.args

    active_parlays = sheet.get_user_active_parlays(user_id)
    if not active_parlays:
        await update.message.reply_text("You have no active parlays.")
        return

    # /cancelparlay [number] — cancel specific parlay from session
    if args:
        session = _sessions.get(user_id)
        if not session or session_expired(session) or session.get("action") != "cancelparlay":
            await update.message.reply_text("Run /cancelparlay first to see your parlays.")
            return
        clear_session(user_id)

        try:
            index = int(args[0]) - 1
            parlays = session["data"]["parlays"]
            if index < 0 or index >= len(parlays):
                await update.message.reply_text("Invalid number. Run /cancelparlay again.")
                return
        except ValueError:
            await update.message.reply_text("Usage: /cancelparlay [number]")
            return

        parlay_id = parlays[index]["parlay_id"]
        legs = parlays[index]["legs"]

        # Check all legs still pre-kickoff
        for leg in legs:
            match = await sheet.get_match_by_id(leg["match_id"])
            if match:
                try:
                    kickoff = datetime.strptime(match["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                    lock_time = kickoff + timedelta(seconds=BET_LOCK_BUFFER)
                    if datetime.now(UTC) >= lock_time:
                        home = format_team(match["home"])
                        away = format_team(match["away"])
                        await update.message.reply_text(
                            f"Cannot cancel — {home} vs {away} has already kicked off."
                        )
                        return
                except Exception:
                    pass

        try:
            count = await sheet.void_parlay_bets(parlay_id, user_id, notify_fn=dm_admin)
            new_balance = sheet.cache["users"][user_id]["credits"]
            stake = legs[0]["amount"]
            await update.message.reply_text(
                f"✅ Parlay cancelled.\n"
                f"{count} leg(s) voided · {stake}c refunded.\n"
                f"Balance: {new_balance}c"
            )
        except Exception as e:
            await update.message.reply_text("Failed to cancel parlay. Please try again.")
            await dm_admin(f"⚠️ Cancel parlay failed for user {user_id}: {e}")
        return

    # Only 1 parlay — cancel directly without listing
    if len(active_parlays) == 1:
        parlay_id = active_parlays[0]
        legs = sheet.get_parlay_bets(parlay_id)

        for leg in legs:
            match = await sheet.get_match_by_id(leg["match_id"])
            if match:
                try:
                    kickoff = datetime.strptime(match["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                    lock_time = kickoff + timedelta(seconds=BET_LOCK_BUFFER)
                    if datetime.now(UTC) >= lock_time:
                        home = format_team(match["home"])
                        away = format_team(match["away"])
                        await update.message.reply_text(
                            f"Cannot cancel — {home} vs {away} has already kicked off."
                        )
                        return
                except Exception:
                    pass

        try:
            count = await sheet.void_parlay_bets(parlay_id, user_id, notify_fn=dm_admin)
            new_balance = sheet.cache["users"][user_id]["credits"]
            stake = legs[0]["amount"] if legs else 0
            await update.message.reply_text(
                f"✅ Parlay cancelled.\n"
                f"{count} leg(s) voided · {stake}c refunded.\n"
                f"Balance: {new_balance}c"
            )
        except Exception as e:
            await update.message.reply_text("Failed to cancel parlay. Please try again.")
            await dm_admin(f"⚠️ Cancel parlay failed for user {user_id}: {e}")
        return

    # Multiple parlays — list them
    lines = ["🎰 Your active parlays:\n"]
    parlay_list = []
    for i, pid in enumerate(active_parlays, 1):
        legs = sheet.get_parlay_bets(pid)
        if not legs:
            continue
        stake = legs[0]["amount"]
        multiplier = PARLAY_MULTIPLIERS.get(len(legs), "?")
        leg_lines = []
        for leg in legs:
            match = await sheet.get_match_by_id(leg["match_id"])
            if match:
                home = format_team(match["home"])
                away = format_team(match["away"])
                outcome_label = format_outcome_label(leg["outcome"], match)
                leg_lines.append(f"{home} vs {away} — {outcome_label}")
        lines.append(f"{i}. {stake}c · {multiplier}x")
        for ll in leg_lines:
            lines.append(f"   {ll}")
        parlay_list.append({"parlay_id": pid, "legs": legs})

    lines.append("\nReply /cancelparlay [number] to cancel.")

    _sessions[user_id] = {
        "action": "cancelparlay",
        "data": {"parlays": parlay_list},
        "expires": datetime.now(UTC) + timedelta(seconds=SESSION_EXPIRY)
    }
    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_poll ────────────────────────────────────────────────────────
async def cmd_admin_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args

    # /admin_poll [number] — trigger poll for selected match
    if args:
        session = _admin_pending.get(ADMIN_TELEGRAM_ID)
        if not session or session_expired(session) or session.get("action") != "poll_select":
            await update.message.reply_text("Run /admin_poll first to see matches.")
            return
        try:
            index = int(args[0]) - 1
            matches = session["data"]["matches"]
            if index < 0 or index >= len(matches):
                await update.message.reply_text("Invalid number.")
                return
        except ValueError:
            await update.message.reply_text("Usage: /admin_poll [number]")
            return

        del _admin_pending[ADMIN_TELEGRAM_ID]
        match = matches[index]
        match_id = match["match_id"]
        sched.trigger_poll(match_id)
        home = format_team(match["home"])
        away = format_team(match["away"])
        await update.message.reply_text(f"⏳ Polling triggered for {home} vs {away}. Result will post when confirmed.")
        return

    # /admin_poll — list today's unfinished matches
    CT = pytz.timezone("America/Chicago")
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

    unfinished = []
    for m in all_matches:
        try:
            kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") != today_ct:
                continue
            if m["status"] not in ("FINISHED", "CANCELLED", "POSTPONED"):
                unfinished.append(m)
        except Exception:
            continue

    if not unfinished:
        await update.message.reply_text("No unfinished matches today.")
        return

    lines = ["Which match to poll?\n"]
    for i, m in enumerate(unfinished, 1):
        home = format_team(m["home"])
        away = format_team(m["away"])
        lines.append(f"{i}. {home} vs {away} — {m['status']}")
    lines.append("\nReply /admin_poll [number]")

    _admin_pending[ADMIN_TELEGRAM_ID] = {
        "action": "poll_select",
        "data": {"matches": unfinished},
        "expires": datetime.now(UTC) + timedelta(seconds=120)
    }

    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_result_push ─────────────────────────────────────────────────
async def cmd_admin_result_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args

    # /admin_result_push [number] — push result message for selected match
    if args:
        session = _admin_pending.get(ADMIN_TELEGRAM_ID)
        if not session or session_expired(session) or session.get("action") != "result_push_select":
            await update.message.reply_text("Run /admin_result_push first to see matches.")
            return
        try:
            index = int(args[0]) - 1
            matches = session["data"]["matches"]
            if index < 0 or index >= len(matches):
                await update.message.reply_text("Invalid number.")
                return
        except ValueError:
            await update.message.reply_text("Usage: /admin_result_push [number]")
            return

        del _admin_pending[ADMIN_TELEGRAM_ID]
        match = matches[index]
        match_id = match["match_id"]

        bets = await sheet.get_bets_for_match(match_id)
        settlements = [b for b in bets if b["status"] in ("won", "lost")]
        result_msg = sched.format_result_message(match, settlements)
        await sched.send_group(result_msg)
        await update.message.reply_text("✅ Result message pushed to group.")
        return

    # /admin_result_push — list today's finished matches
    CT = pytz.timezone("America/Chicago")
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

    finished = []
    for m in all_matches:
        try:
            kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") != today_ct:
                continue
            if m["status"] == "FINISHED":
                finished.append(m)
        except Exception:
            continue

    if not finished:
        await update.message.reply_text("No finished matches today.")
        return

    lines = ["Which match to push result for?\n"]
    for i, m in enumerate(finished, 1):
        home = format_team(m["home"])
        away = format_team(m["away"])
        lines.append(f"{i}. {home} vs {away} — {m['home_score']}–{m['away_score']}")
    lines.append("\nReply /admin_result_push [number]")

    _admin_pending[ADMIN_TELEGRAM_ID] = {
        "action": "result_push_select",
        "data": {"matches": finished},
        "expires": datetime.now(UTC) + timedelta(seconds=120)
    }
    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_eod_push ────────────────────────────────────────────────────
async def cmd_admin_eod_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args

    # Step 2: confirmed
    if args and args[0] == "confirm":
        session = _admin_pending.get(ADMIN_TELEGRAM_ID)
        if not session or session_expired(session) or session.get("action") != "eod_push":
            await update.message.reply_text("Session expired. Run /admin_eod_push again.")
            return
        match_ids = session["data"]["match_ids"]
        del _admin_pending[ADMIN_TELEGRAM_ID]
        try:
            sheet.cache["eod_date"] = None  # allow manual push to bypass guard
            await sched.job_post_standings(match_ids)
            await update.message.reply_text("✅ End of day message pushed to group.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed: {e}")
        return

    # Step 1: preview and ask for confirmation
    try:
        CT = pytz.timezone("America/Chicago")
        today_ct = datetime.now(CT).strftime("%Y-%m-%d")
        today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
        tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

        match_ids = []
        for m in all_matches:
            try:
                kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today_ct:
                    match_ids.append(m["match_id"])
            except Exception:
                continue

        _admin_pending[ADMIN_TELEGRAM_ID] = {
            "action": "eod_push",
            "data": {"match_ids": match_ids},
            "expires": datetime.now(UTC) + timedelta(seconds=120)
        }
        await update.message.reply_text(
            f"About to push EOD message for {len(match_ids)} match(es) + add daily credits.\n\n"
            f"Run /admin_eod_push confirm to proceed."
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed: {e}")


# ── /predict ──────────────────────────────────────────────────────────────────
async def cmd_predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    user_id = update.effective_user.id
    args = context.args

    if len(args) < 2:
        await update.message.reply_text("Usage: /predict [event_id] [option number]")
        return

    event_id = args[0].lower()
    option_input = args[1]

    event = sheet.cache["events"].get(event_id)
    if not event:
        await update.message.reply_text(f"Event {event_id} not found.")
        return
    if event["status"] != "open":
        await update.message.reply_text(f"Event {event_id} is not open for predictions.")
        return
    if not event["is_free"]:
        await update.message.reply_text(f"This is a paid event. Use /bet {event_id} [option] [amount].")
        return

    try:
        option_idx = int(option_input)
        if option_idx < 1 or option_idx > len(event["options"]):
            await update.message.reply_text(f"Invalid option. Choose 1–{len(event['options'])}.")
            return
    except ValueError:
        await update.message.reply_text("Usage: /predict [event_id] [option number]")
        return

    # Check for existing prediction
    existing = [b for b in sheet.cache["bets"] if b["user_id"] == user_id and b["match_id"] == event_id and b["status"] == "open"]
    if existing:
        await update.message.reply_text(f"You already have a prediction on this event.")
        return

    option_str = event["options"][option_idx - 1]
    flag_code = format_team(option_str) if option_str in TEAM_DISPLAY else option_str

    try:
        await sheet.place_event_bet(user_id, event_id, str(option_idx), 0, notify_fn=dm_admin)
        await update.message.reply_text(
            f"✅ Prediction locked!\n"
            f"{event['question']}\n"
            f"{flag_code}\n"
            f"No credits deducted. +{event['reward']}c if correct!"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to place prediction: {e}")


# ── Admin: /admin_event ───────────────────────────────────────────────────────
async def cmd_admin_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Admin event commands:\n"
            "/admin_event create\n"
            "/admin_event open [event_id]\n"
            "/admin_event edit [event_id]\n"
            "/admin_event resolve [event_id] [option]\n"
            "/admin_event cancel [event_id]"
        )
        return

    subcommand = args[0].lower()

    # ── create ──
    if subcommand == "create":
        # Parse: /admin_event create "question" team1 team2 ... [2x] [free 100]
        raw = " ".join(args[1:])

        # Extract question from quotes
        import re
        q_match = re.match(r'"([^"]+)"(.*)', raw)
        if not q_match:
            await update.message.reply_text('Usage: /admin_event create "Question?" team1 team2 [2x] [free 100]')
            return

        question = q_match.group(1)
        rest = q_match.group(2).strip().split()

        # Parse teams, multiplier, free/reward
        teams = []
        multiplier = 1.0
        is_free = False
        reward = 0
        i = 0
        while i < len(rest):
            token = rest[i].lower()
            if re.match(r'^\d+(\.\d+)?x$', token):
                multiplier = float(token[:-1])
            elif token == "free" and i + 1 < len(rest) and rest[i+1].isdigit():
                is_free = True
                reward = int(rest[i+1])
                i += 1
            else:
                # Resolve team alias
                resolved = TEAM_ALIASES.get(token)
                if resolved and resolved in TEAM_DISPLAY:
                    teams.append(TEAM_DISPLAY[resolved][0])
                elif token.upper() in [v[0] for v in TEAM_DISPLAY.values()]:
                    teams.append(token.upper())
                else:
                    teams.append(token.upper())
            i += 1

        if len(teams) < 2:
            await update.message.reply_text("Need at least 2 options.")
            return

        try:
            event_id = await sheet.create_event(question, teams, multiplier, is_free, reward, notify_fn=dm_admin)
            event = sheet.cache["events"][event_id]

            lines = [f"✅ Event created! ID: {event_id}\n", f"{question}\n"]
            for i, opt in enumerate(teams, 1):
                flag = TEAM_DISPLAY.get(
                    next((k for k, v in TEAM_DISPLAY.items() if v[0] == opt), None),
                    ("", "")
                )[1] if opt in [v[0] for v in TEAM_DISPLAY.values()] else ""
                lines.append(f"{i}. {flag} {opt}")

            if is_free:
                lines.append(f"\nFree prediction — +{reward}c for correct answer")
            else:
                lines.append(f"\nPayout: {multiplier}x")

            lines.append(f"\nRun /admin_event open {event_id} when ready.")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to create event: {e}")

    # ── open ──
    elif subcommand == "open" and len(args) >= 2:
        event_id = args[1].lower()
        event = sheet.cache["events"].get(event_id)
        if not event:
            await update.message.reply_text(f"Event {event_id} not found.")
            return
        if event["status"] not in ("draft",):
            await update.message.reply_text(f"Event is already {event['status']}.")
            return

        await sheet.update_event_status(event_id, "open", notify_fn=dm_admin)

        lines = [f"🎯 Special Event!\n{event['question']}\n"]
        for i, opt in enumerate(event["options"], 1):
            flag_code = ""
            for k, (code, flag) in TEAM_DISPLAY.items():
                if code == opt:
                    flag_code = f"{flag} {code}"
                    break
            if not flag_code:
                flag_code = opt
            lines.append(f"{i}. {flag_code}")

        if event["is_free"]:
            lines.append(f"\nPredict: /predict {event_id} [number]")
            lines.append(f"Correct answer wins +{event['reward']}c!")
        else:
            lines.append(f"\nBet: /bet {event_id} [number] [amount]")
            lines.append(f"Payout: {event['multiplier']}x")

        await sched.send_group("\n".join(lines))
        await update.message.reply_text(f"✅ Event {event_id} is now open.")

    # ── edit ──
    elif subcommand == "edit" and len(args) >= 2:
        event_id = args[1].lower()
        event = sheet.cache["events"].get(event_id)
        if not event:
            await update.message.reply_text(f"Event {event_id} not found.")
            return
        if event["status"] != "draft":
            await update.message.reply_text("Can only edit events in draft status.")
            return

        # Store edit session
        _admin_pending[ADMIN_TELEGRAM_ID] = {
            "action": "event_edit",
            "data": {"event_id": event_id},
            "expires": datetime.now(UTC) + timedelta(seconds=300)
        }
        await update.message.reply_text(
            f"Editing {event_id}. Send new command:\n"
            f"/admin_event create \"New question?\" team1 team2 [multiplier] [free reward]\n\n"
            f"Current:\n"
            f"Q: {event['question']}\n"
            f"Options: {', '.join(event['options'])}\n"
            f"Multiplier: {event['multiplier']}x | Free: {event['is_free']} | Reward: {event['reward']}"
        )

    # ── resolve ──
    elif subcommand == "resolve" and len(args) >= 3:
        event_id = args[1].lower()
        winner_input = args[2].upper()
        confirmed = len(args) >= 4 and args[3].lower() == "confirm"

        event = sheet.cache["events"].get(event_id)
        if not event:
            await update.message.reply_text(f"Event {event_id} not found.")
            return
        if event["status"] != "open":
            await update.message.reply_text(f"Event {event_id} is not open.")
            return

        # Resolve winner option
        winner_opt = None
        for opt in event["options"]:
            if opt.upper() == winner_input or TEAM_ALIASES.get(winner_input.lower()) == next((k for k, v in TEAM_DISPLAY.items() if v[0] == opt), None):
                winner_opt = opt
                break
        if not winner_opt:
            # Try fuzzy
            from rapidfuzz import process
            match = process.extractOne(winner_input, event["options"])
            if match and match[1] >= 70:
                winner_opt = match[0]
        if not winner_opt:
            await update.message.reply_text(f"Could not match '{winner_input}' to any option: {', '.join(event['options'])}")
            return

        winner_idx = str(event["options"].index(winner_opt) + 1)
        event_bets = sheet.get_event_bets(event_id)
        winners = [b for b in event_bets if b["outcome"] == winner_idx and b["status"] == "open"]
        losers = [b for b in event_bets if b["outcome"] != winner_idx and b["status"] == "open"]

        if not confirmed:
            lines = [
                f"Confirm resolution:\n{event['question']}",
                f"Winner: {winner_opt}\n",
                "Winners:"
            ]
            for b in winners:
                user = sheet.cache["users"].get(b["user_id"], {})
                name = (user.get("first_name") or user.get("username") or "?")[:10]
                payout = event["reward"] if event["is_free"] else int(b["amount"] * event["multiplier"])
                lines.append(f"• {name} → +{payout}c")
            lines.append("\nLosers:")
            for b in losers:
                user = sheet.cache["users"].get(b["user_id"], {})
                name = (user.get("first_name") or user.get("username") or "?")[:10]
                lines.append(f"• {name} ❌")
            lines.append(f"\n/admin_event resolve {event_id} {winner_input} confirm")
            await update.message.reply_text("\n".join(lines))
            return

        # Confirmed — settle
        try:
            settlements = await sheet.settle_event_bets(event_id, winner_opt, notify_fn=dm_admin)
            await sheet.update_event_status(event_id, "resolved", winner=winner_opt, notify_fn=dm_admin)

            lines = [f"🎯 {event['question']}\n{winner_opt} wins!\n"]
            def get_sort_name(s):
                user = sheet.cache["users"].get(s["user_id"], {})
                return (user.get("first_name") or user.get("username") or "").lower()
            for s in sorted(settlements, key=get_sort_name):
                user = sheet.cache["users"].get(s["user_id"], {})
                name = (user.get("first_name") or user.get("username") or "?")[:10]
                icon = "✅" if s["status"] == "won" else "❌"
                option_str = event["options"][int(s["outcome"]) - 1] if s["outcome"].isdigit() else s["outcome"]
                lines.append(f"{name} — {option_str} {icon}")

            await sched.send_group("\n".join(lines))
            await update.message.reply_text(f"✅ Event {event_id} resolved. {len(winners)} winners paid out.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to resolve event: {e}")

    # ── cancel ──
    elif subcommand == "cancel" and len(args) >= 2:
        event_id = args[1].lower()
        event = sheet.cache["events"].get(event_id)
        if not event:
            await update.message.reply_text(f"Event {event_id} not found.")
            return
        if event["status"] == "resolved":
            await update.message.reply_text("Cannot cancel a resolved event.")
            return

        # Refund all open bets
        open_bets = [b for b in sheet.cache["bets"] if b["match_id"] == event_id and b["status"] == "open"]
        for b in open_bets:
            if b["amount"] > 0:
                await sheet.cancel_bet(b["bet_id"], b["user_id"], notify_fn=dm_admin)

        await sheet.update_event_status(event_id, "cancelled", notify_fn=dm_admin)
        await sched.send_group(f"🎯 Event cancelled: {event['question']}\nAll bets refunded.")
        await update.message.reply_text(f"✅ Event {event_id} cancelled. {len(open_bets)} bets refunded.")

    else:
        await update.message.reply_text(
            "Usage:\n"
            "/admin_event create \"Question?\" team1 team2 [2x] [free 100]\n"
            "/admin_event open [event_id]\n"
            "/admin_event edit [event_id]\n"
            "/admin_event resolve [event_id] [winner]\n"
            "/admin_event cancel [event_id]"
        )


# ── Admin: /admin_simulate_eod ────────────────────────────────────────────────
async def cmd_admin_simulate_eod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    standings = sheet.get_standings()
    lines = ["📅 End of Day\n"]
    lines.append("🇲🇽 MEX 2–0 🇿🇦 RSA · Under 2.5")
    lines.append("🇰🇷 KOR 2–1 🇨🇿 CZE · Over 2.5")
    lines.append("\n🏆 Standings")

    for i, user in enumerate(standings, 1):
        name = (user.get("first_name") or user.get("username") or "Unknown")[:10]
        credits = user["credits"]
        badge = " 🏆" if i == 1 else ""
        lines.append(f"{i}. {name}{badge} — {credits}c (+50c today)")

    lines.append("\n🎉 Peng had the biggest win today with +200c!")
    lines.append("📈 Calvin overtook Roysten 🤡 today.")
    lines.append("⚠️ Shunnnnnn is 80c behind Peng. Watch out!")
    lines.append("\nDaily credits added, good luck tomorrow! 🍀")
    lines.append("Use /groups for today's group tables.")

    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_sim_night ────────────────────────────────────────────────────
async def cmd_admin_sim_night(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    CT = pytz.timezone("America/Chicago")
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

    upcoming = []
    for m in all_matches:
        try:
            kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today_ct and m["status"] in ("SCHEDULED", "TIMED"):
                upcoming.append(m)
        except Exception:
            continue

    if not upcoming:
        await update.message.reply_text("No upcoming matches to simulate.")
        return

    lines = ["🌙 Good evening gents! Matches later:\n"]
    for m in sorted(upcoming, key=lambda x: x["kickoff_utc"]):
        lines.append(f"  {sched.format_match_line(m)}")
    lines.append("\nGet your bets in before kickoff. Good night! 🌛")
    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_sim_morning ──────────────────────────────────────────────────
async def cmd_admin_sim_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    CT = pytz.timezone("America/Chicago")
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

    today_matches = []
    for m in all_matches:
        try:
            kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today_ct:
                today_matches.append((kickoff_utc_dt, m))
        except Exception:
            continue

    if not today_matches:
        await update.message.reply_text("No matches today to simulate.")
        return

    lines = ["☀️ Good morning! Catch up from last night:\n"]
    for kickoff_utc_dt, m in sorted(today_matches, key=lambda x: x[0]):
        home_d = format_team(m["home"])
        away_d = format_team(m["away"])
        if m["status"] == "FINISHED":
            ou_label = "Over 2.5" if m["ou_result"] == "over" else "Under 2.5"
            lines.append(f"{home_d} {m['home_score']}–{m['away_score']} {away_d} · {ou_label}")
        elif m["status"] in ("IN_PLAY", "PAUSED", "HALFTIME"):
            lines.append(f"{home_d} vs {away_d} — IN PLAY")
        else:
            time_str = kickoff_utc_dt.astimezone(SGT).strftime("%I:%M %p SGT").lstrip("0")
            lines.append(f"{home_d} vs {away_d} — {time_str}")
    lines.append("\n[Fun line based on overnight results would appear here]")
    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_sim_prematch ─────────────────────────────────────────────────
async def cmd_admin_sim_prematch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args
    if args:
        session = _admin_pending.get(ADMIN_TELEGRAM_ID)
        if not session or session_expired(session) or session.get("action") != "sim_prematch":
            await update.message.reply_text("Run /admin_sim_prematch first.")
            return
        try:
            index = int(args[0]) - 1
            matches = session["data"]["matches"]
            if index < 0 or index >= len(matches):
                await update.message.reply_text("Invalid number.")
                return
        except ValueError:
            return
        del _admin_pending[ADMIN_TELEGRAM_ID]
        match = matches[index]
        match_label = format_match_teams(match["home"], match["away"])
        bets = await sheet.get_bets_for_match(match["match_id"])
        open_bets = sorted([b for b in bets if b["status"] == "open"],
                           key=lambda b: (sheet.cache["users"].get(b["user_id"], {}).get("first_name") or "").lower())

        lines = [f"⚽ {match_label} kicks off in 15 mins!\n"]
        if open_bets:
            lines.append("Current bets:")
            for b in open_bets:
                user = sheet.cache["users"].get(b["user_id"], {})
                name = (user.get("first_name") or user.get("username") or "?")[:10]
                lines.append(f"{name} — {sched._outcome_label(b['outcome'], match)} — {b['amount']}c")
        else:
            lines.append("No bets placed yet.")
        lines.append("\nGet your bets in before kickoff! ⚽")
        await update.message.reply_text("\n".join(lines))
        return

    CT = pytz.timezone("America/Chicago")
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

    upcoming = []
    for m in all_matches:
        try:
            kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today_ct and m["status"] in ("SCHEDULED", "TIMED"):
                upcoming.append(m)
        except Exception:
            continue

    if not upcoming:
        await update.message.reply_text("No upcoming matches today.")
        return

    lines = ["Which match to simulate pre-match for?\n"]
    for i, m in enumerate(upcoming, 1):
        lines.append(f"{i}. {format_match_teams(m['home'], m['away'])}")
    lines.append("\nReply /admin_sim_prematch [number]")
    _admin_pending[ADMIN_TELEGRAM_ID] = {
        "action": "sim_prematch",
        "data": {"matches": upcoming},
        "expires": datetime.now(UTC) + timedelta(seconds=120)
    }
    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_sim_kickoff ──────────────────────────────────────────────────
async def cmd_admin_sim_kickoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args
    if args:
        session = _admin_pending.get(ADMIN_TELEGRAM_ID)
        if not session or session_expired(session) or session.get("action") != "sim_kickoff":
            await update.message.reply_text("Run /admin_sim_kickoff first.")
            return
        try:
            index = int(args[0]) - 1
            matches = session["data"]["matches"]
            if index < 0 or index >= len(matches):
                await update.message.reply_text("Invalid number.")
                return
        except ValueError:
            return
        del _admin_pending[ADMIN_TELEGRAM_ID]
        match = matches[index]
        await sched.job_kickoff_message(match["match_id"])
        await update.message.reply_text("✅ Kickoff message simulation sent to group.")
        return

    CT = pytz.timezone("America/Chicago")
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

    upcoming = []
    for m in all_matches:
        try:
            kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today_ct:
                upcoming.append(m)
        except Exception:
            continue

    if not upcoming:
        await update.message.reply_text("No matches today.")
        return

    lines = ["Which match to simulate kickoff message for?\n"]
    for i, m in enumerate(upcoming, 1):
        lines.append(f"{i}. {format_match_teams(m['home'], m['away'])}")
    lines.append("\nReply /admin_sim_kickoff [number]")
    _admin_pending[ADMIN_TELEGRAM_ID] = {
        "action": "sim_kickoff",
        "data": {"matches": upcoming},
        "expires": datetime.now(UTC) + timedelta(seconds=120)
    }
    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_sim_result ───────────────────────────────────────────────────
async def cmd_admin_sim_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    args = context.args
    if args:
        session = _admin_pending.get(ADMIN_TELEGRAM_ID)
        if not session or session_expired(session) or session.get("action") != "sim_result":
            await update.message.reply_text("Run /admin_sim_result first.")
            return
        try:
            index = int(args[0]) - 1
            matches = session["data"]["matches"]
            if index < 0 or index >= len(matches):
                await update.message.reply_text("Invalid number.")
                return
        except ValueError:
            return
        del _admin_pending[ADMIN_TELEGRAM_ID]
        match = matches[index]
        bets = await sheet.get_bets_for_match(match["match_id"])
        settlements = [b for b in bets if b["status"] in ("won", "lost")]
        result_msg = sched.format_result_message(match, settlements)
        await update.message.reply_text(f"Simulation:\n\n{result_msg}")
        return

    CT = pytz.timezone("America/Chicago")
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    tomorrow_utc = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc)

    finished = []
    for m in all_matches:
        try:
            kickoff_utc_dt = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if kickoff_utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today_ct and m["status"] == "FINISHED":
                finished.append(m)
        except Exception:
            continue

    if not finished:
        await update.message.reply_text("No finished matches today to simulate.")
        return

    lines = ["Which match to simulate result for?\n"]
    for i, m in enumerate(finished, 1):
        home_d = format_team(m["home"])
        away_d = format_team(m["away"])
        lines.append(f"{i}. {home_d} {m['home_score']}–{m['away_score']} {away_d}")
    lines.append("\nReply /admin_sim_result [number]")
    _admin_pending[ADMIN_TELEGRAM_ID] = {
        "action": "sim_result",
        "data": {"matches": finished},
        "expires": datetime.now(UTC) + timedelta(seconds=120)
    }
    await update.message.reply_text("\n".join(lines))


# ── Admin: /admin_announce ────────────────────────────────────────────────────
async def cmd_admin_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    _sessions[ADMIN_TELEGRAM_ID] = {
        "action": "announce",
        "expires": datetime.now(UTC) + timedelta(seconds=300)
    }
    await update.message.reply_text(
        "📢 Send your announcement as the next message.\n"
        "Formatting, line breaks and emojis will be preserved.\n\n"
        "Send /cancel_admin to abort."
    )


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

    # Direct override: /admin_result [match_id] [home_score] [away_score]
    if len(args) == 3:
        try:
            match_id = args[0]
            home_score = int(args[1])
            away_score = int(args[2])
            match = await sheet.get_match_by_id(match_id)
            if not match:
                await update.message.reply_text(f"Match {match_id} not found in cache. Try /admin_refresh first.")
                return
            home = format_team(match["home"])
            away = format_team(match["away"])
            _admin_pending[ADMIN_TELEGRAM_ID] = {
                "action": "result_confirm",
                "data": {"match": match, "home_score": home_score, "away_score": away_score},
                "expires": datetime.now(UTC) + timedelta(seconds=120)
            }
            total = home_score + away_score
            if home_score > away_score:
                result_label = f"{home} Win"
            elif away_score > home_score:
                result_label = f"{away} Win"
            else:
                result_label = "Draw"
            ou_label = "Over 2.5" if total > 2 else "Under 2.5"
            await update.message.reply_text(
                f"Confirm: {home} vs {away} — {home_score}–{away_score}\n"
                f"Result: {result_label} · {ou_label}\n\n"
                f"Settle all bets? /confirm_admin or /cancel_admin"
            )
            return
        except (ValueError, IndexError):
            await update.message.reply_text("Usage: /admin_result [match_id] [home_score] [away_score]")
            return

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

            # Check parlay completions
            parlay_wins = await sched.check_parlay_completions(match["match_id"])
            if parlay_wins:
                result_msg += "\n"
                for pid, p in parlay_wins:
                    name = sched._get_user_name(p["user_id"])
                    legs_str = "\n".join(f"• {label} ✅" for label in p["leg_labels"])
                    result_msg += f"\n🎰 {name} hit a {p['legs']}-leg parlay!\n{legs_str}\n{p['stake']}c → {p['payout']}c 🔥"

            if not sched.is_silent_hours():
                await sched.send_group(result_msg)
            elif parlay_wins:
                if "pending_parlay_wins" not in sheet.cache:
                    sheet.cache["pending_parlay_wins"] = []
                sheet.cache["pending_parlay_wins"].extend(parlay_wins)

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
    application.add_handler(CommandHandler("groups", cmd_groups))
    application.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    application.add_handler(CommandHandler("mybets", cmd_mybets))
    application.add_handler(CommandHandler("bet", cmd_bet))
    application.add_handler(CommandHandler("cancel", cmd_cancelbet))
    application.add_handler(CommandHandler("parlay", cmd_parlay))
    application.add_handler(CommandHandler("cancelparlay", cmd_cancelparlay))

    # Admin commands
    application.add_handler(CommandHandler("admin_announce", cmd_admin_announce))
    application.add_handler(CommandHandler("admin_status", cmd_admin_status))
    application.add_handler(CommandHandler("admin_refresh", cmd_admin_refresh))
    application.add_handler(CommandHandler("admin_result", cmd_admin_result))
    application.add_handler(CommandHandler("admin_cancel_match", cmd_admin_cancel_match))
    application.add_handler(CommandHandler("admin_credits", cmd_admin_credits))
    application.add_handler(CommandHandler("admin_endtournament", cmd_admin_endtournament))
    application.add_handler(CommandHandler("admin_poll", cmd_admin_poll))
    application.add_handler(CommandHandler("admin_result_push", cmd_admin_result_push))
    application.add_handler(CommandHandler("admin_eod_push", cmd_admin_eod_push))
    application.add_handler(CommandHandler("predict", cmd_predict))
    application.add_handler(CommandHandler("admin_event", cmd_admin_event))
    application.add_handler(CommandHandler("admin_simulate_eod", cmd_admin_simulate_eod))
    application.add_handler(CommandHandler("admin_sim_night", cmd_admin_sim_night))
    application.add_handler(CommandHandler("admin_sim_morning", cmd_admin_sim_morning))
    application.add_handler(CommandHandler("admin_sim_prematch", cmd_admin_sim_prematch))
    application.add_handler(CommandHandler("admin_sim_kickoff", cmd_admin_sim_kickoff))
    application.add_handler(CommandHandler("admin_sim_result", cmd_admin_sim_result))
    application.add_handler(CommandHandler("confirm_admin", cmd_confirm_admin))
    application.add_handler(CommandHandler("cancel_admin", cmd_cancel_admin))


async def post_init(app):
    """Runs after bot starts — triggers startup sequence."""
    global _group_chat_id
    if ENV_GROUP_CHAT_ID:
        _group_chat_id = ENV_GROUP_CHAT_ID
        sched.init(app.bot, _group_chat_id)
        logger.info(f"Group chat ID set from env: {_group_chat_id}")
    sheet.ensure_events_tab()
    await app.bot.set_my_commands([
        ("matches", "Today's matches + kickoff times"),
        ("bet", "Place a bet"),
        ("parlay", "Place a parlay bet"),
        ("mybets", "Your open bets"),
        ("cancel", "Cancel an open bet"),
        ("cancelparlay", "Cancel an active parlay"),
        ("balance", "Your current credits"),
        ("predict", "Free event prediction"),
        ("groups", "Group stage standings"),
        ("leaderboard", "Credits standings"),
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
