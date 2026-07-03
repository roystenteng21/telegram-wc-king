import json
import logging
import asyncio
import urllib.request
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

from config import (
    SGT, UTC, CT, MATCH_CREDITS,
    PREMATCH_SUMMARY_MINUTES, POLL_START_OFFSET, POLL_INTERVAL,
    KNOCKOUT_DURATION, ANTHROPIC_API_KEY,
    ADMIN_TELEGRAM_ID, BOT_VERSION, TEAM_DISPLAY, PARLAY_MULTIPLIERS,
    DAILY_CREDIT_TIERS, TOURNAMENT_FINAL_DATE, NAME_OVERRIDES,
    STATUS_ACTIVE_PLAY
)
import sheet
import api
import katerina as _katerina
from helpers import format_team, format_match_teams

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
async def send_group(message: str, parse_mode: str = None):
    try:
        await _bot.send_message(chat_id=_group_chat_id, text=message, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Failed to send group message: {e}")
        await dm_admin(f"⚠️ Failed to send group message: {e}")


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
    """Returns True if match_id shares the latest kickoff on its own CT date.
    Handles co-final matches (same kickoff time) — both return True.
    Uses the match's kickoff to determine the CT date, not datetime.now()."""
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
    latest_ko = max(m.get("kickoff_utc", "") for m in active)
    return match["kickoff_utc"] == latest_ko


# ── Format helpers ────────────────────────────────────────────────────────────
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
    name = user.get("first_name") or user.get("username") or "Someone"
    return NAME_OVERRIDES.get(name, name)


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
    Parlay legs: Name — outcome — 🎰 N/M (leg number from cache, no amount)
    Bust parlay legs: Name — outcome — 🥀"""
    name = _get_user_name(b["user_id"])
    outcome = _outcome_label(b["outcome"], match)
    if _is_parlay_leg(b):
        pid = b.get("parlay_id")
        if not sheet.is_parlay_alive(pid):
            return f"• {name} — {outcome} — 🥀"
        all_legs = sorted(sheet.get_parlay_bets(pid), key=lambda x: x.get("placed_at", ""))
        total = len(all_legs)
        leg_num = next((i + 1 for i, l in enumerate(all_legs) if l["bet_id"] == b["bet_id"]), "?")
        return f"• {name} — {outcome} — 🎰 {leg_num}/{total}"
    return f"• {name} — {outcome} — {b['amount']:,}c"


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

    def sort_key(s):
        return _get_user_name(s["user_id"]).lower()

    # Separate singles from parlay legs
    singles = [s for s in settlements if not _is_parlay_leg(s)]
    parlay_legs = [s for s in settlements if _is_parlay_leg(s)]

    for s in sorted(singles, key=sort_key):
        name = _get_user_name(s["user_id"])
        icon = "\u2705" if s["status"] == "won" else "\u274c"
        lines.append(f"{name} \u2014 {_outcome_label(s['outcome'], match)} \u2014 {s['amount']:,}c {icon}")

    # Parlay display — compact one-line format with icon sequence
    parlay_section = []

    # Won parlays (last leg just settled)
    won_pids = set()
    if parlay_wins:
        for pid, p in parlay_wins:
            won_pids.add(str(pid))
            name = _get_user_name(p["user_id"])
            icons = "\u2705" * p["legs"]
            parlay_section.append(f"\U0001f3b0 {name} {icons} \u2014 {p['stake']:,}c \u2192 {p['payout']:,}c \U0001f525")

    # In-play and bust parlays
    seen_pids = set()
    for s in sorted(parlay_legs, key=sort_key):
        pid = str(s.get("parlay_id", ""))
        if pid in seen_pids or pid in won_pids:
            continue
        seen_pids.add(pid)
        name = _get_user_name(s["user_id"])
        all_legs = sorted(sheet.get_parlay_bets(pid), key=lambda x: x.get("placed_at", ""))
        icons = "".join(
            "\u2705" if l["status"] == "won" else "\u274c" if l["status"] == "lost" else ""
            for l in all_legs
        )
        settled_count = sum(1 for l in all_legs if l["status"] in ("won", "lost"))
        total = len(all_legs)
        stake = all_legs[0]["amount"] if all_legs else s["amount"]
        is_bust = any(l["status"] == "lost" for l in all_legs)
        if is_bust:
            parlay_section.append(f"\U0001f940 {name} {icons} \u2014 parlay bust, {stake:,}c gone")
        else:
            parlay_section.append(f"\U0001f3b0 {name} {icons} \u2014 {settled_count}/{total} in play \u23f3")

    if parlay_section:
        lines.append("")
        lines.extend(parlay_section)

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
            texts = [b["text"].strip() for b in data.get("content", []) if b.get("type") == "text" and b.get("text", "").strip()]
            result = " ".join(texts) if texts else ""
            return result if result else fallback
    except Exception as e:
        logger.error(f"Katerina API call failed in scheduler: {e}")
        return fallback


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

        # Katerina good luck / roast line
        if open_bets:
            standings = sheet.get_standings()
            bet_summary = ", ".join(
                f"{_get_user_name(b['user_id'])} on {_outcome_label(b['outcome'], match)} ({b['amount']}c)"
                for b in sorted_bets
            )
            standings_str = ", ".join(
                f"{_get_user_name(u['user_id'])} {u['credits']:,}c"
                for u in standings[:3]
            ) if standings else ""
            no_bet_names = [
                _get_user_name(u["user_id"])
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
    affected_parlay_ids.discard("0")

    for pid in affected_parlay_ids:
        result = await sheet.settle_parlay(pid, notify_fn=dm_admin)
        if result:
            payouts.append((pid, result))
    return payouts


async def _auto_settle_stuck_match(match_id: str):
    """
    Settle bets for a match that's FINISHED in cache but still has open bets.
    If some bets were already settled (result was already posted), settle remaining
    bets silently and DM admin — do NOT repost result to group.
    """
    try:
        match = await sheet.get_match_by_id(match_id)
        if not match or not match.get("result"):
            return

        all_match_bets = [b for b in sheet.cache["bets"] if b["match_id"] == str(match_id)]
        open_bets = [b for b in all_match_bets if b["status"] == "open"]
        already_settled = any(b["status"] in ("won", "lost") for b in all_match_bets)

        if not open_bets:
            await check_all_matches_done(match_id)
            return

        # Settle remaining open bets
        settlements = await sheet.settle_bets_for_match(match_id, match["result"], match.get("ou_result", ""), notify_fn=dm_admin)
        parlay_wins = await check_parlay_completions(match_id)

        if already_settled:
            # Result was already posted to group — settle silently, DM admin only
            await dm_admin(f"ℹ️ Match {match_id} had {len(open_bets)} lingering open bet(s) — settled silently. Result was already posted to group.")
            await check_all_matches_done(match_id)
            return

        # Result was never posted — full auto-settle with group message
        is_last = await is_last_match_of_day(match_id)
        topup_line = ""
        if not is_last:
            try:
                await sheet.add_match_credits(MATCH_CREDITS, match_id, notify_fn=dm_admin)
                topup_line = f"\n\n+{MATCH_CREDITS}c added to everyone's account. 🪙"
            except Exception as e:
                logger.error(f"Auto-settle top-up failed for {match_id}: {e}")

        base_result_msg = format_result_message(match, settlements, parlay_wins=parlay_wins)
        result_msg = base_result_msg

        if settlements or parlay_wins:
            home_score = match.get("home_score", "?")
            away_score = match.get("away_score", "?")
            settled_summary = ", ".join(
                f"{_get_user_name(s['user_id'])} {'won' if s['status'] == 'won' else 'lost'} {s['amount']:,}c on {_outcome_label(s['outcome'], match)}"
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
                prompt = (f"{context} Every single person lost — {names}. Go full savage. 1-2 sentences, no mercy. No markdown.")
            else:
                prompt = (f"{context} Write 1-2 sharp sentences reacting to the result and bets with light banter. Reference specific names and outcomes. No markdown.")
            commentary = await _katerina_line(prompt, "", max_tokens=150)
            if commentary:
                result_msg = result_msg + f"\n\n{commentary}"

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

        # Idempotency guard: if this match's bets were already settled by another
        # path (auto-settle via cache refresh), do not settle or post again.
        all_match_bets = [b for b in sheet.cache["bets"] if b["match_id"] == str(match_id)]
        already_settled = any(b["status"] in ("won", "lost") for b in all_match_bets)
        has_open = any(b["status"] == "open" for b in all_match_bets)
        if already_settled and not has_open:
            logger.info(f"Match {match_id} already settled by another path — poll skipping duplicate post")
            await check_all_matches_done(match_id)
            return

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
            if m["status"] in STATUS_ACTIVE_PLAY:
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


async def _send_coming_up_next_day():
    """Send tomorrow's matches after EOD. Shows kickoff times and any bets already placed."""
    try:
        tomorrow_ct = (datetime.now(CT) + timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_matches = await get_ct_date_matches(tomorrow_ct)
        upcoming = [m for m in tomorrow_matches if m.get("status") not in ("FINISHED", "CANCELLED", "POSTPONED")]
        if not upcoming:
            return

        lines = ["📅 Coming up tomorrow:"]
        for m in sorted(upcoming, key=lambda x: x["kickoff_utc"]):
            home_d = format_team(m["home"])
            away_d = format_team(m["away"])
            kickoff_utc = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            time_str = kickoff_utc.astimezone(SGT).strftime("%-I:%M %p SGT")
            lines.append(f"\n{home_d} vs {away_d} — {time_str}")
            open_bets = [b for b in sheet.cache["bets"] if b["match_id"] == str(m["match_id"]) and b["status"] == "open"]
            for b in sorted(open_bets, key=_get_sort_name):
                lines.append(f"  {_format_bet_line(b, m)}")

        await send_group("\n".join(lines))
        logger.info("Coming up tomorrow sent after EOD")
    except Exception as e:
        logger.error(f"_send_coming_up_next_day failed: {e}")
        await dm_admin(f"⚠️ Coming up tomorrow message failed: {e}")


def trigger_poll(match_id: str):
    """Manually trigger an immediate poll for a match (admin use)."""
    _schedule_poll(match_id, delay_seconds=0, attempt=1)
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

        # EOD fires only when ALL active CT-day matches are finished
        active = [m for m in today_matches if m.get("status") not in ("CANCELLED", "POSTPONED")]
        if not active:
            return
        if not all(m["status"] in ("FINISHED", "CANCELLED", "POSTPONED") for m in active):
            return

        sheet.cache["eod_date"] = target_ct_date
        logger.info(f"All matches done for CT {target_ct_date} — waiting 10s before firing standings")
        await asyncio.sleep(10)  # Allow in-flight bet writes to complete before cache refresh
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

        # Derive CT date from match kickoffs — more reliable than datetime.now(CT)
        ct_date = None
        for m in today_matches:
            try:
                ko = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                ct_date = ko.astimezone(CT).strftime("%Y-%m-%d")
                break
            except Exception:
                continue
        if not ct_date:
            ct_date = datetime.now(CT).strftime("%Y-%m-%d")

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

        parlay_already_paid = {}  # won parlays settled mid-day — display only, don't pay again

        for pid in today_parlay_ids:
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

            all_won = all(b["status"] == "won" for b in settled_legs)
            effective_legs = len(settled_legs)

            if all_won and effective_legs >= 2:
                multiplier = PARLAY_MULTIPLIERS.get(effective_legs)
                if not multiplier:
                    continue
                payout = int(stake * multiplier)
                net = payout - stake
                parlay_info = {
                    "user_id": uid,
                    "legs": effective_legs,
                    "stake": stake,
                    "multiplier": multiplier,
                    "payout": payout,
                    "net": net
                }
                if pid in sheet.cache.get("paid_parlays", set()):
                    parlay_already_paid[pid] = parlay_info  # already paid — display only
                else:
                    parlay_payouts[pid] = parlay_info  # pay + display

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
        for pid in today_parlay_ids:
            if pid in parlay_payouts or pid in parlay_already_paid:
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
        await sheet.add_tiered_daily_credits(tier_map, notify_fn=dm_admin, ct_date=ct_date)

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

        # Build EOD message — Option C: standings + parlays only, no per-match recap
        sgt_date = datetime.now(SGT).strftime("%d %b")
        lines = [f"📅 End of Day — {sgt_date}\n"]

        lines.append("🏆 Standings")
        for i, user in enumerate(standings_after, 1):
            name = _get_user_name(user["user_id"])
            credits = user["credits"]
            pl = pl_map.get(user["user_id"], 0)
            daily = tier_map.get(user["user_id"], 0)
            pl_str = f"+{pl:,}c" if pl > 0 else f"{pl:,}c"
            credits_str = f"{credits:,}"
            daily_str = f", +{daily:,}c" if daily else ""
            badge = " 🏆" if i == 1 else ""
            lines.append(f"{i}. {name}{badge} — {credits_str}c ({pl_str}{daily_str})")

        # Parlay section — individual wins, consolidated busts
        all_parlay_wins = {**parlay_payouts, **parlay_already_paid}
        bust_by_user = {}
        for pid, p in parlay_losses.items():
            uid = p["user_id"]
            if uid not in bust_by_user:
                bust_by_user[uid] = {"count": 0, "total_stake": 0}
            bust_by_user[uid]["count"] += 1
            bust_by_user[uid]["total_stake"] += p["stake"]

        if all_parlay_wins or bust_by_user:
            lines.append("")
            for pid, p in sorted(all_parlay_wins.items(), key=lambda x: -x[1]["payout"]):
                name = _get_user_name(p["user_id"])
                lines.append(f"🎰 {name} — {p['legs']}-leg parlay, {p['stake']:,}c → {p['payout']:,}c 🔥")
            for uid, info in sorted(bust_by_user.items(), key=lambda x: _get_user_name(x[0])):
                name = _get_user_name(uid)
                if info["count"] == 1:
                    lines.append(f"🥀 {name} — parlay bust, {info['total_stake']:,}c gone")
                else:
                    lines.append(f"🥀 {name} — {info['count']} busts, {info['total_stake']:,}c gone")

        # Katerina — 1 punchy sentence, best stat angle
        # Build rich context for interesting stat hunting
        singles_stats = {}
        for b in sheet.cache["bets"]:
            if str(b["match_id"]) not in match_ids:
                continue
            if str(b.get("parlay_id", "")) not in ("", "0"):
                continue
            uid = b["user_id"]
            if uid not in singles_stats:
                singles_stats[uid] = {"won": 0, "lost": 0, "biggest": 0}
            if b["status"] == "won":
                singles_stats[uid]["won"] += 1
                singles_stats[uid]["biggest"] = max(singles_stats[uid]["biggest"], b["amount"])
            elif b["status"] == "lost":
                singles_stats[uid]["lost"] += 1
                singles_stats[uid]["biggest"] = max(singles_stats[uid]["biggest"], b["amount"])

        all_bettors = set(singles_stats) | {p["user_id"] for p in list(parlay_losses.values()) + list(all_parlay_wins.values())}
        skipped_uids = set(sheet.cache["users"]) - all_bettors
        skipped_str = ", ".join(_get_user_name(uid) for uid in skipped_uids) if skipped_uids else ""

        singles_lines = []
        for uid, s in singles_stats.items():
            total = s["won"] + s["lost"]
            singles_lines.append(f"{_get_user_name(uid)}: {s['won']}W-{s['lost']}L on singles, biggest bet {s['biggest']:,}c")

        days_to_final = (TOURNAMENT_FINAL_DATE - datetime.now(UTC).date()).days
        standings_str = "\n".join(
            f"{i}. {_get_user_name(u['user_id'])} — {u['credits']:,}c ({'+' if pl_map.get(u['user_id'],0)>0 else ''}{pl_map.get(u['user_id'],0):,}c today)"
            for i, u in enumerate(standings_after, 1)
        )
        parlay_win_str = ", ".join(
            f"{_get_user_name(p['user_id'])} hit {p['legs']}-leg {p['stake']:,}c→{p['payout']:,}c"
            for p in all_parlay_wins.values()
        ) if all_parlay_wins else ""
        bust_str = ", ".join(
            f"{_get_user_name(uid)} lost {info['count']} parlay(s) worth {info['total_stake']:,}c"
            for uid, info in bust_by_user.items()
        ) if bust_by_user else ""

        gap_str = ""
        if len(standings_after) >= 2:
            gap = standings_after[0]["credits"] - standings_after[1]["credits"]
            gap_str = f"{_get_user_name(standings_after[0]['user_id'])} leads {_get_user_name(standings_after[1]['user_id'])} by {gap:,}c"

        top_winner_str, top_loser_str = "", ""
        if pl_map:
            top_winner_uid = max(pl_map, key=lambda uid: pl_map[uid])
            top_loser_uid = min(pl_map, key=lambda uid: pl_map[uid])
            if pl_map[top_winner_uid] > 0:
                top_winner_str = f"{_get_user_name(top_winner_uid)} +{pl_map[top_winner_uid]:,}c"
            if pl_map[top_loser_uid] < 0:
                top_loser_str = f"{_get_user_name(top_loser_uid)} {pl_map[top_loser_uid]:,}c"

        eod_prompt = (
            f"End of day for WC Kings 2026 ({days_to_final} days to Final). "
            f"Write EXACTLY 3 punchy sentences as Katerina the house bookie. "
            f"Vary the focus across the 3 sentences — pick 3 different angles from: biggest swing, tightest race, "
            f"most reckless bet, worst collapse, who skipped, most dominant singles day, parlay hero or villain. "
            f"Be specific, name names, no fluff. No markdown, no hashtags.\n"
            f"Standings:\n{standings_str}\n"
            + (f"Biggest daily gain: {top_winner_str}\n" if top_winner_str else "")
            + (f"Biggest daily loss: {top_loser_str}\n" if top_loser_str else "")
            + (f"Leader gap: {gap_str}\n" if gap_str else "")
            + (f"Singles records today:\n" + "\n".join(singles_lines) + "\n" if singles_lines else "")
            + (f"Parlay wins: {parlay_win_str}\n" if parlay_win_str else "")
            + (f"Parlay busts: {bust_str}\n" if bust_str else "")
            + (f"Skipped betting entirely: {skipped_str}\n" if skipped_str else "")
        )

        if pl_map:
            commentary = await _katerina_line(eod_prompt, "", max_tokens=200)
        else:
            commentary = await _katerina_line(
                "Nobody placed any bets today. Write one short mocking line. 1 sentence max.",
                "Nobody put money down today. Bold strategy. 🤔",
                max_tokens=60
            )

        if commentary:
            lines.append("")
            lines.append(commentary)

        tier_values = sorted(set(tier_map.values()))
        if len(tier_values) > 1:
            lines.append(f"\n+{tier_values[0]}c–{tier_values[-1]}c daily credits added by rank, good luck tomorrow! 🍀")
        else:
            lines.append(f"\n+{tier_values[0]}c daily credits added, good luck tomorrow! 🍀")

        await send_group("\n".join(lines))
        logger.info("End of day standings and daily credits posted")

        # Coming up tomorrow
        await _send_coming_up_next_day()

        # Stage transition check — fire Katerina hype in background if today ends a stage
        asyncio.create_task(_katerina.check_and_send_stage_hype(notify_fn=dm_admin))

    except Exception as e:
        logger.error(f"Post standings job failed: {e}")
        await dm_admin(f"⚠️ Post standings job failed: {e}")


# ── Finished-match settlement guard ──────────────────────────────────────────
async def _check_finished_matches_need_settlement() -> list:
    """
    Check all cached matches. For any that are FINISHED with a result and still
    have open bets, schedule _auto_settle_stuck_match if not already scheduled.
    Returns list of match_ids that had auto_settle scheduled (for health monitor DM).
    """
    scheduled = []
    job_ids = {job.id for job in scheduler.get_jobs()}
    for m in sheet.cache.get("matches", {}).values():
        if m.get("status") != "FINISHED":
            continue
        if not m.get("result"):
            continue
        match_id = str(m["match_id"])
        open_bets = [b for b in sheet.cache["bets"] if b["match_id"] == match_id and b["status"] == "open"]
        if not open_bets:
            continue
        job_id = f"auto_settle_{match_id}"
        if job_id in job_ids:
            continue
        scheduler.add_job(
            _auto_settle_stuck_match,
            trigger=DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=5)),
            args=[match_id],
            id=job_id,
            replace_existing=True
        )
        scheduled.append(match_id)
        logger.info(f"Match {match_id} FINISHED with open bets — scheduled auto-settle")
    return scheduled


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

    # After upserts: catch any FINISHED matches with open bets that have no settlement job
    await _check_finished_matches_need_settlement()


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
        if "cache_refresh" not in job_ids:
            issues.append("cache_refresh job missing from scheduler")

        # 3. Any match in active play with no poll job registered
        for m in sheet.cache.get("matches", {}).values():
            if m.get("status") in STATUS_ACTIVE_PLAY:
                mid = str(m["match_id"])
                has_poll = any(mid in job.id and "poll" in job.id for job in scheduler.get_jobs())
                if not has_poll:
                    _schedule_poll(mid, delay_seconds=10, attempt=1)
                    issues.append(f"Match {mid} is {m['status']} — no poll job found, scheduled emergency poll")

        # 4. Any FINISHED match with open bets and no settlement job
        settled = await _check_finished_matches_need_settlement()
        for mid in settled:
            issues.append(f"Match {mid} FINISHED with open bets — no settlement job found, scheduled auto-settle")

        # 5. Bot can reach group chat — lightweight check via _group_chat_id
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

    # Cache refresh — staggered to avoid colliding with cron jobs at :00 and :30
    scheduler.add_job(
        job_refresh_cache,
        trigger=CronTrigger(minute="5,15,25,35,45,55"),
        id="cache_refresh",
        replace_existing=True,
        misfire_grace_time=120
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

        if status in STATUS_ACTIVE_PLAY:
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

        # Pre-match summary — 15 min before kickoff
        summary_time = kickoff_utc - timedelta(minutes=PREMATCH_SUMMARY_MINUTES)
        if summary_time > now_utc:
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
        # Brief pause before sheet access — prevents 429 on rapid restarts
        await asyncio.sleep(8)
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
                await asyncio.sleep(1.5)  # Rate limit: space out sheet writes
        except RuntimeError as e:
            await dm_admin(f"⚠️ Startup: failed to fetch today's fixtures: {e}\nUse /admin_refresh to retry.")

        register_static_jobs()

        yesterday_cached = await sheet.get_matches_for_date(yesterday)
        today_matches = await sheet.get_matches_for_date(today)
        tomorrow_matches_cached = await sheet.get_matches_for_date(tomorrow)
        all_today_matches = yesterday_cached + today_matches + tomorrow_matches_cached
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
