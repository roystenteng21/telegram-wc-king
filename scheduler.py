import logging
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import (
    SGT, UTC, DAILY_CREDITS,
    NIGHT_REMINDER_HOUR, NIGHT_REMINDER_MINUTE,
    MORNING_CATCHUP_HOUR, MORNING_CATCHUP_MINUTE,
    PREMATCH_SUMMARY_MINUTES, POLL_START_OFFSET, POLL_INTERVAL,
    GROUP_STAGE_DURATION, KNOCKOUT_DURATION,
    ADMIN_TELEGRAM_ID, BOT_VERSION
)
import sheet
import api

logger = logging.getLogger(__name__)

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
async def send_group(message: str):
    try:
        await _bot.send_message(chat_id=_group_chat_id, text=message)
    except Exception as e:
        logger.error(f"Failed to send group message: {e}")
        await dm_admin(f"⚠️ Failed to send group message: {e}")


# ── Silent hours check ────────────────────────────────────────────────────────
def is_silent_hours() -> bool:
    """Returns True if current SGT time is between 12:00 AM and 7:30 AM."""
    now_sgt = datetime.now(SGT)
    start = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now_sgt.replace(hour=7, minute=30, second=0, microsecond=0)
    return start <= now_sgt < end


# ── Format helpers ────────────────────────────────────────────────────────────
def format_match_line(match: dict) -> str:
    home = match["home"][:3].upper()
    away = match["away"][:3].upper()
    kickoff_utc = datetime.strptime(match["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    kickoff_sgt = kickoff_utc.astimezone(SGT)
    time_str = kickoff_sgt.strftime("%I:%M %p SGT").lstrip("0")
    return f"{home} vs {away} — {time_str}"


def format_standings(match_ids: list = None) -> str:
    standings = sheet.get_standings()
    if not standings:
        return "No players yet."

    pl_map = sheet.get_daily_pl(match_ids) if match_ids else {}

    lines = []
    for i, user in enumerate(standings, 1):
        name = (user.get("first_name") or user.get("username") or "Unknown")[:10]
        credits = user["credits"]
        pl = pl_map.get(user["user_id"], 0)
        pl_str = f"+{pl}" if pl > 0 else str(pl)
        badge = " 🏆" if i == 1 else ""
        lines.append(f"{i}. {name}{badge}    {credits}c    {pl_str}")

    return "\n".join(lines)


def format_result_message(match: dict, settlements: list) -> str:
    home = match["home"][:3].upper()
    away = match["away"][:3].upper()
    home_score = match["home_score"]
    away_score = match["away_score"]
    result = match["result"]
    ou_result = match["ou_result"]

    result_label = "Draw" if result == "draw" else (f"{home} Win" if result == "home" else f"{away} Win")
    ou_label = "Over 2.5" if ou_result == "over" else "Under 2.5"

    lines = [
        f"🏁 FT: {home} vs {away} — {home_score}–{away_score}",
        f"Result: {result_label} · {ou_label}",
        ""
    ]

    if not settlements:
        return "\n".join(lines)

    # Sort alphabetically by first_name
    def get_name(s):
        user = sheet.cache["users"].get(s["user_id"], {})
        return (user.get("first_name") or user.get("username") or "").lower()

    sorted_settlements = sorted(settlements, key=get_name)

    lines.append(f"{'Player':<12} {'Bet':<6} {'Amt':>5} {'P&L':>6}")
    lines.append("─" * 32)

    for s in sorted_settlements:
        user = sheet.cache["users"].get(s["user_id"], {})
        name = (user.get("first_name") or user.get("username") or "?")[:10]
        outcome = s["outcome"].capitalize()
        amt = f"{s['amount']}c"
        pl = f"+{s['amount']}" if s["status"] == "won" else f"-{s['amount']}"
        lines.append(f"{name:<12} {outcome:<6} {amt:>5} {pl:>6}")

    return "\n".join(lines)


# ── Night reminder (11PM SGT) ────────────────────────────────────────────────
async def job_night_reminder():
    try:
        tomorrow = (datetime.now(SGT) + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            matches = api.fetch_matches_for_date(tomorrow)
        except RuntimeError as e:
            await dm_admin(f"⚠️ Night reminder: failed to fetch tomorrow's fixtures: {e}")
            return

        if not matches:
            logger.info("Night reminder: no matches tomorrow, skipping")
            return

        lines = ["🌙 Good evening gents! Matches tomorrow:\n"]
        for m in sorted(matches, key=lambda x: x["kickoff_utc"]):
            lines.append(f"  {format_match_line(m)}")
        lines.append("\nGet your bets in before kickoff. Good night! 🌛")

        await send_group("\n".join(lines))
        logger.info("Night reminder sent")
    except Exception as e:
        logger.error(f"Night reminder job failed: {e}")
        await dm_admin(f"⚠️ Night reminder job failed: {e}")


# ── Morning catchup (7:30AM SGT) ─────────────────────────────────────────────
async def job_morning_catchup():
    try:
        now_sgt = datetime.now(SGT)
        today = now_sgt.strftime("%Y-%m-%d")
        today_matches = await sheet.get_matches_for_date(today)

        overnight_finished = [
            m for m in today_matches
            if m["status"] == "FINISHED"
        ]

        if not overnight_finished:
            logger.info("Morning catchup: no overnight results")
            return

        lines = ["☀️ Good morning! Overnight results:\n"]
        for m in sorted(overnight_finished, key=lambda x: x["kickoff_utc"]):
            home = m["home"][:3].upper()
            away = m["away"][:3].upper()
            result_label = "Draw" if m["result"] == "draw" else (
                f"{home} Win" if m["result"] == "home" else f"{away} Win"
            )
            ou_label = "Over 2.5" if m["ou_result"] == "over" else "Under 2.5"
            lines.append(f"🏁 {home} vs {away} — {m['home_score']}–{m['away_score']} | {result_label} · {ou_label}")

        await send_group("\n".join(lines))
        logger.info("Morning catchup sent")
    except Exception as e:
        logger.error(f"Morning catchup job failed: {e}")
        await dm_admin(f"⚠️ Morning catchup job failed: {e}")


# ── Pre-match summary ─────────────────────────────────────────────────────────
async def job_prematch_summary(match_id: str):
    try:
        match = await sheet.get_match_by_id(match_id)
        if not match:
            await dm_admin(f"⚠️ Pre-match summary: match {match_id} not found")
            return

        bets = await sheet.get_bets_for_match(match_id)
        open_bets = [b for b in bets if b["status"] == "open"]

        home = match["home"][:3].upper()
        away = match["away"][:3].upper()

        if not open_bets:
            logger.info(f"Pre-match summary: no bets for {match_id}, skipping")
            return

        # Sort alphabetically
        def get_name(b):
            user = sheet.cache["users"].get(b["user_id"], {})
            return (user.get("first_name") or user.get("username") or "").lower()

        sorted_bets = sorted(open_bets, key=get_name)

        lines = [f"⚽ {home} vs {away} kicks off in 15 mins!\n"]
        lines.append(f"{'Player':<12} {'Pick':<8} {'Amt':>5}")
        lines.append("─" * 28)

        for b in sorted_bets:
            user = sheet.cache["users"].get(b["user_id"], {})
            name = (user.get("first_name") or user.get("username") or "?")[:10]
            outcome = b["outcome"].capitalize()
            amt = f"{b['amount']}c"
            lines.append(f"{name:<12} {outcome:<8} {amt:>5}")

        lines.append("\nGood luck! 🤞")
        await send_group("\n".join(lines))
        logger.info(f"Pre-match summary sent for {match_id}")
    except Exception as e:
        logger.error(f"Pre-match summary job failed for {match_id}: {e}")
        await dm_admin(f"⚠️ Pre-match summary job failed for match {match_id}: {e}")


# ── Result polling ────────────────────────────────────────────────────────────
async def job_poll_result(match_id: str, attempt: int = 1):
    try:
        logger.info(f"Polling result for match {match_id} (attempt {attempt})")
        try:
            result_data = api.fetch_match_result(match_id)
        except RuntimeError as e:
            error_msg = str(e)
            if "429" in error_msg or "rate limit" in error_msg.lower():
                await dm_admin(
                    f"⚠️ API limit reached. Cannot fetch result for match {match_id}.\n"
                    f"Use /admin_result to update manually."
                )
                return
            await dm_admin(f"⚠️ API error polling match {match_id}: {e}")
            # Reschedule retry
            _schedule_poll(match_id, delay_seconds=POLL_INTERVAL, attempt=attempt + 1)
            return

        if not result_data:
            # Not finished yet — reschedule
            logger.info(f"Match {match_id} not finished yet, rescheduling poll")
            _schedule_poll(match_id, delay_seconds=POLL_INTERVAL, attempt=attempt + 1)
            return

        # Result confirmed — settle bets
        home_score = result_data["home_score"]
        away_score = result_data["away_score"]
        result, ou_result = await sheet.update_match_result(match_id, home_score, away_score, notify_fn=dm_admin)
        settlements = await sheet.settle_bets_for_match(match_id, result, ou_result, notify_fn=dm_admin)

        match = await sheet.get_match_by_id(match_id)
        result_msg = format_result_message(match, settlements)

        if is_silent_hours():
            logger.info(f"Match {match_id} result held — silent hours")
        else:
            await send_group(result_msg)

        # Check if all matches today are done
        await check_all_matches_done()

    except Exception as e:
        logger.error(f"Poll result job failed for {match_id}: {e}")
        await dm_admin(f"⚠️ Poll result job failed for match {match_id}: {e}")


def _schedule_poll(match_id: str, delay_seconds: int, attempt: int):
    run_time = datetime.now(UTC) + timedelta(seconds=delay_seconds)
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
async def check_all_matches_done():
    try:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        today_matches = await sheet.get_matches_for_date(today)

        if not today_matches:
            return

        all_done = all(
            m["status"] in ("FINISHED", "CANCELLED", "POSTPONED")
            for m in today_matches
        )

        if not all_done:
            return

        logger.info("All matches done — firing standings")
        match_ids = [m["match_id"] for m in today_matches]
        await job_post_standings(match_ids)

    except Exception as e:
        logger.error(f"check_all_matches_done failed: {e}")
        await dm_admin(f"⚠️ Failed to check if all matches done: {e}")


# ── Post standings + daily credits ───────────────────────────────────────────
async def job_post_standings(match_ids: list):
    try:
        today_matches = []
        for mid in match_ids:
            m = await sheet.get_match_by_id(mid)
            if m:
                today_matches.append(m)

        # Build results block
        result_lines = ["📅 End of Day Results\n"]
        for m in sorted(today_matches, key=lambda x: x["kickoff_utc"]):
            if m["status"] == "FINISHED":
                home = m["home"][:3].upper()
                away = m["away"][:3].upper()
                result_label = "Draw" if m["result"] == "draw" else (
                    f"{home} Win" if m["result"] == "home" else f"{away} Win"
                )
                ou_label = "Over 2.5" if m["ou_result"] == "over" else "Under 2.5"
                result_lines.append(f"🏁 {home} vs {away} — {m['home_score']}–{m['away_score']} | {result_label} · {ou_label}")

        # Build standings block
        result_lines.append("\n🏆 Standings")
        result_lines.append(f"{'#':<3} {'Player':<12} {'Credits':>8} {'Today':>7}")
        result_lines.append("─" * 34)

        standings = sheet.get_standings()
        pl_map = sheet.get_daily_pl(match_ids)

        for i, user in enumerate(standings, 1):
            name = (user.get("first_name") or user.get("username") or "Unknown")[:10]
            credits = user["credits"]
            pl = pl_map.get(user["user_id"], 0)
            pl_str = f"+{pl}" if pl > 0 else str(pl)
            badge = " 🏆" if i == 1 else ""
            result_lines.append(f"{i:<3} {name+badge:<12} {credits:>8} {pl_str:>7}")

        await send_group("\n".join(result_lines))

        # Add daily credits
        await sheet.add_daily_credits(DAILY_CREDITS, notify_fn=dm_admin)

        # Credits message
        await send_group("Good PM gents, your day's credits have been added. Good luck today! 🍀")

        logger.info("Standings and daily credits posted")

    except Exception as e:
        logger.error(f"Post standings job failed: {e}")
        await dm_admin(f"⚠️ Post standings job failed: {e}")


# ── Cache refresh job ─────────────────────────────────────────────────────────
async def job_refresh_cache():
    await sheet.refresh_cache(notify_fn=dm_admin)


# ── Register daily jobs ───────────────────────────────────────────────────────
def register_static_jobs():
    """Register fixed-time daily jobs. Called on startup."""

    # Night reminder — 11PM SGT daily
    scheduler.add_job(
        job_night_reminder,
        trigger=CronTrigger(hour=NIGHT_REMINDER_HOUR, minute=NIGHT_REMINDER_MINUTE, timezone=SGT),
        id="night_reminder",
        replace_existing=True
    )

    # Morning catchup — 7:30AM SGT daily
    scheduler.add_job(
        job_morning_catchup,
        trigger=CronTrigger(hour=MORNING_CATCHUP_HOUR, minute=MORNING_CATCHUP_MINUTE, timezone=SGT),
        id="morning_catchup",
        replace_existing=True
    )

    # Cache refresh — every 5 minutes
    scheduler.add_job(
        job_refresh_cache,
        trigger=CronTrigger(minute="*/5"),
        id="cache_refresh",
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

        if status in ("FINISHED", "CANCELLED", "POSTPONED", "IN_PLAY", "PAUSED"):
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
                replace_existing=True
            )
            logger.info(f"Scheduled pre-match summary for {match_id} at {summary_time}")

        # Result polling — starts kickoff + 95 min
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

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        try:
            matches = api.fetch_today_matches()
            for m in matches:
                await sheet.upsert_match(m, notify_fn=dm_admin)
            await sheet.refresh_cache(notify_fn=dm_admin)
        except RuntimeError as e:
            await dm_admin(f"⚠️ Startup: failed to fetch today's fixtures: {e}\nUse /admin_refresh to retry.")

        register_static_jobs()

        today_matches = await sheet.get_matches_for_date(today)
        register_match_jobs(today_matches)

        scheduler.start()

        await dm_admin(
            f"✅ Degen v{BOT_VERSION} is up and running\n"
            f"Sheet: Connected\n"
            f"Matches today: {len(today_matches)}\n"
            f"Scheduler: {len(scheduler.get_jobs())} jobs active"
        )

        logger.info("Startup complete")

    except Exception as e:
        logger.error(f"Startup failed: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Degen startup failed: {e}")
