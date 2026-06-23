import json
import logging
import asyncio
import urllib.request
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import (
    SGT, UTC, DAILY_CREDITS, MATCH_CREDITS,
    NIGHT_REMINDER_HOUR, NIGHT_REMINDER_MINUTE,
    MORNING_CATCHUP_HOUR, MORNING_CATCHUP_MINUTE,
    PREMATCH_SUMMARY_MINUTES, POLL_START_OFFSET, POLL_INTERVAL,
    KNOCKOUT_DURATION, ANTHROPIC_API_KEY,
    ADMIN_TELEGRAM_ID, BOT_VERSION, TEAM_DISPLAY, PARLAY_MULTIPLIERS,
    DAILY_CREDIT_TIERS
)
import sheet
import api
from helpers import format_team, format_match_teams

logger = logging.getLogger(__name__)

# Module-level timezone constant
CT = pytz.timezone("America/Chicago")

scheduler = AsyncIOScheduler(timezone=UTC)

# Will be set by bot.py on startup
_bot = None
_group_chat_id = None


def init(bot, group_chat_id):
    global _bot, _group_chat_id
    _bot = bot
    _group_chat_id = group_chat_id


# ── DM admin ─────────────────────────────────────────────────────────────────
async def dm_admin(message: str):
    try:
        await _bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=message)
    except Exception as e:
        logger.error(f"Failed to DM admin: {e}")


# ── Group message ─────────────────────────────────────────────────────────────
async def send_group(message: str, parse_mode: str = None):
    try:
        await _bot.send_message(chat_id=_group_chat_id, text=message, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Failed to send group message: {e}")
        await dm_admin(f"⚠️ Failed to send group message: {e}")


# ── Silent hours — imported from helpers ─────────────────────────────────────
from helpers import is_silent_hours


# ── CT date helpers ───────────────────────────────────────────────────────────
async def get_ct_date_matches(ct_date: str) -> list:
    """Return all matches whose kickoff falls on the given CT date (YYYY-MM-DD).
    Uses the match's own CT date — not datetime.now() — so restarts across midnight
    never cause a match to be looked up against the wrong day's schedule."""
    ct_dt = datetime.strptime(ct_date, "%Y-%m-%d")
    seen = set()
    all_matches = []
    for delta in [-1, 0, 1]:
        utc_date = (ct_dt + timedelta(days=delta)).strftime("%Y-%m-%d")
        for m in await sheet.get_matches_for_date(utc_date):
            if m["match_id"] not in seen:
                seen.add(m["match_id"])
                all_matches.append(m)
    result = []
    for m in all_matches:
        try:
            ko = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if ko.astimezone(CT).strftime("%Y-%m-%d") == ct_date:
                result.append(m)
        except Exception:
            continue
    return result


async def get_today_ct_matches() -> list:
    """Return all matches whose kickoff falls on today's CT date."""
    return await get_ct_date_matches(datetime.now(CT).strftime("%Y-%m-%d"))


# ── Last match of day check ───────────────────────────────────────────────────
async def is_last_match_of_day(match_id: str) -> bool:
    """Returns True if match_id has the latest kickoff on its own CT date.
    Uses the match's kickoff to determine the CT date — not datetime.now() —
    so this stays correct even when the bot restarts after midnight UTC."""
    match = await sheet.get_match_by_id(match_id)
    if not match or not match.get("kickoff_utc"):
        return True
    ko = datetime.strptime(match["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    match_ct_date = ko.astimezone(CT).strftime("%Y-%m-%d")
    day_matches = await get_ct_date_matches(match_ct_date)
    if not day_matches:
        return True
    active = [m for m in day_matches if m.get("status") not in ("CANCELLED", "POSTPONED")]
    if not active:
        return True
    latest = max(active, key=lambda m: m.get("kickoff_utc", ""))
    return str(latest["match_id"]) == str(match_id)


# ── Format helpers ────────────────────────────────────────────────────────────
def format_match_line(match: dict) -> str:
    kickoff_utc = datetime.strptime(match["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    kickoff_sgt = kickoff_utc.astimezone(SGT)
    time_str = kickoff_sgt.strftime("%I:%M %p SGT").lstrip("0")
    return f"{format_match_teams(match['home'], match['away'])} — {time_str}"



def _outcome_label(outcome: str, match: dict) -> str:
    """Convert internal outcome to display label."""
    if outcome == "draw":
        return "Draw"
    if outcome == "over":
        return "Over 2.5"
    if outcome == "under":
        return "Under 2.5"
    if outcome == "home":
        team = match["home"]
        code = TEAM_DISPLAY[team][0] if team in TEAM_DISPLAY else team[:3].upper()
        return f"{code} Win"
    if outcome == "away":
        team = match["away"]
        code = TEAM_DISPLAY[team][0] if team in TEAM_DISPLAY else team[:3].upper()
        return f"{code} Win"
    return outcome.capitalize()


def _get_user_name(uid: int) -> str:
    user = sheet.cache["users"].get(uid, {})
    return (user.get("first_name") or user.get("username") or "Someone")


def _get_sort_name(b: dict) -> str:
    """Sort key for bets — alphabetical by player first name."""
    user = sheet.cache["users"].get(b["user_id"], {})
    return (user.get("first_name") or user.get("username") or "").lower()


def _is_parlay_leg(s: dict) -> bool:
    """Returns True if this bet is a parlay leg (not a single)."""
    pid = s.get("parlay_id", "")
    return bool(pid) and str(pid) not in ("", "0")


def _format_bet_line(b: dict, match: dict) -> str:
    """Standard bet line for all pre-match listings.
    Singles: Name — outcome — amount c
    Parlay legs: Name — outcome — 🎰 N/M (leg number from cache, no amount)"""
    name = _get_user_name(b["user_id"])
    outcome = _outcome_label(b["outcome"], match)
    if _is_parlay_leg(b):
        pid = b.get("parlay_id")
        all_legs = sorted(sheet.get_parlay_bets(pid), key=lambda x: x.get("placed_at", ""))
        total = len(all_legs)
        leg_num = next((i + 1 for i, l in enumerate(all_legs) if l["bet_id"] == b["bet_id"]), "?")
        return f"• {name} — {outcome} — 🎰 {leg_num}/{total}"
    return f"• {name} — {outcome} — {b['amount']}c"


def format_result_message(match: dict, settlements: list, parlay_wins: list = None) -> str:
    home_display = format_team(match["home"])
    away_display = format_team(match["away"])
    ou_label = "Over 2.5" if match["ou_result"] == "over" else "Under 2.5"

    lines = [
        f"{home_display} {match['home_score']}\u2013{match['away_score']} {away_display}",
        f"{_outcome_label(match['result'], match)} \u00b7 {ou_label}",
        "",
    ]

    if not settlements:
        lines.append("No bets placed on this match.")
        return "\n".join(lines)

    def get_name_str(uid):
        user = sheet.cache["users"].get(uid, {})
        return (user.get("first_name") or user.get("username") or "?")[:10]

    def sort_key(s):
        user = sheet.cache["users"].get(s["user_id"], {})
        return (user.get("first_name") or user.get("username") or "").lower()

    # Separate singles from parlay legs
    singles = [s for s in settlements if not _is_parlay_leg(s)]
    parlay_legs = [s for s in settlements if _is_parlay_leg(s)]

    for s in sorted(singles, key=sort_key):
        name = get_name_str(s["user_id"])
        icon = "\u2705" if s["status"] == "won" else "\u274c"
        lines.append(f"{name} \u2014 {_outcome_label(s['outcome'], match)} \u2014 {s['amount']}c {icon}")

    # Parlay display — one line per parlay
    if parlay_wins:
        for pid, p in parlay_wins:
            name = get_name_str(p["user_id"])
            legs_str = " \u00b7 ".join(f"{label} \u2705" for label in p["leg_labels"])
            lines.append(f"\U0001f3b0 {name}: {legs_str} \u2014 {p['stake']}c \u2192 {p['payout']}c \U0001f525")
    elif parlay_legs:
        seen_pids = {}
        for s in sorted(parlay_legs, key=sort_key):
            pid = str(s.get("parlay_id", ""))
            if pid not in seen_pids:
                seen_pids[pid] = []
            seen_pids[pid].append(s)
        for pid, legs in seen_pids.items():
            name = get_name_str(legs[0]["user_id"])
            legs_str = " \u00b7 ".join(
                f"{_outcome_label(l['outcome'], match)} {'✅' if l['status'] == 'won' else '❌'}"
                for l in legs
            )
            stake = legs[0]["amount"]
            all_lost = all(l["status"] == "lost" for l in legs)
            suffix = "\u2014 dead \U0001f940" if all_lost else "\u2014 in play"
            lines.append(f"\U0001f3b0 {name}: {legs_str} \u2014 {stake}c {suffix}")

    return "\n".join(lines)




async def _katerina_line(prompt: str, fallback: str, max_tokens: int = 120) -> str:
    """Call Katerina API for a short scheduled message line. Returns fallback on failure."""
    if not ANTHROPIC_API_KEY:
        return fallback
    try:
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "system": (
                "You are Katerina, the sharp, witty, confident house bookie for WC Kings 2026. "
                "Light banter is your default — warm or playfully cheeky depending on the moment. "
                "Savage mode only when the context clearly calls for it (e.g. everyone just lost). "
                "Pure English. No markdown. No swearing. Short, punchy, personality-driven."
            ),
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": ANTHROPIC_API_KEY
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            text_blocks = [b for b in data.get("content", []) if b.get("type") == "text"]
            result = text_blocks[0]["text"].strip() if text_blocks else ""
            return result if result else fallback
    except Exception as e:
        logger.error(f"Katerina API call failed in scheduler: {e}")
        return fallback


# ── Night reminder (11PM SGT) ────────────────────────────────────────────────
async def job_night_reminder():
    try:
        today_ct = datetime.now(CT).strftime("%Y-%m-%d")
        now_utc = datetime.now(UTC)
        yesterday_utc = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
        today_utc = now_utc.strftime("%Y-%m-%d")
        tomorrow_utc = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")

        # Read from cache first — cache already holds yesterday + today + tomorrow from startup
        cached_all = list(sheet.cache.get("matches", {}).values())
        raw_from_cache = [
            m for m in cached_all
            if m.get("kickoff_utc", "") >= yesterday_utc
        ]

        raw = None
        if raw_from_cache:
            raw = raw_from_cache
            logger.info("Night reminder: using cached fixtures")
        else:
            # Cache empty — fall back to API with 5-attempt retry
            last_error = None
            for attempt in range(1, 6):
                try:
                    raw = (
                        api.fetch_matches_for_date(yesterday_utc) +
                        api.fetch_matches_for_date(today_utc) +
                        api.fetch_matches_for_date(tomorrow_utc)
                    )
                    logger.info(f"Night reminder: API fetch succeeded on attempt {attempt}")
                    break
                except RuntimeError as e:
                    last_error = e
                    logger.warning(f"Night reminder: API fetch attempt {attempt} failed: {e}")
                    if attempt < 5:
                        await asyncio.sleep(5)
            if raw is None:
                await dm_admin(f"⚠️ Night reminder: failed to fetch fixtures after 5 attempts: {last_error}")
                return

        matches = []
        for m in raw:
            try:
                ko = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                if (
                    ko.astimezone(CT).strftime("%Y-%m-%d") == today_ct
                    and ko > now_utc
                    and m.get("status") not in ("FINISHED", "CANCELLED", "POSTPONED")
                ):
                    matches.append(m)
            except Exception:
                continue

        if not matches:
            logger.info("Night reminder: no upcoming matches today CT, skipping")
            return

        lines = ["🌙 Good evening gents! Matches tonight:\n"]

        bet_context_parts = []
        for m in sorted(matches, key=lambda x: x["kickoff_utc"]):
            lines.append(f"  {format_match_line(m)}")
            open_bets = [b for b in sheet.cache["bets"] if b["match_id"] == str(m["match_id"]) and b["status"] == "open"]
            if open_bets:
                for b in sorted(open_bets, key=_get_sort_name):
                    name = _get_user_name(b["user_id"])
                    lines.append(f"  {_format_bet_line(b, m)}")
                    bet_context_parts.append(f"{name} on {_outcome_label(b['outcome'], m)} for {format_match_teams(m['home'], m['away'])}")
            else:
                lines.append("  No bets yet.")
            lines.append("")

        # Katerina closing line
        if bet_context_parts:
            bet_summary = ", ".join(bet_context_parts)
            prompt = (
                f"It's 11PM. Tonight's WC matches are set. Current bets placed: {bet_summary}. "
                f"Write one short punchy good night line — acknowledge who's bet, maybe a light dig. "
                f"1 sentence max. No hashtags."
            )
            closing = await _katerina_line(prompt, "Get your bets in before kickoff. Good night! 🌛")
        else:
            prompt = (
                f"It's 11PM. Tonight's WC matches are set but nobody has bet yet. "
                f"Write one short punchy good night line encouraging bets. 1 sentence max."
            )
            closing = await _katerina_line(prompt, "No bets placed yet. Get on it before kickoff. Good night! 🌛")

        lines.append(closing)
        await send_group("\n".join(lines))
        logger.info("Night reminder sent")
    except Exception as e:
        logger.error(f"Night reminder job failed: {e}")
        await dm_admin(f"⚠️ Night reminder job failed: {e}")



# ── Pre-match summary ─────────────────────────────────────────────────────────
async def job_prematch_summary(match_id: str):
    try:
        # Suppress during silent hours — 3AM match already covered by night reminder
        if is_silent_hours():
            logger.info(f"Pre-match summary suppressed for {match_id} — silent hours")
            return

        match = await sheet.get_match_by_id(match_id)
        if not match:
            await dm_admin(f"⚠️ Pre-match summary: match {match_id} not found")
            return

        bets = await sheet.get_bets_for_match(match_id)
        open_bets = [b for b in bets if b["status"] == "open"]
        match_label = format_match_teams(match["home"], match["away"])

        sorted_bets = sorted(open_bets, key=_get_sort_name)

        lines = [f"⚽ {match_label} kicks off in 15 mins!\n"]
        if sorted_bets:
            lines.append("Current bets:")
            for b in sorted_bets:
                lines.append(f"{_format_bet_line(b, match)}")
        else:
            lines.append("No bets placed yet.")

        # Katerina last-call line
        if sorted_bets:
            bet_summary = ", ".join(
                f"{_get_user_name(b['user_id'])} on {_outcome_label(b['outcome'], match)}"
                for b in sorted_bets
            )
            prompt = (
                f"{match_label} kicks off in 15 minutes. Bets so far: {bet_summary}. "
                f"Write one short last-call line — light banter, encourage anyone sitting out to get in. 1 sentence."
            )
            closing = await _katerina_line(prompt, "Last chance — get your bets in before kickoff! ⚽")
        else:
            prompt = (
                f"{match_label} kicks off in 15 minutes and nobody has bet yet. "
                f"Write one short last-call line — lightly cheeky, slightly incredulous. 1 sentence."
            )
            closing = await _katerina_line(prompt, "Nobody's bet yet? Last chance before kickoff! ⚽")

        lines.append(f"\n{closing}")
        await send_group("\n".join(lines))
        logger.info(f"Pre-match summary sent for {match_id}")
    except Exception as e:
        logger.error(f"Pre-match summary job failed for {match_id}: {e}")
        await dm_admin(f"⚠️ Pre-match summary job failed for match {match_id}: {e}")


# ── Kickoff message ───────────────────────────────────────────────────────────
async def job_kickoff_message(match_id: str):
    try:
        match = await sheet.get_match_by_id(match_id)
        if not match:
            return

        bets = await sheet.get_bets_for_match(match_id)
        open_bets = [b for b in bets if b["status"] == "open"]
        match_label = format_match_teams(match["home"], match["away"])

        sorted_bets = sorted(open_bets, key=_get_sort_name)

        lines = [
            f"{match_label} has kicked off!",
            "🚨 Bets are closed!",
            "",
        ]

        for b in sorted_bets:
            lines.append(f"{_format_bet_line(b, match)}")

        # Suppress during silent hours unless this is the last match
        if is_silent_hours() and not await is_last_match_of_day(match_id):
            logger.info(f"Kickoff message suppressed for {match_id} — silent hours, not last match")
            return

        # Katerina good luck / roast line
        if open_bets:
            standings = sheet.get_standings()
            bet_summary = ", ".join(
                f"{_get_user_name(b['user_id'])} on {_outcome_label(b['outcome'], match)} ({b['amount']}c)"
                for b in sorted_bets
            )
            standings_str = ", ".join(
                f"{sheet.cache['users'].get(u['user_id'], {}).get('first_name') or 'Unknown'} {u['credits']}c"
                for u in standings[:3]
            ) if standings else ""
            no_bet_names = [
                sheet.cache["users"].get(u["user_id"], {}).get("first_name") or "Unknown"
                for u in standings if u["user_id"] not in {b["user_id"] for b in open_bets}
            ]
            prompt = (
                f"{match_label} has just kicked off. Bets placed: {bet_summary}. "
                + (f"Sitting out: {', '.join(no_bet_names)}. " if no_bet_names else "")
                + (f"Current standings: {standings_str}. " if standings_str else "")
                + "Write one fun good luck line — light banter, reference specific names and their picks. "
                "1-2 sentences. Sharp and punchy."
            )
            fun_line = await _katerina_line(prompt, "Good luck everyone. May the better bets win. ⚽", max_tokens=150)
            lines.append(f"\n{fun_line}")

        await send_group("\n".join(lines))
        logger.info(f"Kickoff message sent for {match_id}")
    except Exception as e:
        logger.error(f"Kickoff message job failed for {match_id}: {e}")
        await dm_admin(f"⚠️ Kickoff message job failed for match {match_id}: {e}")


# ── Result polling ────────────────────────────────────────────────────────────
async def check_parlay_completions(match_id: str) -> list:
    """
    After a match settles, find all parlays that had a leg on this match
    and check if they're now fully settled. Returns list of payout dicts.
    """
    payouts = []
    affected_parlay_ids = set(
        b.get("parlay_id", "") for b in sheet.cache["bets"]
        if b.get("parlay_id") and b["match_id"] == str(match_id)
        and b["market"] == "result"
    )
    affected_parlay_ids.discard("")

    for pid in affected_parlay_ids:
        result = await sheet.settle_parlay(pid, notify_fn=dm_admin)
        if result:
            payouts.append((pid, result))
    return payouts


async def _auto_settle_stuck_match(match_id: str):
    """
    Settle bets for a match that's FINISHED in cache but still has open bets.
    Sends the result message to the group automatically — no manual push needed.
    """
    try:
        match = await sheet.get_match_by_id(match_id)
        if not match or not match.get("result"):
            return
        open_bets = [b for b in sheet.cache["bets"] if b["match_id"] == match_id and b["status"] == "open"]
        if not open_bets:
            await check_all_matches_done()
            return

        # Settle bets
        settlements = await sheet.settle_bets_for_match(match_id, match["result"], match.get("ou_result", ""), notify_fn=dm_admin)

        # Check parlay completions
        parlay_wins = await check_parlay_completions(match_id)

        # Is this the last match of the day?
        is_last = await is_last_match_of_day(match_id)

        # Post-match top-up
        topup_line = ""
        if not is_last:
            try:
                await sheet.add_match_credits(MATCH_CREDITS, match_id, notify_fn=dm_admin)
                topup_line = f"\n\n+{MATCH_CREDITS}c added to everyone's account. 🪙"
            except Exception as e:
                logger.error(f"Auto-settle top-up failed for {match_id}: {e}")

        # Build result message
        base_result_msg = format_result_message(match, settlements, parlay_wins=parlay_wins)
        result_msg = base_result_msg

        if settlements or parlay_wins:
            home_score = match.get("home_score", "?")
            away_score = match.get("away_score", "?")
            settled_summary = ", ".join(
                f"{_get_user_name(s['user_id'])} {'won' if s['status'] == 'won' else 'lost'} {s['amount']}c on {_outcome_label(s['outcome'], match)}"
                for s in settlements if not _is_parlay_leg(s)
            )
            context = f"Result: {format_match_teams(match['home'], match['away'])} {home_score}-{away_score}."
            if settled_summary:
                context += f" Singles: {settled_summary}."

            singles_on_match = [s for s in settlements if not _is_parlay_leg(s)]
            parlay_legs_on_match = [s for s in settlements if _is_parlay_leg(s)]
            everyone_lost = (
                bool(settlements) and not parlay_wins and
                all(s["status"] == "lost" for s in singles_on_match) and
                all(s["status"] == "lost" for s in parlay_legs_on_match)
            )
            if everyone_lost:
                names = ", ".join(dict.fromkeys(_get_user_name(s["user_id"]) for s in settlements))
                prompt = (
                    f"{context} Every single person lost — {names}. "
                    f"Go full savage. 1-2 sentences, no mercy. No markdown."
                )
            else:
                prompt = (
                    f"{context} Write 1-2 sharp sentences reacting to the result and bets with light banter. "
                    f"Reference specific names and outcomes. No markdown."
                )
            commentary = await _katerina_line(prompt, "", max_tokens=150)
            if commentary:
                result_msg = result_msg + f"\n\n{commentary}"

        # Send or hold for morning flush
        if is_silent_hours() and not is_last:
            if "held_results" not in sheet.cache:
                sheet.cache["held_results"] = []
            sheet.cache["held_results"].append(base_result_msg)
            if not scheduler.get_job("morning_flush"):
                now_sgt = datetime.now(SGT)
                send_time_sgt = now_sgt.replace(hour=MORNING_CATCHUP_HOUR, minute=MORNING_CATCHUP_MINUTE, second=0, microsecond=0)
                if send_time_sgt <= now_sgt:
                    send_time_sgt = send_time_sgt + timedelta(days=1)
                send_time_utc = send_time_sgt.astimezone(UTC)
                scheduler.add_job(
                    _send_morning_flush,
                    trigger=DateTrigger(run_date=send_time_utc),
                    id="morning_flush",
                    replace_existing=True
                )
        else:
            if topup_line:
                result_msg = result_msg + topup_line
            await send_group(result_msg)
            if not is_last:
                await _send_coming_up_today()

        await dm_admin(f"ℹ️ Match {match_id} was auto-settled and result posted to group.")
        logger.info(f"Auto-settled stuck match {match_id}")
        await check_all_matches_done(match_id)

    except Exception as e:
        logger.error(f"Auto-settle failed for {match_id}: {e}")
        await dm_admin(f"⚠️ Auto-settle failed for match {match_id}: {e}")


async def job_poll_result(match_id: str, attempt: int = 1):
    MAX_POLL_ATTEMPTS = 36  # 36 x 5min = 3 hours max
    try:
        logger.info(f"Polling result for match {match_id} (attempt {attempt})")
        try:
            result_data = api.fetch_match_result(match_id)
        except RuntimeError as e:
            error_msg = str(e)
            if "429" in error_msg or "rate limit" in error_msg.lower():
                if attempt < MAX_POLL_ATTEMPTS:
                    await dm_admin(f"⚠️ API rate limit hit polling match {match_id} — retrying in 10 minutes.")
                    _schedule_poll(match_id, delay_seconds=10 * 60, attempt=attempt + 1)
                else:
                    await dm_admin(f"⚠️ Match {match_id} polling gave up after {MAX_POLL_ATTEMPTS} attempts. Use /admin_result to settle manually.")
                return
            await dm_admin(f"⚠️ API error polling match {match_id}: {e}")
            if attempt < MAX_POLL_ATTEMPTS:
                _schedule_poll(match_id, delay_seconds=POLL_INTERVAL, attempt=attempt + 1)
            else:
                await dm_admin(f"⚠️ Match {match_id} polling gave up after {MAX_POLL_ATTEMPTS} attempts. Use /admin_result to settle manually.")
            return

        if not result_data:
            logger.info(f"Match {match_id} not finished yet, rescheduling poll")
            if attempt < MAX_POLL_ATTEMPTS:
                _schedule_poll(match_id, delay_seconds=POLL_INTERVAL, attempt=attempt + 1)
            else:
                await dm_admin(f"⚠️ Match {match_id} still not finished after {MAX_POLL_ATTEMPTS} attempts. Use /admin_result to settle manually.")
            return

        # Result confirmed — settle bets
        home_score = result_data["home_score"]
        away_score = result_data["away_score"]
        result, ou_result = await sheet.update_match_result(match_id, home_score, away_score, notify_fn=dm_admin)
        settlements = await sheet.settle_bets_for_match(match_id, result, ou_result, notify_fn=dm_admin)

        # Check parlay completions after settlement
        parlay_wins = await check_parlay_completions(match_id)

        match = await sheet.get_match_by_id(match_id)
        is_last = await is_last_match_of_day(match_id)

        # Post-match top-up — all users except after last match (EOD handles that)
        topup_line = ""
        if not is_last:
            try:
                await sheet.add_match_credits(MATCH_CREDITS, match_id, notify_fn=dm_admin)
                topup_line = f"\n\n+{MATCH_CREDITS}c added to everyone's account. 🪙"
            except Exception as e:
                logger.error(f"Post-match top-up failed for {match_id}: {e}")
                await dm_admin(f"⚠️ Post-match top-up failed for match {match_id}: {e}")

        # Build result message — Katerina commentary injected before top-up line
        base_result_msg = format_result_message(match, settlements, parlay_wins=parlay_wins)
        result_msg = base_result_msg

        if settlements or parlay_wins:
            settled_summary = ", ".join(
                f"{_get_user_name(s['user_id'])} {'won' if s['status'] == 'won' else 'lost'} {s['amount']}c on {_outcome_label(s['outcome'], match)}"
                for s in settlements if not (s.get("parlay_id") and str(s.get("parlay_id")) not in ("", "0"))
            )
            parlay_summary = ", ".join(
                f"{_get_user_name(p['user_id'])} hit {p['legs']}-leg parlay {p['stake']}c→{p['payout']}c"
                for _, p in (parlay_wins or [])
            )
            context = f"Result: {format_match_teams(match['home'], match['away'])} {home_score}-{away_score}."
            if settled_summary:
                context += f" Singles: {settled_summary}."
            if parlay_summary:
                context += f" Parlay wins: {parlay_summary}."

            # Detect if everyone lost — singles + all parlay legs on this match
            singles_on_match = [s for s in settlements if not _is_parlay_leg(s)]
            parlay_legs_on_match = [s for s in settlements if _is_parlay_leg(s)]
            everyone_lost = (
                bool(settlements) and
                not parlay_wins and
                all(s["status"] == "lost" for s in singles_on_match) and
                all(s["status"] == "lost" for s in parlay_legs_on_match)
            )

            if everyone_lost:
                names = ", ".join(dict.fromkeys(_get_user_name(s["user_id"]) for s in settlements))
                prompt = (
                    f"{context} Every single person lost — {names}. "
                    f"Go full savage. 1-2 sentences, no mercy. Reference specific names and their losing picks. No markdown."
                )
            else:
                prompt = (
                    f"{context} Write 1-2 sharp sentences reacting to the result and bets with light banter. "
                    f"Reference specific names and outcomes. No markdown."
                )
            commentary = await _katerina_line(prompt, "", max_tokens=150)
            if commentary:
                result_msg = result_msg + f"\n\n{commentary}"

        if is_silent_hours() and not is_last:
            logger.info(f"Match {match_id} result held — silent hours, appending to morning flush")
            # Hold only base result (score + bets) — morning flush adds one combined Katerina + top-up
            if "held_results" not in sheet.cache:
                sheet.cache["held_results"] = []
            sheet.cache["held_results"].append(base_result_msg)

            # Schedule morning flush if not already scheduled
            if not scheduler.get_job("morning_flush"):
                now_sgt = datetime.now(SGT)
                send_time_sgt = now_sgt.replace(hour=MORNING_CATCHUP_HOUR, minute=MORNING_CATCHUP_MINUTE, second=0, microsecond=0)
                if send_time_sgt <= now_sgt:
                    send_time_sgt = send_time_sgt + timedelta(days=1)
                send_time_utc = send_time_sgt.astimezone(UTC)
                scheduler.add_job(
                    _send_morning_flush,
                    trigger=DateTrigger(run_date=send_time_utc),
                    id="morning_flush",
                    replace_existing=True
                )
                logger.info(f"Morning flush scheduled at {send_time_utc}")
        else:
            if topup_line:
                result_msg = result_msg + topup_line
            await send_group(result_msg)
            # Fire "Coming up today" follow-up if more matches remain
            if not is_last:
                await _send_coming_up_today()

        # Check if all matches today are done
        await check_all_matches_done(match_id)

    except Exception as e:
        logger.error(f"Poll result job failed for {match_id}: {e}")
        await dm_admin(f"⚠️ Poll result job failed for match {match_id}: {e}")


async def _send_morning_flush():
    """Send all held overnight results as one combined message at 7:30AM, then fire coming up today."""
    try:
        held = sheet.cache.pop("held_results", [])
        if not held:
            logger.info("Morning flush: no held results to send")
            await _send_coming_up_today()
            return

        match_count = len(held)
        total_topup = MATCH_CREDITS * match_count

        # Build combined message
        lines = ["Good morning lads! 🌅"]
        for i, result_block in enumerate(held):
            lines.append("")
            lines.append(result_block)

        # Single Katerina commentary covering all overnight results
        all_results_summary = f"{match_count} overnight results: " + " | ".join(
            r.split("\n")[0] for r in held  # first line of each result block = scoreline
        )
        prompt = (
            f"{all_results_summary}. Write one sharp line reacting to the overnight results overall — "
            f"light banter. Reference names if notable outcomes. 1-2 sentences. No markdown."
        )
        commentary = await _katerina_line(prompt, "", max_tokens=120)
        if commentary:
            lines.append("")
            lines.append(commentary)

        # Combined top-up line
        lines.append("")
        if match_count == 1:
            lines.append(f"+{MATCH_CREDITS}c added to everyone's account. 🪙")
        else:
            lines.append(f"+{MATCH_CREDITS}c × {match_count} added to everyone's account. 🪙")

        await send_group("\n".join(lines))
        logger.info(f"Morning flush sent: {match_count} result(s)")

        # Fire coming up today once
        await _send_coming_up_today()

    except Exception as e:
        logger.error(f"Morning flush failed: {e}")
        await dm_admin(f"⚠️ Morning flush failed: {e}")


async def _send_coming_up_today():
    """Send a 'Coming up today' follow-up message showing remaining matches and bets."""
    try:
        today_matches = await get_today_ct_matches()
        remaining = [m for m in today_matches if m["status"] not in ("FINISHED", "CANCELLED", "POSTPONED")]
        if not remaining:
            return

        lines = ["📅 Coming up today:"]
        for m in sorted(remaining, key=lambda x: x["kickoff_utc"]):
            home_d = format_team(m["home"])
            away_d = format_team(m["away"])
            if m["status"] in ("IN_PLAY", "PAUSED", "HALFTIME"):
                lines.append(f"\n{home_d} vs {away_d} — ● In Play")
            else:
                kickoff_utc = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                time_str = kickoff_utc.astimezone(SGT).strftime("%I:%M %p SGT").lstrip("0")
                lines.append(f"\n{home_d} vs {away_d} — {time_str}")

            open_bets = [b for b in sheet.cache["bets"] if b["match_id"] == str(m["match_id"]) and b["status"] == "open"]
            if open_bets:
                for b in sorted(open_bets, key=_get_sort_name):
                    lines.append(f"  {_format_bet_line(b, m)}")

        await send_group("\n".join(lines))
        logger.info("Coming up today sent")
    except Exception as e:
        logger.error(f"_send_coming_up_today failed: {e}")
        await dm_admin(f"⚠️ Coming up today message failed: {e}")


def trigger_poll(match_id: str):
    """Manually trigger an immediate poll for a match (admin use)."""
    _schedule_poll(match_id, delay_seconds=0, attempt=99)
    logger.info(f"Admin triggered poll for match {match_id}")


def _schedule_poll(match_id: str, delay_seconds: int, attempt: int):
    # Add 30s offset to avoid colliding with cache refresh jobs at :00
    run_time = datetime.now(UTC) + timedelta(seconds=delay_seconds + 30)
    job_id = f"poll_{match_id}_attempt_{attempt}"
    scheduler.add_job(
        job_poll_result,
        trigger=DateTrigger(run_date=run_time),
        args=[match_id, attempt],
        id=job_id,
        replace_existing=True
    )
    logger.info(f"Scheduled poll for match {match_id} at {run_time} (attempt {attempt})")


# ── Check all matches done → fire standings ───────────────────────────────────
async def check_all_matches_done(match_id: str = None):
    """Fire EOD when the last match of the CT day is finished.
    Pass match_id to resolve the correct CT date from the match's kickoff —
    avoids wrong-day lookups when the bot restarts after midnight UTC."""
    try:
        if match_id:
            match = await sheet.get_match_by_id(match_id)
            if not match or not match.get("kickoff_utc"):
                return
            ko = datetime.strptime(match["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            target_ct_date = ko.astimezone(CT).strftime("%Y-%m-%d")
        else:
            target_ct_date = datetime.now(CT).strftime("%Y-%m-%d")

        today_matches = await get_ct_date_matches(target_ct_date)
        if not today_matches:
            return

        # Guard against double-fire
        if sheet.cache.get("eod_date") == target_ct_date:
            logger.info(f"EOD already fired for CT {target_ct_date}, skipping")
            return

        # EOD fires when the last match by kickoff time is finished
        active = [m for m in today_matches if m.get("status") not in ("CANCELLED", "POSTPONED")]
        if not active:
            return
        latest = max(active, key=lambda m: m.get("kickoff_utc", ""))
        if latest["status"] not in ("FINISHED", "CANCELLED", "POSTPONED"):
            return

        sheet.cache["eod_date"] = target_ct_date
        logger.info(f"Last match done for CT {target_ct_date} — firing standings")
        match_ids = [str(m["match_id"]) for m in today_matches]
        await job_post_standings(match_ids)

    except Exception as e:
        logger.error(f"check_all_matches_done failed: {e}")
        await dm_admin(f"⚠️ Failed to check if all matches done: {e}")


# ── Post standings + daily credits ───────────────────────────────────────────
async def job_post_standings(match_ids: list):
    try:
        # Force fresh data before P&L and credit calculations
        await sheet.refresh_cache(notify_fn=dm_admin)

        # Normalise all match_ids to str to avoid int/str comparison mismatches
        match_ids = [str(mid) for mid in match_ids]

        today_matches = []
        for mid in match_ids:
            m = await sheet.get_match_by_id(mid)
            if m:
                today_matches.append(m)

        # Get standings BEFORE adding credits (for P&L calculation)
        pl_map = sheet.get_daily_pl(match_ids)
        standings_before = sheet.get_standings()

        # Parlay settlement — check all parlays with legs in today's matches
        parlay_payouts = {}  # parlay_id -> {uid, legs, stake, multiplier, payout}
        today_parlay_ids = set(
            b.get("parlay_id", "") for b in sheet.cache["bets"]
            if b.get("parlay_id") and b["match_id"] in match_ids
            and b["market"] == "result"
        )
        today_parlay_ids.discard("")

        for pid in today_parlay_ids:
            # Skip already paid out (settled after last leg)
            if pid in sheet.cache.get("paid_parlays", set()):
                continue

            legs = sheet.get_parlay_bets(pid)
            if not legs:
                continue
            uid = legs[0]["user_id"]
            stake = legs[0]["amount"]  # same amount on all legs

            # Separate settled from voided (voided = match cancelled)
            active_legs = [b for b in legs if b["status"] in ("won", "lost", "void")]
            settled_legs = [b for b in active_legs if b["status"] in ("won", "lost")]
            open_legs = [b for b in legs if b["status"] == "open"]

            if open_legs:
                await dm_admin(f"⚠️ Parlay {pid} has {len(open_legs)} unsettled leg(s) at EOD — skipped. Settle bets manually then re-push EOD.")
                continue

            if not settled_legs:
                continue  # nothing settled yet

            # Drop voided legs — recalculate multiplier on remaining settled legs
            all_won = all(b["status"] == "won" for b in settled_legs)
            effective_legs = len(settled_legs)

            if all_won and effective_legs >= 2:
                multiplier = PARLAY_MULTIPLIERS.get(effective_legs, PARLAY_MULTIPLIERS[4] if effective_legs > 4 else None)
                if not multiplier:
                    continue
                payout = int(stake * multiplier)
                net = payout - stake
                parlay_payouts[pid] = {
                    "user_id": uid,
                    "legs": effective_legs,
                    "stake": stake,
                    "multiplier": multiplier,
                    "payout": payout,
                    "net": net
                }

        # Credit parlay winners
        parlay_losses = {}  # parlay_id -> {uid, legs, stake}
        if parlay_payouts:
            for pid, p in parlay_payouts.items():
                uid = p["user_id"]
                user = sheet.cache["users"].get(uid)
                if user:
                    new_credits = user["credits"] + p["payout"]
                    await sheet.update_user_credits(uid, new_credits, notify_fn=dm_admin)
                    await sheet.append_ledger(
                        uid, "payout", p["payout"], new_credits,
                        f"Parlay {pid} won ({p['legs']} legs x{p['multiplier']})",
                        notify_fn=dm_admin
                    )

        # Collect losing parlays for display
        # Do NOT skip paid_parlays here — losses are marked paid mid-day by settle_parlay
        # and would otherwise never show the fallen rose at EOD
        for pid in today_parlay_ids:
            if pid in parlay_payouts:
                continue  # already a winner, skip
            legs = sheet.get_parlay_bets(pid)
            if not legs:
                continue
            open_legs = [b for b in legs if b["status"] == "open"]
            if open_legs:
                continue  # still in play, skip
            settled = [b for b in legs if b["status"] in ("won", "lost")]
            if not settled:
                continue
            if any(b["status"] == "lost" for b in settled):
                parlay_losses[pid] = {
                    "user_id": legs[0]["user_id"],
                    "legs": len(settled),
                    "stake": legs[0]["amount"],
                }

        # Add tiered daily credits — lower ranks get more, tied players get most generous tier
        tier_amounts = DAILY_CREDIT_TIERS
        tier_map = {}
        i = 0
        rank_pos = 0
        while i < len(standings_before):
            j = i
            while j < len(standings_before) and standings_before[j]["credits"] == standings_before[i]["credits"]:
                j += 1
            worst_idx = min(rank_pos + (j - i) - 1, len(tier_amounts) - 1)
            amount = tier_amounts[worst_idx]
            for k in range(i, j):
                tier_map[standings_before[k]["user_id"]] = amount
            rank_pos += (j - i)
            i = j
        await sheet.add_tiered_daily_credits(tier_map, notify_fn=dm_admin)

        # Get standings AFTER credits added
        standings_after = sheet.get_standings()

        # Overtakes — compute before building message
        before_ranks = {u["user_id"]: i+1 for i, u in enumerate(standings_before)}
        after_ranks = {u["user_id"]: i+1 for i, u in enumerate(standings_after)}
        overtakes = []
        for uid, new_rank in after_ranks.items():
            old_rank = before_ranks.get(uid, new_rank)
            if new_rank < old_rank:
                passed = [u for u, r in after_ranks.items() if before_ranks.get(u, r) < old_rank and r >= new_rank and u != uid]
                for passed_uid in passed:
                    overtakes.append((uid, passed_uid))

        # Build EOD message
        from datetime import date as _date_cls
        sgt_date = datetime.now(SGT).strftime("%d %b")
        lines = [f"📅 End of Day — {sgt_date}\n"]

        # Match results
        for m in sorted(today_matches, key=lambda x: x["kickoff_utc"]):
            if m["status"] == "FINISHED":
                home_d = format_team(m["home"])
                away_d = format_team(m["away"])
                ou_label = "Over 2.5" if m["ou_result"] == "over" else "Under 2.5"
                lines.append(f"{home_d} {m['home_score']}–{m['away_score']} {away_d} · {ou_label}")

        lines.append("\n🏆 Standings")

        for i, user in enumerate(standings_after, 1):
            name = _get_user_name(user["user_id"])
            credits = user["credits"]
            pl = pl_map.get(user["user_id"], 0)
            pl_str = f"+{pl}c" if pl > 0 else f"{pl}c"
            badge = " 🏆" if i == 1 else ""
            lines.append(f"{i}. {name}{badge} — {credits}c ({pl_str} today)")

        # Parlay section — right after standings
        if parlay_payouts or parlay_losses:
            lines.append("")
            for pid, p in parlay_payouts.items():
                name = _get_user_name(p["user_id"])
                lines.append(f"🎰 {name} hit a {p['legs']}-leg parlay! {p['stake']}c → {p['payout']}c 🔥")
            for pid, p in parlay_losses.items():
                name = _get_user_name(p["user_id"])
                lines.append(f"🥀 {name}'s {p['legs']}-leg parlay didn't make it. {p['stake']}c gone.")

        # Commentary — Katerina generates 2-3 sentences
        standings_str = "\n".join(
            f"{i}. {_get_user_name(u['user_id'])} — {u['credits']}c ({'+' if pl_map.get(u['user_id'],0)>0 else ''}{pl_map.get(u['user_id'],0)}c today)"
            for i, u in enumerate(standings_after, 1)
        )
        parlay_win_str = ", ".join(
            f"{_get_user_name(p['user_id'])} hit {p['legs']}-leg parlay {p['stake']}c→{p['payout']}c"
            for p in parlay_payouts.values()
        ) if parlay_payouts else ""
        parlay_loss_str = ", ".join(
            f"{_get_user_name(p['user_id'])}'s {p['legs']}-leg parlay busted"
            for p in parlay_losses.values()
        ) if parlay_losses else ""
        overtake_str = ", ".join(
            f"{_get_user_name(uid)} overtook {_get_user_name(passed)}"
            for uid, passed in overtakes
        ) if overtakes else ""

        # Additional context for Katerina
        from datetime import date as _date_cls
        days_to_final = (TOURNAMENT_FINAL_DATE - _date_cls.today()).days

        match_results_str = ", ".join(
            f"{m['home']} {m['home_score']}-{m['away_score']} {m['away']}"
            for m in sorted(today_matches, key=lambda x: x["kickoff_utc"])
            if m["status"] == "FINISHED"
        ) if today_matches else ""

        if pl_map:
            top_winner_uid = max(pl_map, key=lambda uid: pl_map[uid])
            top_loser_uid = min(pl_map, key=lambda uid: pl_map[uid])
            winner_str = f"{_get_user_name(top_winner_uid)} +{pl_map[top_winner_uid]}c" if pl_map[top_winner_uid] > 0 else ""
            loser_str = f"{_get_user_name(top_loser_uid)} {pl_map[top_loser_uid]}c" if pl_map[top_loser_uid] < 0 else ""
        else:
            winner_str = loser_str = ""

        gap_str = ""
        if len(standings_after) >= 2:
            gap = standings_after[0]["credits"] - standings_after[1]["credits"]
            gap_str = f"{_get_user_name(standings_after[0]['user_id'])} leads by {gap}c"

        eod_prompt = (
            f"End of day for WC Kings 2026 ({days_to_final} days to the Final). "
            f"Write 2-3 sharp, punchy sentences as Katerina the house bookie. Be specific — name names.\n"
            + (f"Today's matches: {match_results_str}\n" if match_results_str else "")
            + f"Standings:\n{standings_str}\n"
            + (f"Biggest winner today: {winner_str}\n" if winner_str else "")
            + (f"Biggest loser today: {loser_str}\n" if loser_str else "")
            + (f"Leader gap: {gap_str}\n" if gap_str else "")
            + (f"Parlay wins: {parlay_win_str}\n" if parlay_win_str else "")
            + (f"Parlay losses: {parlay_loss_str}\n" if parlay_loss_str else "")
            + (f"Overtakes: {overtake_str}\n" if overtake_str else "")
            + "No markdown. No hashtags. Hype the winner, roast the loser. "
            + "Reference the prize (champion jersey + dining vouchers for 1st, runner-up jersey for 2nd) for extra sting."
        )

        if pl_map:
            commentary = await _katerina_line(eod_prompt, "", max_tokens=200)
        else:
            commentary = await _katerina_line(
                "Nobody placed any bets today. Write one short, slightly mocking line about it. 1 sentence.",
                "Nobody put money down today. Bold strategy. 🤔",
                max_tokens=80
            )

        if commentary:
            lines.append("")
            lines.append(commentary)

        tier_values = sorted(set(tier_map.values()))
        if len(tier_values) > 1:
            lines.append(f"\n+{tier_values[0]}c–{tier_values[-1]}c daily credits added (by rank), good luck tomorrow! 🍀")
        else:
            lines.append(f"\n+{tier_values[0]}c daily credits added, good luck tomorrow! 🍀")

        await send_group("\n".join(lines))
        logger.info("End of day standings and daily credits posted")

        # Stage transition check — fire Katerina hype in background if today ends a stage
        import katerina as _katerina
        asyncio.create_task(_katerina.check_and_send_stage_hype(notify_fn=dm_admin))

    except Exception as e:
        logger.error(f"Post standings job failed: {e}")
        await dm_admin(f"⚠️ Post standings job failed: {e}")


# ── Cache refresh job ─────────────────────────────────────────────────────────
async def job_refresh_cache():
    await sheet.refresh_cache(notify_fn=dm_admin)
    # Re-fetch yesterday/today/tomorrow from API to catch UTC boundary matches
    try:
        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        for date_str in [yesterday, today, tomorrow]:
            try:
                for m in api.fetch_matches_for_date(date_str):
                    try:
                        await sheet.upsert_match(m, notify_fn=dm_admin)
                    except Exception:
                        pass  # Individual upsert failure — skip, don't break full refresh
                    await asyncio.sleep(0.3)  # Rate limit: space out sheet writes
            except RuntimeError:
                pass  # API fetch failure — skip date, don't break full refresh
    except Exception as e:
        logger.warning(f"Cache refresh API upsert failed: {e}")


# ── Health monitor job ────────────────────────────────────────────────────────
async def job_health_monitor():
    """
    Periodic health check. Runs every 15min during peak (9PM-1AM SGT),
    every 1h outside peak. DMs admin only if something looks wrong.
    """
    try:
        issues = []

        # 1. Cache freshness — should have been refreshed within last 15 min
        last_refresh = sheet.cache.get("last_refresh")
        if last_refresh:
            age_minutes = (datetime.now(UTC) - last_refresh.astimezone(UTC)).total_seconds() / 60
            if age_minutes > 15:
                issues.append(f"Cache stale — last refresh {int(age_minutes)}min ago")
        else:
            issues.append("Cache has never been refreshed")

        # 2. Scheduler jobs still registered
        job_ids = {job.id for job in scheduler.get_jobs()}
        if "night_reminder" not in job_ids:
            issues.append("night_reminder job missing from scheduler")
        if "cache_refresh" not in job_ids:
            issues.append("cache_refresh job missing from scheduler")

        # 3. Any match IN_PLAY or PAUSED with no poll job registered
        for m in sheet.cache.get("matches", {}).values():
            if m.get("status") in ("IN_PLAY", "PAUSED"):
                mid = str(m["match_id"])
                has_poll = any(mid in job.id and "poll" in job.id for job in scheduler.get_jobs())
                if not has_poll:
                    _schedule_poll(mid, delay_seconds=10, attempt=1)
                    issues.append(f"Match {mid} is {m['status']} — no poll job found, scheduled emergency poll")

        # 4. Bot can reach group chat — lightweight check via _group_chat_id
        if _group_chat_id is None:
            issues.append("Group chat ID not set — bot may not be connected to group")

        if issues:
            header = "⚠️ Health monitor:"
            msg = header + "\n" + "\n".join(f"• {i}" for i in issues)
            await dm_admin(msg)
            logger.warning(f"Health monitor flagged {len(issues)} issue(s)")
        else:
            logger.info("Health monitor: all checks passed")

    except Exception as e:
        logger.error(f"Health monitor failed: {e}")


# ── Register daily jobs ───────────────────────────────────────────────────────
def register_static_jobs():
    """Register fixed-time daily jobs. Called on startup."""

    # Night reminder — 11PM SGT daily
    scheduler.add_job(
        job_night_reminder,
        trigger=CronTrigger(hour=NIGHT_REMINDER_HOUR, minute=NIGHT_REMINDER_MINUTE, timezone=SGT),
        id="night_reminder",
        replace_existing=True,
        misfire_grace_time=600
    )

    # Cache refresh — staggered to avoid colliding with cron jobs at :00 and :30
    scheduler.add_job(
        job_refresh_cache,
        trigger=CronTrigger(minute="5,15,25,35,45,55"),
        id="cache_refresh",
        replace_existing=True
    )

    # Health monitor — every 10 minutes all day
    scheduler.add_job(
        job_health_monitor,
        trigger=CronTrigger(minute="0,10,20,30,40,50"),
        id="health_monitor",
        replace_existing=True
    )

    logger.info("Static jobs registered")


def register_match_jobs(matches: list):
    """
    Register per-match jobs for today's matches.
    Called on startup and after admin_refresh.
    """
    now_utc = datetime.now(UTC)

    for m in matches:
        match_id = str(m["match_id"])
        status = m.get("status", "")

        if status in ("FINISHED", "CANCELLED", "POSTPONED"):
            if status == "FINISHED":
                # Match marked FINISHED but bets may still be open (e.g. force-synced via /admin_refresh)
                open_bets = [b for b in sheet.cache["bets"] if b["match_id"] == match_id and b["status"] == "open"]
                if open_bets and m.get("result"):
                    scheduler.add_job(
                        _auto_settle_stuck_match,
                        trigger=DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=5)),
                        args=[match_id],
                        id=f"auto_settle_{match_id}",
                        replace_existing=True
                    )
                    logger.info(f"Match {match_id} FINISHED with open bets — scheduled auto-settle")
            continue

        if status in ("IN_PLAY", "PAUSED"):
            # Bot restarted mid-match — schedule immediate poll
            _schedule_poll(match_id, delay_seconds=10, attempt=1)
            logger.info(f"Bot restarted mid-match {match_id} ({status}) — scheduling immediate poll")
            continue

        try:
            kickoff_utc = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except Exception as e:
            logger.error(f"Invalid kickoff_utc for match {match_id}: {e}")
            continue

        kickoff_sgt = kickoff_utc.astimezone(SGT)

        # Pre-match summary — 15 min before kickoff, only if after 7:30AM SGT
        summary_time = kickoff_utc - timedelta(minutes=PREMATCH_SUMMARY_MINUTES)
        cutoff_sgt = kickoff_sgt.replace(hour=MORNING_CATCHUP_HOUR, minute=MORNING_CATCHUP_MINUTE, second=0)
        if summary_time > now_utc and kickoff_sgt > cutoff_sgt:
            scheduler.add_job(
                job_prematch_summary,
                trigger=DateTrigger(run_date=summary_time),
                args=[match_id],
                id=f"prematch_{match_id}",
                replace_existing=True,
                misfire_grace_time=60
            )
            logger.info(f"Scheduled pre-match summary for {match_id} at {summary_time}")

        # Kickoff message — at exact kickoff time
        if kickoff_utc > now_utc:
            scheduler.add_job(
                job_kickoff_message,
                trigger=DateTrigger(run_date=kickoff_utc + timedelta(seconds=60)),
                args=[match_id],
                id=f"kickoff_{match_id}",
                replace_existing=True
            )
            logger.info(f"Scheduled kickoff message for {match_id} at {kickoff_utc}")

        # Result polling — starts kickoff + 120 min
        is_knockout = m.get("round", "").upper() not in ("GROUP_STAGE", "")
        offset = POLL_START_OFFSET if not is_knockout else (KNOCKOUT_DURATION + 5 * 60)
        poll_start = kickoff_utc + timedelta(seconds=offset)

        if poll_start > now_utc:
            _schedule_poll(match_id, delay_seconds=int((poll_start - now_utc).total_seconds()), attempt=1)
        else:
            # Kickoff already passed and poll start is in the past — poll immediately
            if status != "FINISHED":
                _schedule_poll(match_id, delay_seconds=10, attempt=1)

    logger.info(f"Match jobs registered for {len(matches)} matches")


# ── Startup ───────────────────────────────────────────────────────────────────
async def on_startup(notify_fn=None):
    """
    Full startup sequence:
    1. Refresh cache
    2. Load today's fixtures from API
    3. Register all jobs
    4. DM admin
    """
    try:
        await sheet.refresh_cache(notify_fn=dm_admin)
        await asyncio.sleep(5)  # Brief pause after cache read to avoid Sheets rate limit

        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            yesterday_matches = api.fetch_matches_for_date(yesterday)
            matches = api.fetch_today_matches()
            tomorrow_matches = api.fetch_matches_for_date(tomorrow)
            for m in yesterday_matches + matches + tomorrow_matches:
                try:
                    await sheet.upsert_match(m, notify_fn=dm_admin)
                except Exception as e:
                    logger.warning(f"Startup: upsert skipped for match {m.get('match_id', '?')}: {e}")
                await asyncio.sleep(0.5)  # Rate limit: space out sheet writes
        except RuntimeError as e:
            await dm_admin(f"⚠️ Startup: failed to fetch today's fixtures: {e}\nUse /admin_refresh to retry.")

        register_static_jobs()

        yesterday_cached = await sheet.get_matches_for_date(yesterday)
        today_matches = await sheet.get_matches_for_date(today)
        tomorrow_matches_cached = await sheet.get_matches_for_date(tomorrow)
        all_today_matches = yesterday_cached + today_matches + tomorrow_matches_cached
        register_match_jobs(all_today_matches)

        scheduler.start()

        # Startup recovery — fire night reminder if bot restarted during 11PM hour
        now_sgt = datetime.now(SGT)
        if now_sgt.hour == NIGHT_REMINDER_HOUR:
            logger.info("Startup during 11PM hour — firing night reminder immediately")
            scheduler.add_job(job_night_reminder, trigger=DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=5)), id="night_reminder_recovery", replace_existing=True)

        if _bot is not None:
            await dm_admin(
                f"✅ Degen v{BOT_VERSION} is up and running\n"
                f"Sheet: Connected\n"
                f"Matches today: {len(all_today_matches)}\n"
                f"Scheduler: {len(scheduler.get_jobs())} jobs active"
            )

        logger.info("Startup complete")

    except Exception as e:
        logger.error(f"Startup failed: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Degen startup failed: {e}")
