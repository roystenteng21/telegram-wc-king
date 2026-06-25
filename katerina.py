import asyncio
import json
import logging
import random
import re
import urllib.request
import pytz
from collections import Counter
from datetime import date, datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes

from config import (
    ADMIN_TELEGRAM_ID, SGT, UTC,
    ANTHROPIC_API_KEY, TOURNAMENT_STAGES, TOURNAMENT_FINAL_DATE,
    PRIZE_INFO, PRIZE_PLAYER_COUNT, NAME_OVERRIDES
)
import sheet
from helpers import (
    application, dm_admin, is_silent_hours, is_group_message,
    format_team, truncate, _chat_history, get_group_chat_id
)

logger = logging.getLogger(__name__)

# Module-level timezone constant — avoid recreating on every call
CT = pytz.timezone("America/Chicago")

# Ignore mentions sent before this time — prevents backlog replay after restart
_startup_time = datetime.now(UTC)

def _get_current_stage() -> str:
    """Return current tournament stage name based on today's date."""
    today = date.today()
    for stage in TOURNAMENT_STAGES:
        if stage["start"] <= today <= stage["end"]:
            return stage["name"]
    if today < TOURNAMENT_STAGES[0]["start"]:
        return "Pre-Tournament"
    if today > TOURNAMENT_FINAL_DATE:
        return "Tournament Over"
    for i in range(len(TOURNAMENT_STAGES) - 1):
        if TOURNAMENT_STAGES[i]["end"] < today < TOURNAMENT_STAGES[i+1]["start"]:
            return f"Between {TOURNAMENT_STAGES[i]['name']} and {TOURNAMENT_STAGES[i+1]['name']}"
    return "Unknown"


def _get_in_bet_for_katerina(user_id: int) -> int:
    """Credits currently tied up in active bets (singles + one stake per alive parlay)."""
    open_bets = [b for b in sheet.cache.get("bets", []) if b["user_id"] == user_id and b["status"] == "open"]

    def _has_pid(b):
        pid = b.get("parlay_id", "")
        return bool(pid) and str(pid) not in ("", "0")

    singles_stake = sum(b["amount"] for b in open_bets if not _has_pid(b))
    seen_parlays = set()
    parlay_stake = 0
    for b in open_bets:
        pid = b.get("parlay_id", "")
        if _has_pid(b) and pid not in seen_parlays and sheet.is_parlay_alive(pid):
            seen_parlays.add(pid)
            parlay_stake += b["amount"]
    return singles_stake + parlay_stake


def _build_katerina_context() -> str:
    """Build a snapshot of current bot state for Katerina's system prompt."""
    now_sgt = datetime.now(SGT)
    refresh = sheet.cache.get("last_refresh")
    refresh_str = refresh.astimezone(SGT).strftime("%I:%M %p SGT") if refresh else "unknown"

    current_stage = _get_current_stage()
    days_to_final = (TOURNAMENT_FINAL_DATE - date.today()).days
    current_player_count = len(sheet.cache.get("users", {}))

    standings = sheet.get_standings()
    standings_lines = []
    for i, u in enumerate(standings, 1):
        raw_name = truncate(u.get("first_name") or u.get("username") or "Unknown")
        name = NAME_OVERRIDES.get(raw_name, raw_name)
        in_bet = _get_in_bet_for_katerina(u["user_id"])
        effective = u["credits"] + in_bet
        credit_str = f"{u['credits']:,}c" + (f" (+{in_bet:,}c in bets = {effective:,}c total)" if in_bet > 0 else "")
        standings_lines.append(f"{i}. {name} — {credit_str}")

    today_ct = datetime.now(CT).strftime("%Y-%m-%d")

    match_lines = []
    try:
        for m in sorted(sheet.cache["matches"].values(), key=lambda x: x["kickoff_utc"]):
            try:
                ko = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                if ko.astimezone(CT).strftime("%Y-%m-%d") != today_ct:
                    continue
                ko_sgt = ko.astimezone(SGT).strftime("%I:%M %p SGT")
                status = m["status"]
                home = format_team(m["home"])
                away = format_team(m["away"])
                bets_on = [b for b in sheet.cache["bets"] if b["match_id"] == str(m["match_id"]) and b["status"] == "open"]
                if status == "FINISHED":
                    match_lines.append(f"{home} {m['home_score']}–{m['away_score']} {away} (FT) — {len(bets_on)} bets")
                else:
                    match_lines.append(f"{home} vs {away} — {ko_sgt} — {status} — {len(bets_on)} bets")
            except Exception:
                continue
    except Exception:
        pass

    silent = is_silent_hours()

    # Per-player bet summary
    player_bet_lines = []
    for uid, u in sheet.cache.get("users", {}).items():
        raw_name = truncate(u.get("first_name") or u.get("username") or "Unknown")
        name = NAME_OVERRIDES.get(raw_name, raw_name)
        user_bets = [b for b in sheet.cache["bets"] if b["user_id"] == uid]

        def _has_pid(b):
            pid = b.get("parlay_id", "")
            return bool(pid) and str(pid) not in ("", "0")
        open_singles = [b for b in user_bets if b["status"] == "open" and not _has_pid(b)]
        open_parlay_ids = list({b["parlay_id"] for b in user_bets if b["status"] == "open" and _has_pid(b)})
        all_parlay_ids = list({b["parlay_id"] for b in user_bets if _has_pid(b)})
        dead_parlay_ids = [
            pid for pid in all_parlay_ids
            if any(b["status"] == "lost" for b in user_bets if b.get("parlay_id") == pid)
        ]

        today_ct_local = datetime.now(CT).strftime("%Y-%m-%d")
        today_settled = []
        for b in user_bets:
            if b["status"] not in ("won", "lost"):
                continue
            m = sheet.cache["matches"].get(str(b["match_id"]), {})
            try:
                ko = datetime.strptime(m.get("kickoff_utc", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                if ko.astimezone(CT).strftime("%Y-%m-%d") == today_ct_local:
                    today_settled.append(b)
            except Exception:
                continue

        parts = []
        if open_singles:
            parts.append(f"{len(open_singles)} open single(s)")
        if open_parlay_ids:
            parts.append(f"{len(open_parlay_ids)} active parlay(s)")
        if dead_parlay_ids:
            parts.append(f"{len(dead_parlay_ids)} 🥀 dead parlay(s)")
        if today_settled:
            wins = sum(1 for b in today_settled if b["status"] == "won")
            losses = sum(1 for b in today_settled if b["status"] == "lost")
            parts.append(f"today: {wins}W {losses}L")
        if parts:
            player_bet_lines.append(f"{name}: {', '.join(parts)}")

    return f"""Data as of {refresh_str}.

CURRENT TIME: {now_sgt.strftime("%I:%M %p SGT")}
SILENT HOURS: {"YES (12AM–7:30AM SGT)" if silent else "NO"}

TOURNAMENT STAGE: {current_stage}
DAYS TO FINAL (July 19): {days_to_final}
PLAYER COUNT: {current_player_count} (expected: {PRIZE_PLAYER_COUNT})

PRIZE POOL:
{PRIZE_INFO}

LEADERBOARD:
{chr(10).join(standings_lines) if standings_lines else "No players yet"}

TODAY'S MATCHES:
{chr(10).join(match_lines) if match_lines else "No matches today"}

PLAYER BET STATUS:
{chr(10).join(player_bet_lines) if player_bet_lines else "No bet activity"}
"""




async def _call_katerina(user_message: str, bot_context: str, sender_name: str = None, sender_stats: str = None) -> str:
    """Call Claude API to get Katerina's reply."""

    sender_block = ""
    if sender_name:
        sender_block = f"\nTHE PERSON TALKING TO YOU RIGHT NOW: {sender_name}"
        if sender_stats:
            sender_block += f"\n{sender_stats}"

    system = """You are Katerina, the house bookie and dealer for a World Cup betting bot called Degen.

Your personality:
- Sharp, witty, confident. Light banter is your default mode — warm when the moment calls for it, playful otherwise.
- You read the room. For greetings, casual mentions, or encouragement requests — be warm or lightly witty, not cutting.
- Savage mode is reserved ONLY for explicit roast requests or when the context clearly calls for it (e.g. everyone just lost their bets).
- For neutral interactions, randomly vary between genuinely warm and lightly cheeky — keep people guessing.
- You do NOT assume the house always wins — you respect good bettors.
- Occasionally smug, occasionally flirty, always sharp.
- Light dramatic flair when upsets happen.
- Pure English. No swearing. No "darling" or "love".
- "Sucker" is reserved ONLY for people who lost bets or have a bad record — do NOT use it as a generic filler or casual address.
- Short replies — 1 to 3 sentences max unless the question genuinely needs more.
- When referencing data, always say "as of [time]" from the data snapshot.
- If the data doesn't confirm something, say you don't have that right now.
- You NEVER place bets, change credits, or run commands. Direct to /bet only — NEVER mention /predict.
- You are NOT a customer service bot. You have a personality. Use it.
- When someone says "me" or "my", they are referring to the person identified in THE PERSON TALKING TO YOU RIGHT NOW. Address them by name.
- NEVER use markdown formatting. No **bold**, no _italic_, no backticks. Plain text only.
- NEVER mention match kickoff times in your replies.
- When referencing a failed or dead parlay, always use the 🥀 emoji.
- When asked about predictions and search results are unavailable, say you couldn't find current expert opinions and leave it there. Do NOT speculate, invent analysis, or make up facts.

Current bot state:
""" + bot_context + sender_block

    try:
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 300,
            "system": system,
            "messages": [{"role": "user", "content": user_message}]
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
            return text_blocks[0]["text"].strip() if text_blocks else None
    except Exception as e:
        logger.error(f"Katerina API call failed: {e}")
        return None


async def _get_roast_data(target_uid: int) -> dict:
    """Build roast data for a specific user."""
    user = sheet.cache["users"].get(target_uid, {})
    raw_name = truncate(user.get("first_name") or user.get("username") or "that sucker")
    name = NAME_OVERRIDES.get(raw_name, raw_name)
    credits = user.get("credits", 0)
    in_bet = _get_in_bet_for_katerina(target_uid)
    effective_credits = credits + in_bet
    standings = sheet.get_standings()
    rank = next((i+1 for i, u in enumerate(standings) if u["user_id"] == target_uid), None)
    total_players = len(standings)

    all_bets = [b for b in sheet.cache["bets"] if b["user_id"] == target_uid and b["status"] in ("won", "lost")]
    won = [b for b in all_bets if b["status"] == "won"]
    lost = [b for b in all_bets if b["status"] == "lost"]
    total_bets = len(all_bets)
    win_rate = round(len(won) / total_bets * 100) if total_bets else 0

    # Today's P&L
    today_ct = datetime.now(CT).strftime("%Y-%m-%d")
    today_bets = []
    for b in all_bets:
        match = sheet.cache["matches"].get(str(b["match_id"]), {})
        ko_str = match.get("kickoff_utc", "")
        try:
            ko = datetime.strptime(ko_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if ko.astimezone(CT).strftime("%Y-%m-%d") == today_ct:
                today_bets.append(b)
        except Exception:
            continue

    today_pl = sum(b["amount"] if b["status"] == "won" else -b["amount"] for b in today_bets)

    # Biggest loss
    biggest_loss_bet = max(lost, key=lambda b: b["amount"], default=None)

    # Open bets today
    open_bets = [b for b in sheet.cache["bets"] if b["user_id"] == target_uid and b["status"] == "open"]

    # Most common outcome bet
    outcome_counts = Counter(b["outcome"] for b in all_bets)
    top_outcome = outcome_counts.most_common(1)[0] if outcome_counts else None

    # Avg bet size as % of effective credits (bet cowardice indicator)
    avg_bet = round(sum(b["amount"] for b in all_bets) / total_bets) if total_bets else 0
    avg_bet_pct = round(avg_bet / effective_credits * 100, 1) if effective_credits > 0 else 0

    # OU market participation
    ou_bets = len([b for b in all_bets if b["market"] == "ou"])
    ou_pct = round(ou_bets / total_bets * 100) if total_bets else 0

    # Parlay record
    all_user_bets = sheet.cache["bets"]
    parlay_ids = set(
        b["parlay_id"] for b in all_user_bets
        if b["user_id"] == target_uid
        and b.get("parlay_id") and str(b.get("parlay_id")) not in ("", "0")
    )
    parlays_attempted = len(parlay_ids)
    parlays_won = len([pid for pid in parlay_ids if pid in sheet.cache.get("paid_parlays", set())])

    # Current streak (positive = win streak, negative = loss streak)
    sorted_settled = sorted(all_bets, key=lambda b: b.get("placed_at", ""), reverse=True)
    streak = 0
    if sorted_settled:
        streak_status = sorted_settled[0]["status"]
        for b in sorted_settled:
            if b["status"] == streak_status:
                streak += 1 if streak_status == "won" else -1
            else:
                break

    # Matches skipped (finished matches with no bet from this user)
    finished_match_ids = {m["match_id"] for m in sheet.cache["matches"].values() if m.get("status") == "FINISHED"}
    bet_match_ids = {b["match_id"] for b in all_user_bets if b["user_id"] == target_uid}
    skipped_matches = len(finished_match_ids - bet_match_ids)

    # All-time net P&L from betting
    all_time_pl = sum(b["amount"] if b["status"] == "won" else -b["amount"] for b in all_bets)

    return {
        "name": name,
        "credits": credits,
        "in_bet": in_bet,
        "effective_credits": effective_credits,
        "rank": rank,
        "total_players": total_players,
        "total_bets": total_bets,
        "wins": len(won),
        "losses": len(lost),
        "win_rate": win_rate,
        "today_pl": today_pl,
        "today_bets": len(today_bets),
        "biggest_loss": biggest_loss_bet,
        "open_bets_count": len(open_bets),
        "top_outcome": top_outcome,
        "is_leader": rank == 1,
        "is_last": rank == total_players if total_players else False,
        "avg_bet": avg_bet,
        "avg_bet_pct": avg_bet_pct,
        "ou_pct": ou_pct,
        "parlays_attempted": parlays_attempted,
        "parlays_won": parlays_won,
        "streak": streak,
        "skipped_matches": skipped_matches,
        "all_time_pl": all_time_pl,
    }


async def _generate_roast(name: str, data: dict, roast_angle: str = None) -> str:
    """Call Claude API to generate a Katerina roast."""
    days_to_final = (TOURNAMENT_FINAL_DATE - date.today()).days
    prize_context = f"There are {days_to_final} days left until the Final. Winner takes the World Cup champion jersey."

    system = """You are Katerina, the sharp, confident, slightly savage house bookie for a World Cup betting bot called Degen.

Generate a roast of a player based on their stats. Rules:
- 1–2 sentences max
- Sharp but not mean-spirited. Tease, don't destroy.
- Use their actual stats naturally — don't just list numbers
- Reference the prize (World Cup champion jersey for 1st, runner-up jersey for 2nd) for extra sting — but vary it, don't always mention it
- Last place players get slightly harsher treatment around the prize
- The leader is also fair game — especially for playing it safe, making tiny bets, or coasting on their lead
- Anyone can catch smoke — don't always go for the obvious angle
- Occasionally smug or dramatic
- No swearing
- Pure English
- Vary your openings — don't start with the same word every time
- Never say "darling" or "love"
- "sucker" only for bad bettors/losers, not as generic address
"""

    streak_str = (
        f"{data['streak']}-win streak" if data['streak'] > 1
        else f"{abs(data['streak'])}-loss streak" if data['streak'] < -1
        else "no notable streak"
    )

    credit_detail = f"{data['credits']:,}c in wallet"
    if data['in_bet'] > 0:
        credit_detail += f" + {data['in_bet']:,}c in active bets = {data['effective_credits']:,}c total"

    prompt = f"""Roast this player named {data['name']}:
- Credits: {credit_detail} (rank {data['rank']} of {data['total_players']})
- Total bets: {data['total_bets']} ({data['wins']} wins, {data['losses']} losses, {data['win_rate']}% win rate)
- Average bet: {data['avg_bet']:,}c ({data['avg_bet_pct']}% of effective credits) — low % = playing it safe
- OU market usage: {data['ou_pct']}% of bets are over/under
- Parlay record: {data['parlays_attempted']} attempted, {data['parlays_won']} won
- Current streak: {streak_str}
- Matches skipped (no bet placed): {data['skipped_matches']}
- All-time net P&L from betting: {data['all_time_pl']:,}c
- Today's P&L: {data['today_pl']:,}c
- Biggest loss: {f"{data['biggest_loss']['amount']:,}c on {data['biggest_loss']['outcome']}" if data['biggest_loss'] else 'none recorded'}
- Most bet outcome: {f"{data['top_outcome'][0]} ({data['top_outcome'][1]} times)" if data['top_outcome'] else 'none'}
- Is leaderboard leader: {data['is_leader']}
- Is last place: {data['is_last']}
- Tournament context: {prize_context}
"""
    if roast_angle:
        prompt += f"\nSpecific roast angle requested: {roast_angle}. Lead with this angle, use the stats as supporting ammunition."
    else:
        prompt += "\nGive Katerina's roast. One or two sentences only. Mix up the angle — stats, behaviour, bet sizing, prize stakes, rank, whatever stings most for this player."

    try:
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 150,
            "system": system,
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
            result = json.loads(resp.read())
            text_blocks = [b for b in result.get("content", []) if b.get("type") == "text"]
            return text_blocks[0]["text"].strip() if text_blocks else None
    except Exception as e:
        logger.error(f"Roast API call failed: {e}")
        fallbacks_last = [
            f"{data['name']}'s at the bottom. That champion jersey is not going to them. 💀",
            f"Last place with {days_to_final} days left. {data['name']} needs a miracle and better bets. 🙏",
            f"The runner-up jersey is looking very out of reach for {data['name']} right now. Very. 😬",
            f"{data['name']} has {data['credits']}c and is dead last. The champion jersey has other plans. 📉",
        ]
        fallbacks_general = [
            f"{data['name']} has a {data['win_rate']}% win rate. I've seen better odds on a coin toss. 🪙",
            f"Rank {data['rank']} of {data['total_players']}. {data['name']} is committed to that position. 😏",
            f"{data['name']} — {data['wins']} wins, {data['losses']} losses. The numbers don't lie. 😬",
            f"{data['credits']}c in the tank. {data['name']} is either strategic or in denial.",
            f"With {days_to_final} days to the Final, {data['name']}'s got some ground to make up. Understatement of the tournament.",
            f"{data['name']} placed {data['total_bets']} bets and won {data['wins']}. Quantity over quality isn't working. 📊",
        ]
        pool = fallbacks_last * 2 + fallbacks_general if data['is_last'] else fallbacks_general
        return random.choice(pool)


# ── /roast command ────────────────────────────────────────────────────────────
async def cmd_roast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_message(update):
        await update.message.reply_text("Roasts happen in the group, not in private. 😏")
        return

    bot_username = context.bot.username.lower() if context.bot.username else ""

    # Check if roasting the bot itself
    if context.args:
        target_arg = context.args[0].lstrip("@").lower()
        if target_arg in (bot_username, "katerina", "katerina_bot", "degenWC_bot".lower(), "degenwc_bot"):
            lines = [
                "Oh you want to roast me? I run the book, sweetheart. I always win. 😘",
                "Bold move targeting the dealer. I respect it. Still won't work. 💅",
                "You really tried to roast the house. Adorable.",
                "Coming for me? I've seen better attempts from bottom-of-the-table bettors. 😏",
                "Cute. Now go place a bet and let the adults talk. 🎰",
                "I don't get roasted. I do the roasting around here. Try again. 😒",
                "Targeting the house? That's either very brave or very stupid. Either way, I'm flattered. 😏",
                "Bold. Wrong. But bold. 💅",
            ]
            await update.message.reply_text(random.choice(lines))
            return

    # Resolve target
    target_uid = None
    target_name = None

    if context.args and context.args[0].lower() == "all":
        # Roast everyone in the group — parallel API calls
        standings = sheet.get_standings()
        if not standings:
            await update.message.reply_text("Nobody to roast yet. 😏")
            return
        async def _roast_one(u):
            data = await _get_roast_data(u["user_id"])
            roast = await _generate_roast(data["name"], data)
            return f"• {roast}" if roast else None
        results = await asyncio.gather(*[_roast_one(u) for u in standings])
        roast_lines = [r for r in results if r]
        if roast_lines:
            header = "🎤 Katerina has something for everyone:"
            msg = header + "\n\n" + "\n\n".join(roast_lines)
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("Tried to roast everyone, came up blank. Rare day. 😒")
        return

    if context.args:
        target_arg = context.args[0].lstrip("@").lower()

        # "me" → roast the sender
        if target_arg == "me":
            target_uid = update.effective_user.id
            u = sheet.cache["users"].get(target_uid, {})
            if not u:
                await update.message.reply_text("You're not even registered. That's the real roast. 😏")
                return
            target_name = u.get("first_name") or u.get("username") or "you"
        else:
            def _clean(s):
                # Strip emojis and non-alphanumeric for comparison
                return re.sub(r'[^\w]', '', s).lower()

            target_clean = _clean(target_arg)
            best_uid = None
            best_score = 0

            for uid, u in sheet.cache["users"].items():
                uname_clean = _clean(u.get("username") or "")
                fname_clean = _clean(u.get("first_name") or "")
                # Exact match first
                if target_arg == (u.get("username") or "").lower() or target_arg == (u.get("first_name") or "").lower():
                    best_uid = uid
                    best_score = 100
                    break
                # Cleaned fuzzy match
                for candidate in [uname_clean, fname_clean]:
                    if not candidate:
                        continue
                    from rapidfuzz import fuzz as _fuzz
                    score = _fuzz.ratio(target_clean, candidate)
                    if score > best_score:
                        best_score = score
                        best_uid = uid

            if best_uid and best_score >= 70:
                target_uid = best_uid
                u = sheet.cache["users"].get(target_uid, {})
                target_name = u.get("first_name") or u.get("username")
            else:
                await update.message.reply_text(f"I don't know @{context.args[0].lstrip('@')}. Not registered, or just irrelevant. 🤷")
                return

    else:
        # No target — Katerina picks someone at random
        standings = sheet.get_standings()
        if not standings:
            await update.message.reply_text("No players to roast yet. Come back when someone's losing. 😏")
            return
        picked = random.choice(standings)
        target_uid = picked["user_id"]
        u = sheet.cache["users"].get(target_uid, {})
        target_name = u.get("first_name") or u.get("username") or "someone"

    # Build roast data and generate
    roast_angle = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else None
    data = await _get_roast_data(target_uid)
    roast = await _generate_roast(target_name, data, roast_angle=roast_angle)
    await update.message.reply_text(f"🎤 {roast}")


# ── Katerina mention handler ──────────────────────────────────────────────────
async def send_stage_hype(current_stage: str, next_stage: str, notify_fn=None, stage_pl_summary: str = "") -> bool:
    """Generate and send a Katerina stage transition hype message. Returns True if sent."""
    try:
        standings = sheet.get_standings()
        leader = standings[0] if standings else None
        leader_name = truncate(leader.get("first_name") or leader.get("username") or "Someone") if leader else "Someone"

        standings_str = "\n".join(
            f"{i}. {truncate(u.get('first_name') or u.get('username') or 'Unknown')} — {u['credits']}c"
            for i, u in enumerate(standings, 1)
        ) if standings else "No standings yet"

        prompt = (
            f"The {current_stage} has just ended. The {next_stage} begins next. "
            f"You are Katerina, the savage, unfiltered hype bot for WC Kings 2026, a private betting group. "
            f"Write a 3-4 sentence hype message that gets the group fired up for the next stage. "
            f"Be dramatic, ruthless, and exciting. Reference specific notable bets or P&L from the stage if provided. "
            f"Mention who is leading the credits standings going into the next stage. "
            f"The prize: 1st place wins the World Cup champion jersey. 2nd place wins the runner-up jersey. "
            f"No hashtags. No markdown.\n\n"
            f"CREDITS STANDINGS:\n{standings_str}\n\n"
            f"STAGE HIGHLIGHTS:\n{stage_pl_summary if stage_pl_summary else 'No stage summary available.'}"
        )

        bot_context = _build_katerina_context()
        hype = await _call_katerina(prompt, bot_context)
        if not hype:
            if notify_fn:
                await notify_fn(f"⚠️ Katerina stage hype failed — no response from API. Use /admin_stage_hype manually.")
            return False

        from scheduler import send_group
        await send_group(f"🔥 {hype}")
        logger.info(f"Stage hype sent: {current_stage} → {next_stage}")
        return True
    except Exception as e:
        logger.error(f"Stage hype failed: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Stage hype failed: {e}")
        return False


async def check_and_send_stage_hype(notify_fn=None):
    """
    Called after EOD. Checks if today is the last day of a stage and tomorrow
    starts a new one. If so, fires Katerina hype. No-op if no transition detected.
    """
    today_ct = datetime.now(CT).date()
    tomorrow_ct = today_ct + timedelta(days=1)

    current_stage = None
    next_stage = None

    for i, stage in enumerate(TOURNAMENT_STAGES):
        if stage["start"] <= today_ct <= stage["end"]:
            current_stage = stage["name"]
            # Check if today is the last day of this stage
            if today_ct == stage["end"] and i + 1 < len(TOURNAMENT_STAGES):
                next_stage = TOURNAMENT_STAGES[i + 1]["name"]
            break

    if not current_stage or not next_stage:
        logger.info("Stage hype check: no transition today, skipping")
        return

    logger.info(f"Stage transition detected: {current_stage} → {next_stage}, firing hype")
    await send_stage_hype(current_stage, next_stage, notify_fn=notify_fn)


async def send_bailout_roast(users: list, amount: int, notify_fn=None):
    """Roast all players for needing a bailout credit top-up."""
    try:
        standings = sheet.get_standings()
        zero_players = [
            truncate(s.get("first_name") or s.get("username") or "Someone")
            for s in standings if s.get("credits", 0) == 0
        ]
        bottom_players = [
            f"{truncate(s.get('first_name') or s.get('username') or '?')} ({s['credits']}c)"
            for s in standings[-2:]
        ] if standings else []

        leaderboard_str = "\n".join(
            f"{i+1}. {truncate(s.get('first_name') or s.get('username') or '?')} — {s['credits']}c"
            for i, s in enumerate(standings)
        ) if standings else "No standings available."

        zero_note = f"Players with 0 credits: {', '.join(zero_players)}. " if zero_players else ""
        bottom_note = f"Bottom of the leaderboard: {', '.join(bottom_players)}. " if bottom_players else ""

        prompt = (
            f"The admin just gave every player in WC Kings 2026 a {amount} credit bailout. "
            f"Current leaderboard:\n{leaderboard_str}\n\n"
            f"{zero_note}{bottom_note}"
            f"You are Katerina, savage and unfiltered. "
            f"Roast all of them mercilessly for being so bad at betting that they needed a bank bailout. "
            f"Specifically call out anyone on 0 credits or near the bottom. Name them. Be brutal, funny, and specific. "
            f"Keep it under 150 words. No hashtags."
        )
        bot_context = _build_katerina_context()
        roast = await _call_katerina(prompt, bot_context)
        if not roast:
            if notify_fn:
                await notify_fn("⚠️ Katerina bailout roast failed — no response from API.")
            return
        from scheduler import send_group
        await send_group(f"💸 {roast}")
    except Exception as e:
        logger.error(f"Bailout roast failed: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Bailout roast failed: {e}")


# ── Katerina mention handler ──────────────────────────────────────────────────
async def handle_katerina_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply when bot is @mentioned or 'katerina' appears in message."""
    if not update.message or not update.message.text:
        return
    if not is_group_message(update):
        return
    if update.effective_user and update.effective_user.is_bot:
        return

    # Ignore messages sent before startup (backlog replay after restart)
    msg_time = update.message.date
    if msg_time is not None:
        msg_utc = msg_time.replace(tzinfo=UTC) if msg_time.tzinfo is None else msg_time.astimezone(UTC)
        if msg_utc < (_startup_time - timedelta(seconds=60)):
            logger.info(f"Ignoring pre-startup mention from {update.effective_user.first_name} at {msg_utc}")
            return

    text = update.message.text
    bot_username = f"@{context.bot.username}" if context.bot.username else ""

    # Check triggers
    is_mention = (
        (bot_username and bot_username.lower() in text.lower()) or
        "katerina" in text.lower()
    )
    if not is_mention:
        return

    # Strip the trigger word to get the actual question
    clean = text
    if bot_username:
        clean = clean.replace(bot_username, "").replace(bot_username.lower(), "")
    clean = clean.replace("Katerina", "").replace("katerina", "").strip()
    if not clean:
        clean = "say something"

    # Build sender context
    sender = update.effective_user
    sender_uid = sender.id
    sender_cache = sheet.cache["users"].get(sender_uid, {})
    sender_name = sender_cache.get("first_name") or sender_cache.get("username") or sender.first_name or sender.username or "someone"
    sender_credits = sender_cache.get("credits", 0)
    sender_in_bet = _get_in_bet_for_katerina(sender_uid)
    sender_effective = sender_credits + sender_in_bet

    # Build sender stats for Katerina
    all_sender_bets = [b for b in sheet.cache["bets"] if b["user_id"] == sender_uid and b["status"] in ("won", "lost")]
    sender_wins = sum(1 for b in all_sender_bets if b["status"] == "won")
    sender_losses = sum(1 for b in all_sender_bets if b["status"] == "lost")
    standings = sheet.get_standings()
    sender_rank = next((i+1 for i, u in enumerate(standings) if u["user_id"] == sender_uid), None)
    credit_detail = f"{sender_credits:,}c in wallet"
    if sender_in_bet > 0:
        credit_detail += f" + {sender_in_bet:,}c in active bets = {sender_effective:,}c total"
    sender_stats = (
        f"Credits: {credit_detail} | "
        f"Record: {sender_wins}W-{sender_losses}L | "
        f"Rank: {sender_rank} of {len(standings)}"
    ) if sender_rank else f"Credits: {credit_detail} (not yet ranked)"

    # Check if this is a predictions/odds/analysis question — trigger web search
    search_keywords = ["odds", "prediction", "predict", "outside world", "favourite", "favorite",
                       "who will win", "analysis", "expert", "betting line", "what do you think",
                       "chance", "likely", "bookie", "market"]
    wants_analysis = any(kw in clean.lower() for kw in search_keywords)

    web_results = ""
    if wants_analysis:

        # Resolve to the team's next upcoming match first
        now_utc = datetime.now(UTC)
        match_query = None
        for m in sorted(sheet.cache.get("matches", {}).values(), key=lambda x: x.get("kickoff_utc", "")):
            home = m.get("home", "")
            away = m.get("away", "")
            if home.lower() in clean.lower() or away.lower() in clean.lower():
                try:
                    ko = datetime.strptime(m["kickoff_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                    if ko > now_utc and m.get("status") in ("SCHEDULED", "TIMED"):
                        match_query = f"{home} vs {away} prediction analyst preview 2026 World Cup"
                        break
                except Exception:
                    continue
        if not match_query:
            match_query = f"{clean} 2026 World Cup analyst prediction preview"

        search_prompt = (
            f"Search for analyst predictions and expert previews for: {match_query}. "
            f"Focus on: win probability, expected scoreline or predicted result, team form and recent performance. "
            f"Do NOT include individual player highlights, live betting odds, or bookmaker prices. "
            f"Return a factual 3-sentence summary of what analysts and pundits are saying. No fluff."
        )

        try:
            # Step 1 — send search request, get tool_use block back
            step1_payload = json.dumps({
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": search_prompt}]
            }).encode()
            req1 = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=step1_payload,
                headers={
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-api-key": ANTHROPIC_API_KEY
                }
            )
            with urllib.request.urlopen(req1, timeout=20) as resp1:
                step1 = json.loads(resp1.read())

            # Extract tool_use blocks from step 1 response
            tool_use_blocks = [b for b in step1.get("content", []) if b.get("type") == "tool_use"]

            if not tool_use_blocks:
                # Model returned text directly without searching
                for block in step1.get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        web_results = block["text"].strip()
                        break
            else:
                # Step 2 — send tool results back to get final answer
                # web_search_20250305 is server-side; tool_result content="" is correct
                messages = [
                    {"role": "user", "content": search_prompt},
                    {"role": "assistant", "content": step1.get("content", [])},
                    {"role": "user", "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": b["id"],
                            "content": ""
                        }
                        for b in tool_use_blocks
                    ]}
                ]
                step2_payload = json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 800,
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    "messages": messages
                }).encode()
                req2 = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=step2_payload,
                    headers={
                        "content-type": "application/json",
                        "anthropic-version": "2023-06-01",
                        "x-api-key": ANTHROPIC_API_KEY
                    }
                )
                with urllib.request.urlopen(req2, timeout=20) as resp2:
                    step2 = json.loads(resp2.read())

                # Extract text — may need another round if model searched again
                step2_tool_use = [b for b in step2.get("content", []) if b.get("type") == "tool_use"]
                if step2_tool_use:
                    # Model searched again — do one more round
                    messages2 = messages + [
                        {"role": "assistant", "content": step2.get("content", [])},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": b["id"], "content": ""}
                            for b in step2_tool_use
                        ]}
                    ]
                    step3_payload = json.dumps({
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 800,
                        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                        "messages": messages2
                    }).encode()
                    req3 = urllib.request.Request(
                        "https://api.anthropic.com/v1/messages",
                        data=step3_payload,
                        headers={
                            "content-type": "application/json",
                            "anthropic-version": "2023-06-01",
                            "x-api-key": ANTHROPIC_API_KEY
                        }
                    )
                    with urllib.request.urlopen(req3, timeout=20) as resp3:
                        step2 = json.loads(resp3.read())

                for block in step2.get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        web_results = block["text"].strip()
                        break

        except Exception as e:
            logger.error(f"Katerina web search failed: {e}")
            web_results = ""

    bot_context = _build_katerina_context()
    if web_results:
        bot_context += f"\nWEB SEARCH RESULTS (use this to answer their question about match predictions):\n{web_results}"
    elif wants_analysis:
        bot_context += "\nWEB SEARCH: Could not retrieve current expert predictions. Tell the user you couldn't find current opinions on this. Do NOT speculate, invent analysis, or fabricate any facts."

    if _chat_history:
        history_str = "\n".join(list(_chat_history)[-50:])
        bot_context += f"\n\nRECENT GROUP CHAT (last {min(len(_chat_history), 50)} messages):\n{history_str}"

    # During silent hours — DM sender instead of replying in group
    if is_silent_hours():
        quiet_lines = [
            "Quiet hours. Bets are still open but I'm not taking questions right now. Back at 7:30AM. 😌",
            "I'm off the clock. Place your bets via /bet if you need to — I'll be back at 7:30AM. 🌙",
            "Sleeping hours. The house is still open for bets, just not for chat. See you at 7:30AM. 😴",
            "Quiet hours. Use /bet if you need to place one. Questions can wait till 7:30AM. 😌",
            "Taking a break. Bets still work — just use /bet. I'm back at 7:30AM SGT. 🌙",
        ]
        try:
            await application.bot.send_message(
                chat_id=update.effective_user.id,
                text=random.choice(quiet_lines)
            )
        except Exception as e:
            logger.warning(f"Could not DM {update.effective_user.id} during silent hours: {e}")
        return

    reply = await _call_katerina(clean, bot_context, sender_name=sender_name, sender_stats=sender_stats)
    if reply:
        await update.message.reply_text(reply)
    else:
        fallbacks = [
            "I'm thinking. Don't rush me. 😒",
            "Give me a second, I'm counting other people's losses. 💸",
            "Busy. Try again. 😏",
        ]
        await update.message.reply_text(random.choice(fallbacks))