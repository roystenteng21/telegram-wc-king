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
    ADMIN_TELEGRAM_ID, BOT_VERSION, TEAM_DISPLAY, PARLAY_MULTIPLIERS
)
import sheet
import api

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=UTC)

# Will be set by bot.py on startup
_bot = None
_group_chat_id = None


def format_team(name: str) -> str:
    if name in TEAM_DISPLAY:
        code, flag = TEAM_DISPLAY[name]
        return f"{flag} {code}"
    return name[:3].upper()


def format_match_teams(home: str, away: str) -> str:
    return f"{format_team(home)} vs {format_team(away)}"

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


# ── Silent hours check ────────────────────────────────────────────────────────
def is_silent_hours() -> bool:
    """Returns True if current SGT time is between 12:00 AM and 7:30 AM."""
    now_sgt = datetime.now(SGT)
    start = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now_sgt.replace(hour=7, minute=30, second=0, microsecond=0)
    return start <= now_sgt < end


# ── Last match of day check ───────────────────────────────────────────────────
async def is_last_match_of_day(match_id: str) -> bool:
    """Returns True if match_id is the only unfinished match left on today's CT date."""
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
                today_matches.append(m)
        except Exception:
            continue

    for m in today_matches:
        if str(m["match_id"]) == str(match_id):
            continue
        if m["status"] not in ("FINISHED", "CANCELLED", "POSTPONED"):
            return False
    return True


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


def format_result_message(match: dict, settlements: list) -> str:
    home_display = format_team(match["home"])
    away_display = format_team(match["away"])
    ou_label = "Over 2.5" if match["ou_result"] == "over" else "Under 2.5"

    lines = [
        f"{home_display} {match['home_score']}–{match['away_score']} {away_display}",
        f"{_outcome_label(match['result'], match)} · {ou_label}",
        "",
    ]

    if not settlements:
        lines.append("No bets placed on this match.")
        return "\n".join(lines)

    def get_name(s):
        user = sheet.cache["users"].get(s["user_id"], {})
        return (user.get("first_name") or user.get("username") or "").lower()

    for s in sorted(settlements, key=get_name):
        user = sheet.cache["users"].get(s["user_id"], {})
        name = (user.get("first_name") or user.get("username") or "?")[:10]
        icon = "✅" if s["status"] == "won" else "❌"
        lines.append(f"{name} — {_outcome_label(s['outcome'], match)} — {s['amount']}c {icon}")

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

        lines = ["🌙 Good evening gents! Matches later:\n"]
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
        import random
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
            logger.info("Morning catchup: no matches today, skipping")
            return

        lines = ["☀️ Good morning! Catch up from last night:\n"]
        now_utc = datetime.now(UTC)

        overnight_match_ids = []
        upcoming_lines = []
        for kickoff_utc_dt, m in sorted(today_matches, key=lambda x: x[0]):
            home_d = format_team(m["home"])
            away_d = format_team(m["away"])
            if m["status"] == "FINISHED":
                overnight_match_ids.append(m["match_id"])
                # Only show score here if no pending result message (i.e. wasn't suppressed)
                pending_results = sheet.cache.get("pending_result_messages", [])
                if not pending_results:
                    ou_label = "Over 2.5" if m["ou_result"] == "over" else "Under 2.5"
                    lines.append(f"{home_d} {m['home_score']}–{m['away_score']} {away_d} · {ou_label}")
            elif m["status"] in ("IN_PLAY", "PAUSED", "HALFTIME"):
                lines.append(f"{home_d} vs {away_d} — ● In Play")
            else:
                time_str = kickoff_utc_dt.astimezone(SGT).strftime("%I:%M %p SGT").lstrip("0")
                upcoming_lines.append(f"{home_d} vs {away_d} — {time_str}")

        # Fun line based on overnight match results
        if overnight_match_ids:
            pl_map = sheet.get_daily_pl(overnight_match_ids)
            if pl_map:
                best_uid = max(pl_map, key=lambda u: pl_map[u])
                worst_uid = min(pl_map, key=lambda u: pl_map[u])
                best_pl = pl_map[best_uid]
                worst_pl = pl_map[worst_uid]

                def get_name(uid):
                    user = sheet.cache["users"].get(uid, {})
                    return (user.get("first_name") or user.get("username") or "Someone")

                if best_pl > 0:
                    name = get_name(best_uid)
                    fun = random.choice([
                        f"{name} won big while everyone was asleep! 💰",
                        f"{name} woke up richer today! 😏",
                        f"{name} had a good night's sleep and a good bet! 🤑",
                    ])
                elif worst_pl < 0:
                    name = get_name(worst_uid)
                    fun = random.choice([
                        f"{name} woke up to a bad surprise! 😬",
                        f"{name} lost big while everyone was sleeping! 😅",
                        f"{name} might need more than coffee this morning! ☕",
                    ])
                else:
                    fun = None

                if fun:
                    lines.append(f"\n{fun}")

        # Overnight match results held from silent hours (includes bet settlements)
        pending_results = sheet.cache.get("pending_result_messages", [])
        if pending_results:
            lines.append("\n⚽ Overnight Results:")
            for msg in pending_results:
                lines.append(f"\n{msg}")
            sheet.cache["pending_result_messages"] = []

        # Upcoming matches today
        if upcoming_lines:
            lines.append("\n📅 Coming up today:")
            for ul in upcoming_lines:
                lines.append(ul)

        # Parlay wins from silent hours
        pending = sheet.cache.get("pending_parlay_wins", [])
        if pending:
            for pid, p in pending:
                name = _get_user_name(p["user_id"])
                legs_str = "\n".join(f"• {label} ✅" for label in p["leg_labels"])
                lines.append(f"\n🎰 {name} hit a {p['legs']}-leg parlay!\n{legs_str}\n{p['stake']}c → {p['payout']}c 🔥")
            sheet.cache["pending_parlay_wins"] = []

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
        match_label = format_match_teams(match["home"], match["away"])

        def get_name(b):
            user = sheet.cache["users"].get(b["user_id"], {})
            return (user.get("first_name") or user.get("username") or "").lower()

        sorted_bets = sorted(open_bets, key=get_name)

        lines = [f"⚽ {match_label} kicks off in 15 mins!\n"]
        if sorted_bets:
            lines.append("Current bets:")
            for b in sorted_bets:
                user = sheet.cache["users"].get(b["user_id"], {})
                name = (user.get("first_name") or user.get("username") or "?")[:10]
                lines.append(f"{name} — {_outcome_label(b['outcome'], match)} — {b['amount']}c")
        else:
            lines.append("No bets placed yet.")

        lines.append("\nGet your bets in before kickoff! ⚽")
        await send_group("\n".join(lines))
        logger.info(f"Pre-match summary sent for {match_id}")
    except Exception as e:
        logger.error(f"Pre-match summary job failed for {match_id}: {e}")
        await dm_admin(f"⚠️ Pre-match summary job failed for match {match_id}: {e}")


# ── Kickoff message ───────────────────────────────────────────────────────────
async def job_kickoff_message(match_id: str):
    try:
        import random
        match = await sheet.get_match_by_id(match_id)
        if not match:
            return

        bets = await sheet.get_bets_for_match(match_id)
        open_bets = [b for b in bets if b["status"] == "open"]
        match_label = format_match_teams(match["home"], match["away"])

        def get_name_str(uid):
            user = sheet.cache["users"].get(uid, {})
            return (user.get("first_name") or user.get("username") or "Someone")

        def get_sort_name(b):
            user = sheet.cache["users"].get(b["user_id"], {})
            return (user.get("first_name") or user.get("username") or "").lower()

        sorted_bets = sorted(open_bets, key=get_sort_name)

        lines = [
            f"{match_label} has kicked off!",
            "🚨 Bets are closed!",
            "",
        ]

        for b in sorted_bets:
            user = sheet.cache["users"].get(b["user_id"], {})
            name = (user.get("first_name") or user.get("username") or "?")[:10]
            lines.append(f"{name} — {_outcome_label(b['outcome'], match)} — {b['amount']}c")

        # Fun line
        standings = sheet.get_standings()
        fun_line = None

        if not open_bets:
            pass  # no bets, no fun line
        elif len(set(b["user_id"] for b in open_bets)) == 1:
            name = get_name_str(open_bets[0]["user_id"])
            fun_line = random.choice([
                f"Brave soul {name}! Everyone else folded. 🫡",
                f"{name} going solo today! 🕺",
                f"Just {name}? Bold move. 👏",
            ])
        else:
            # Check biggest bet
            bet_totals = {}
            for b in open_bets:
                bet_totals[b["user_id"]] = bet_totals.get(b["user_id"], 0) + b["amount"]
            max_uid = max(bet_totals, key=lambda u: bet_totals[u])
            max_amt = bet_totals[max_uid]
            avg_amt = sum(bet_totals.values()) / len(bet_totals)

            if max_amt > avg_amt * 2:
                name = get_name_str(max_uid)
                fun_line = random.choice([
                    f"{name} going all in today! 😤",
                    f"Big money from {name} today! 💰",
                    f"Confident or crazy {name}? Big bet! 👀",
                ])
            elif standings:
                # Check if leader didn't bet
                leader = standings[0]
                leader_uid = leader["user_id"]
                leader_bet_uids = {b["user_id"] for b in open_bets}
                if leader_uid not in leader_bet_uids:
                    name = get_name_str(leader_uid)
                    fun_line = random.choice([
                        f"Playing it safe {name}? 😏",
                        f"{name} sitting out. Smart or scared? 🤔",
                        f"The leader {name} on the sidelines! 👑",
                    ])
                else:
                    # Last place
                    last = standings[-1]
                    name = get_name_str(last["user_id"])
                    fun_line = random.choice([
                        f"Good luck {name}, you need it! 🍀",
                        f"{name} could really use a win here! 😬",
                        f"Come on {name}, time to climb! 💪",
                    ])

        if fun_line:
            lines.append(f"\n{fun_line}")

        if is_silent_hours() and not await is_last_match_of_day(match_id):
            logger.info(f"Kickoff message suppressed for {match_id} — silent hours, not last match")
            return

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


async def job_poll_result(match_id: str, attempt: int = 1):
    MAX_POLL_ATTEMPTS = 36  # 36 x 5min = 3 hours max
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
        result_msg = format_result_message(match, settlements)

        # Append parlay wins to result message
        if parlay_wins:
            result_msg += "\n"
            for pid, p in parlay_wins:
                name = _get_user_name(p["user_id"])
                legs_str = "\n".join(f"• {label} ✅" for label in p["leg_labels"])
                result_msg += f"\n🎰 {name} hit a {p['legs']}-leg parlay!\n{legs_str}\n{p['stake']}c → {p['payout']}c 🔥"

        if is_silent_hours() and not await is_last_match_of_day(match_id):
            logger.info(f"Match {match_id} result held — silent hours, not last match")
            # Store full result message for morning catchup
            if "pending_result_messages" not in sheet.cache:
                sheet.cache["pending_result_messages"] = []
            sheet.cache["pending_result_messages"].append(result_msg)
            # Store parlay wins too
            if parlay_wins:
                if "pending_parlay_wins" not in sheet.cache:
                    sheet.cache["pending_parlay_wins"] = []
                sheet.cache["pending_parlay_wins"].extend(parlay_wins)
        else:
            await send_group(result_msg)

        # Check if all matches today are done
        await check_all_matches_done()

    except Exception as e:
        logger.error(f"Poll result job failed for {match_id}: {e}")
        await dm_admin(f"⚠️ Poll result job failed for match {match_id}: {e}")


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
async def check_all_matches_done():
    try:
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
                    today_matches.append(m)
            except Exception:
                continue

        if not today_matches:
            return

        all_done = all(
            m["status"] in ("FINISHED", "CANCELLED", "POSTPONED")
            for m in today_matches
        )

        if not all_done:
            return

        # Guard against double-fire — check if EOD already ran today
        eod_date = sheet.cache.get("eod_date")
        if eod_date == today_ct:
            logger.info("EOD already fired today, skipping duplicate")
            return

        sheet.cache["eod_date"] = today_ct
        logger.info("All matches done — firing standings")
        match_ids = [str(m["match_id"]) for m in today_matches]
        await job_post_standings(match_ids)

    except Exception as e:
        logger.error(f"check_all_matches_done failed: {e}")
        await dm_admin(f"⚠️ Failed to check if all matches done: {e}")


# ── Post standings + daily credits ───────────────────────────────────────────
async def job_post_standings(match_ids: list):
    try:
        import random
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

        # Add daily credits
        await sheet.add_daily_credits(DAILY_CREDITS, notify_fn=dm_admin)

        # Get standings AFTER credits added
        standings_after = sheet.get_standings()

        # Build EOD message
        lines = ["📅 End of Day\n"]

        # Match results
        for m in sorted(today_matches, key=lambda x: x["kickoff_utc"]):
            if m["status"] == "FINISHED":
                home_d = format_team(m["home"])
                away_d = format_team(m["away"])
                ou_label = "Over 2.5" if m["ou_result"] == "over" else "Under 2.5"
                lines.append(f"{home_d} {m['home_score']}–{m['away_score']} {away_d} · {ou_label}")

        lines.append("\n🏆 Standings")

        for i, user in enumerate(standings_after, 1):
            name = (user.get("first_name") or user.get("username") or "Unknown")[:10]
            credits = user["credits"]
            pl = pl_map.get(user["user_id"], 0)
            pl_str = f"+{pl}c" if pl > 0 else f"{pl}c"
            badge = " 🏆" if i == 1 else ""
            lines.append(f"{i}. {name}{badge} — {credits}c ({pl_str} today)")

        # Dynamic commentary
        def get_name(uid):
            user = sheet.cache["users"].get(uid, {})
            return (user.get("first_name") or user.get("username") or "Someone")

        # Biggest winner today
        if pl_map:
            best_uid = max(pl_map, key=lambda u: pl_map[u])
            best_pl = pl_map[best_uid]
            if best_pl > 0:
                lines.append(f"\n🎉 {get_name(best_uid)} had the biggest win today with +{best_pl}c!")

        # Overtakes — compare before and after rankings
        before_ranks = {u["user_id"]: i+1 for i, u in enumerate(standings_before)}
        after_ranks = {u["user_id"]: i+1 for i, u in enumerate(standings_after)}
        overtakes = []
        for uid, new_rank in after_ranks.items():
            old_rank = before_ranks.get(uid, new_rank)
            if new_rank < old_rank:
                # This person moved up — find who they passed
                passed = [u for u, r in after_ranks.items() if before_ranks.get(u, r) < old_rank and r >= new_rank and u != uid]
                for passed_uid in passed:
                    overtakes.append((get_name(uid), get_name(passed_uid)))
        if overtakes:
            parts = " and ".join([f"{loser} 🤡" for _, loser in overtakes])
            winner_name = overtakes[0][0]
            lines.append(f"📈 {winner_name} overtook {parts} today.")

        # Gap warning — if 2nd is within 100c of 1st
        if len(standings_after) >= 2:
            first_credits = standings_after[0]["credits"]
            second_credits = standings_after[1]["credits"]
            gap = first_credits - second_credits
            if 0 < gap <= 100:
                first_name = get_name(standings_after[0]["user_id"])
                second_name = get_name(standings_after[1]["user_id"])
                lines.append(f"⚠️ {second_name} is {gap}c behind {first_name}. Watch out!")

        # Parlay shoutout
        if parlay_payouts:
            def get_name_p(uid):
                user = sheet.cache["users"].get(uid, {})
                return (user.get("first_name") or user.get("username") or "Someone")
            for pid, p in parlay_payouts.items():
                name = get_name_p(p["user_id"])
                lines.append(f"🎰 {name} hit a {p['legs']}-leg parlay! {p['stake']}c → {p['payout']}c 🔥")

        lines.append(f"\nDaily credits added, good luck tomorrow! 🍀")
        lines.append("Use /groups for today's group tables.")

        await send_group("\n".join(lines))
        logger.info("End of day standings and daily credits posted")

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
        replace_existing=True,
        misfire_grace_time=60
    )

    # Morning catchup — 7:30AM SGT daily
    scheduler.add_job(
        job_morning_catchup,
        trigger=CronTrigger(hour=MORNING_CATCHUP_HOUR, minute=MORNING_CATCHUP_MINUTE, timezone=SGT),
        id="morning_catchup",
        replace_existing=True,
        misfire_grace_time=60
    )

    # Cache refresh — staggered to avoid colliding with cron jobs at :00 and :30
    scheduler.add_job(
        job_refresh_cache,
        trigger=CronTrigger(minute="5,15,25,35,45,55"),
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

        if status in ("FINISHED", "CANCELLED", "POSTPONED"):
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
                replace_existing=True
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
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            matches = api.fetch_today_matches()
            tomorrow_matches = api.fetch_matches_for_date(tomorrow)
            for m in matches + tomorrow_matches:
                await sheet.upsert_match(m, notify_fn=dm_admin)
            # No second refresh needed — upsert_match updates cache directly
        except RuntimeError as e:
            await dm_admin(f"⚠️ Startup: failed to fetch today's fixtures: {e}\nUse /admin_refresh to retry.")

        register_static_jobs()

        today_matches = await sheet.get_matches_for_date(today)
        tomorrow_matches_cached = await sheet.get_matches_for_date(tomorrow)
        all_today_matches = today_matches + tomorrow_matches_cached
        register_match_jobs(all_today_matches)

        scheduler.start()

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
