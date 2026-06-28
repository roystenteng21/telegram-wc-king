import logging
from datetime import datetime, timedelta, date
from telegram import Update
from telegram.ext import ContextTypes

from config import (
    ADMIN_TELEGRAM_ID, SGT, UTC, CT, TEAM_DISPLAY,
    RESULT_OUTCOMES, ALL_OUTCOMES,
    SESSION_EXPIRY, BET_LOCK_BUFFER, PARLAY_MULTIPLIERS, NAME_OVERRIDES
)
import sheet
import scheduler as sched
import api


# ── Portugal intervention helper ──────────────────────────────────────────────
def _is_portugal_win(match: dict, outcome: str) -> bool:
    return (
        (match.get("home") == "Portugal" and outcome == "home") or
        (match.get("away") == "Portugal" and outcome == "away")
    )


async def _send_por_intervention(update, user_id: int, amount: int, match: dict):
    """Fire Katerina Portugal intervention to the group after a POR win bet."""
    try:
        user = sheet.cache["users"].get(user_id, {})
        name = user.get("first_name") or user.get("username") or "Someone"
        prompt = (
            f"{name} just bet {amount}c on Portugal to win. "
            f"Roast them for it — Ronaldo hasn't won a major tournament since Euro 2016 and he's coasting on legacy. "
            f"Tell them to run /cancel to save their credits. 1-2 sentences, savage but fun. No markdown."
        )
        msg = await sched._katerina_line(
            prompt,
            f"⚠️ {name} just put {amount}c on Portugal. Ronaldo's been collecting paychecks, not trophies, since 2016. Run /cancel before kickoff — I'm trying to save you here. 🙏"
        )
        await sched.send_group(f"⚠️ {msg}")
    except Exception as e:
        logger.error(f"POR intervention failed: {e}")
from helpers import (
    dm_admin, is_silent_hours, is_group_message,
    send_confirmation, format_team, format_match_teams, format_outcome_label,
    truncate, session_expired, clear_session,
    ensure_registered, resolve_team, find_match_for_team,
    map_outcome_to_result, get_market_for_outcome,
    _sessions
)

logger = logging.getLogger(__name__)


def _display_name(user_dict: dict) -> str:
    """Apply name overrides and truncate for display."""
    name = truncate(user_dict.get("first_name") or user_dict.get("username") or "?")
    return NAME_OVERRIDES.get(name, name)


def _get_in_bet(user_id: int) -> int:
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

    # If EOD has fired today, show tomorrow's matches instead
    eod_fired = sheet.cache.get("eod_date") == today_ct
    if eod_fired:
        tomorrow_ct = (datetime.now(CT) + timedelta(days=1)).strftime("%Y-%m-%d")
        day_after_utc = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d")
        next_matches_raw = await sheet.get_matches_for_date(today_utc) + await sheet.get_matches_for_date(tomorrow_utc) + await sheet.get_matches_for_date(day_after_utc)
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
    parlay_summary = {}  # parlay_id -> {name, total_legs, stake, multiplier, legs_seen}

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
                name = _display_name(user) if user else "?"
                outcome_label = format_outcome_label(b["outcome"], m)
                if b["status"] == "won":
                    icon = " ✅"
                elif b["status"] == "lost":
                    icon = " ❌"
                else:
                    icon = ""

                pid = b.get("parlay_id", "")
                if pid and str(pid) not in ("", "0"):
                    # Skip legs whose parlay is already dead (a leg lost)
                    if not sheet.is_parlay_alive(pid):
                        continue
                    # Collect parlay info for summary
                    if pid not in parlay_summary:
                        all_legs = sheet.get_parlay_bets(pid)
                        total_legs = len(all_legs)
                        multiplier = PARLAY_MULTIPLIERS.get(total_legs, min(PARLAY_MULTIPLIERS.values()))
                        parlay_summary[pid] = {
                            "name": name,
                            "total_legs": total_legs,
                            "stake": b["amount"],
                            "multiplier": multiplier,
                            "legs_seen": 0
                        }
                    parlay_summary[pid]["legs_seen"] += 1
                    leg_num = parlay_summary[pid]["legs_seen"]
                    total = parlay_summary[pid]["total_legs"]
                    lines.append(f"• {name} — {outcome_label}{icon} — 🎰 {leg_num}/{total}")
                else:
                    lines.append(f"• {name} — {outcome_label} — {b['amount']:,}c{icon}")

        lines.append("")  # blank line between matches

    # Parlay summary at bottom
    if parlay_summary:
        lines.append("🎰 Parlays")
        for pid, p in parlay_summary.items():
            potential = int(p["stake"] * p["multiplier"])
            lines.append(f"• {p['name']} — {p['total_legs']}-leg · {p['stake']:,}c → {potential:,}c if all win")
        lines.append("")

    # Katerina commentary — day overview, light banter
    try:
        all_open_bets = [b for b in sheet.cache["bets"] if b["status"] == "open"]
        if all_open_bets and day_matches:
            bet_parts = []
            for m in sorted(day_matches, key=lambda x: x["kickoff_utc"]):
                match_bets = [b for b in all_open_bets if b["match_id"] == str(m["match_id"])]
                for b in match_bets:
                    user = sheet.cache["users"].get(b["user_id"], {})
                    name = _display_name(user) if user else "?"
                    pid = b.get("parlay_id", "")
                    is_parlay = bool(pid) and str(pid) not in ("", "0")
                    label = format_outcome_label(b["outcome"], m)
                    match_label = f"{format_team(m['home'])} vs {format_team(m['away'])}"
                    bet_parts.append(f"{name} {'(parlay) ' if is_parlay else ''}on {label} for {match_label}")
            if bet_parts:
                prompt = (
                    f"Today's bets in WC Kings 2026: {', '.join(bet_parts)}. "
                    f"Write one short line reacting to the day's action — light banter, mention names, "
                    f"note any parlays running. 1-2 sentences max. No hashtags. No markdown."
                )
                commentary = await sched._katerina_line(prompt, "", max_tokens=120)
                if commentary:
                    lines.append(commentary)
    except Exception as e:
        logger.warning(f"Katerina /matches commentary failed: {e}")

    await update.message.reply_text("\n".join(lines))


# ── /balance ──────────────────────────────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await ensure_registered(update)
    credits = user_data["credits"]
    user_dict = sheet.cache["users"].get(update.effective_user.id, {})
    name = _display_name(user_dict) if user_dict else truncate(update.effective_user.first_name or "")
    in_bet = _get_in_bet(update.effective_user.id)
    if in_bet > 0:
        await update.message.reply_text(f"💰 {name}, your balance: {credits:,}c (+{in_bet:,}c in active bets)")
    else:
        await update.message.reply_text(f"💰 {name}, your balance: {credits:,}c")


# ── /groups ───────────────────────────────────────────────────────────────────
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)

    # During knockout stage, group standings are no longer relevant
    from config import TOURNAMENT_STAGES
    today = datetime.now(UTC).date()
    group_stage = next((s for s in TOURNAMENT_STAGES if s["name"] == "Group Stage"), None)
    if group_stage and today > group_stage["end"]:
        await update.message.reply_text(
            "Group stage is over.\nUse /brackets for the knockout bracket."
        )
        return

    try:
        today_matches = await sched.get_today_ct_matches()
        today_teams = {team for m in today_matches for team in (m["home"], m["away"])}

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


# ── /brackets ─────────────────────────────────────────────────────────────────
async def cmd_brackets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)

    from config import TOURNAMENT_STAGES
    today = datetime.now(UTC).date()
    group_stage = next((s for s in TOURNAMENT_STAGES if s["name"] == "Group Stage"), None)
    if group_stage and today <= group_stage["end"]:
        await update.message.reply_text(
            "Group stage is still ongoing.\nUse /groups for current standings."
        )
        return

    try:
        matches = api.fetch_knockout_matches()
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ Could not fetch bracket: {e}")
        return

    if not matches:
        await update.message.reply_text("Knockout bracket not yet available.")
        return

    STAGE_ORDER = [
        ("ROUND_OF_32",  "🔵 Round of 32"),
        ("ROUND_OF_16",  "🟡 Round of 16"),
        ("QUARTER_FINAL","🟠 Quarterfinals"),
        ("SEMI_FINAL",   "🔴 Semifinals"),
        ("THIRD_PLACE",  "🥉 Third Place"),
        ("FINAL",        "🏆 Final"),
    ]

    by_stage = {}
    for m in matches:
        by_stage.setdefault(m["stage"], []).append(m)

    lines = ["🗓 WC 2026 Bracket\n"]
    for stage_key, stage_label in STAGE_ORDER:
        if stage_key not in by_stage:
            continue
        lines.append(stage_label)
        for m in sorted(by_stage[stage_key], key=lambda x: x["utcDate"]):
            home = format_team(m["home"]) if m["home"] != "TBD" else "TBD"
            away = format_team(m["away"]) if m["away"] != "TBD" else "TBD"
            if m["status"] == "FINISHED" and m["home_score"] is not None:
                lines.append(f"{home} {m['home_score']}–{m['away_score']} {away} ✅")
            elif m["status"] in ("IN_PLAY", "PAUSED", "HALFTIME"):
                lines.append(f"{home} vs {away} ● Live")
            else:
                try:
                    ko = datetime.strptime(m["utcDate"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
                    ko_sgt = ko.astimezone(SGT).strftime("%-d %b, %-I:%M%p SGT").lower()
                    lines.append(f"{home} vs {away} — {ko_sgt}")
                except Exception:
                    lines.append(f"{home} vs {away}")
        lines.append("")

    await update.message.reply_text("\n".join(lines))


# ── /leaderboard ──────────────────────────────────────────────────────────────
async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    standings = sheet.get_standings()
    if not standings:
        await update.message.reply_text("No players yet.")
        return

    lines = ["🏆 Leaderboard\n"]
    for i, user in enumerate(standings, 1):
        name = _display_name(user)
        badge = " 🏆" if i == 1 else ""
        in_bet = _get_in_bet(user["user_id"])
        credits_str = f"{user['credits']:,}c"
        if in_bet > 0:
            credits_str += f" (+{in_bet:,}c live)"
        lines.append(f"{i}. {name}{badge} — {credits_str}")

    await update.message.reply_text("\n".join(lines))


# ── /mybets ───────────────────────────────────────────────────────────────────
async def cmd_mybets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    user_id = update.effective_user.id

    all_open = [b for b in sheet.cache["bets"] if b["user_id"] == user_id and b["status"] == "open"]

    if not all_open:
        await update.message.reply_text("You have no open bets.")
        return

    singles = [b for b in all_open if not b.get("parlay_id", "")]
    parlay_ids = []
    seen = set()
    for b in all_open:
        pid = b.get("parlay_id", "")
        if pid and pid not in seen:
            parlay_ids.append(pid)
            seen.add(pid)

    alive_parlay_ids = [pid for pid in parlay_ids if sheet.is_parlay_alive(pid)]

    if not singles and not alive_parlay_ids:
        await update.message.reply_text("You have no open bets.")
        return

    lines = ["📋 Your open bets:\n"]

    # Singles
    if singles:
        lines.append("Singles:")
        for i, bet in enumerate(singles, 1):
            match = await sheet.get_match_by_id(bet["match_id"])
            if match:
                outcome_label = format_outcome_label(bet["outcome"], match)
                home = format_team(match["home"])
                away = format_team(match["away"])
                label = f"{home} vs {away} — {outcome_label}"
            else:
                label = f"Match {bet['match_id']} — {bet['outcome'].capitalize()}"
            lines.append(f"{i}. {label} — {bet['amount']:,}c")

    # Parlays
    for pid in alive_parlay_ids:
        legs = sheet.get_parlay_bets(pid)
        if not legs:
            continue
        stake = legs[0]["amount"]
        num_legs = len(legs)
        if num_legs < 2:
            # Incomplete parlay — partial placement failure, show as warning
            lines.append(f"\n⚠️ Incomplete parlay (1 leg placed — use /cancelparlay to refund {stake:,}c):")
        else:
            multiplier = PARLAY_MULTIPLIERS.get(num_legs, min(PARLAY_MULTIPLIERS.values()))
            potential = int(stake * multiplier)
            lines.append(f"\n🎰 Parlay ({num_legs}-leg · {stake:,}c → {potential:,}c if all win):")
        for leg in legs:
            match = await sheet.get_match_by_id(leg["match_id"])
            if match:
                outcome_label = format_outcome_label(leg["outcome"], match)
                home = format_team(match["home"])
                away = format_team(match["away"])
                lines.append(f"• {home} vs {away} — {outcome_label}")
            else:
                lines.append(f"• Match {leg['match_id']} — {leg['outcome'].capitalize()}")

    footer = []
    if singles:
        footer.append("/cancel to cancel a single bet.")
    if alive_parlay_ids:
        footer.append("/cancelparlay to cancel a parlay.")
    if footer:
        lines.append("\n" + " ".join(footer))

    # Katerina one-liner on the player's bets
    try:
        user_data = sheet.cache["users"].get(user_id, {})
        name = _display_name(user_data) if user_data else "?"
        bet_parts = []
        for bet in singles:
            match = await sheet.get_match_by_id(bet["match_id"])
            if match:
                label = format_outcome_label(bet["outcome"], match)
                match_label = f"{format_team(match['home'])} vs {format_team(match['away'])}"
                bet_parts.append(f"{label} on {match_label} ({bet['amount']:,}c)")
        for pid in alive_parlay_ids:
            legs = sheet.get_parlay_bets(pid)
            if legs:
                multiplier = PARLAY_MULTIPLIERS.get(len(legs), min(PARLAY_MULTIPLIERS.values()))
                bet_parts.append(f"{len(legs)}-leg parlay ({legs[0]['amount']:,}c → {int(legs[0]['amount'] * multiplier):,}c)")
        if bet_parts:
            prompt = (
                f"{name} has these open bets: {', '.join(bet_parts)}. "
                f"Write one short line reacting to their bets — light banter. "
                f"Address them by name. 1 sentence. No hashtags. No markdown."
            )
            commentary = await sched._katerina_line(prompt, "", max_tokens=80)
            if commentary:
                lines.append(f"\n{commentary}")
    except Exception as e:
        logger.warning(f"Katerina /mybets commentary failed: {e}")

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
            await update.message.reply_text(f"Insufficient credits. Balance: {user_data['credits']:,}c")
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
                f"{flag_code} — {amount:,}c\n"
                f"Balance: {new_balance:,}c"
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
            f"Insufficient credits. Your balance: {user_data['credits']:,}c"
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
            f"{outcome_label} — {amount:,}c\n"
            f"Balance: {new_balance:,}c"
        )

        await send_confirmation(update, confirm_msg)

        # POR rule — warn if betting on Portugal to win
        if _is_portugal_win(match, internal_outcome):
            await _send_por_intervention(update, user.id, amount, match)

    except ValueError as e:
        await update.message.reply_text(str(e))
    except Exception as e:
        logger.error(f"Bet placement failed for {user.id}: {e}")
        await update.message.reply_text("Something went wrong placing your bet. Please try again.")
        await dm_admin(f"⚠️ Bet placement failed for user {user.id}: {e}")


async def _rollback_parlay(first_bet_id: str, user_id: int, amount: int, parlay_id: str):
    """Guaranteed rollback: cancel bet + restore credits. Falls back to direct credit fix if sheet fails."""
    try:
        await sheet.cancel_bet(first_bet_id, user_id)
    except Exception:
        # cancel_bet failed (likely 429) — force-restore credits directly
        try:
            current = sheet.cache["users"].get(user_id, {}).get("credits", 0)
            restored = current + amount
            await sheet.update_user_credits(user_id, restored, notify_fn=dm_admin)
            await sheet.append_ledger(user_id, "refund", amount, restored,
                                      f"Parlay {parlay_id} rollback refund", dm_admin)
            # Mark stuck leg void in cache so it doesn't show in mybets
            for b in sheet.cache["bets"]:
                if b.get("parlay_id") == parlay_id and b["status"] == "open":
                    b["status"] = "void"
        except Exception as fallback_err:
            await dm_admin(
                f"⚠️ Parlay rollback failed for user {user_id} — "
                f"manual refund of {amount}c needed. Error: {fallback_err}"
            )


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
                f"{match_line} — {bet['amount']:,}c refunded.\n"
                f"Balance: {new_balance:,}c"
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
        lines.append(f"{i}. {label} — {bet['amount']:,}c")

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
            "Multipliers: 2-leg 4.5x · 3-leg 8x · 4-leg 16x · 5-leg 32x · 6-leg 64x\n"
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
    if len(leg_parts) > 6:
        await update.message.reply_text("Maximum 6 legs per parlay.")
        return

    if user_data["credits"] < amount:
        await update.message.reply_text(f"Insufficient credits. Balance: {user_data['credits']:,}c")
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
            outcome = leg["outcome"]
            if outcome == "draw":
                outcome_label = "Draw"
            elif outcome == "home":
                outcome_label = f"{format_team(leg['match']['home'])} Win"
            elif outcome == "away":
                outcome_label = f"{format_team(leg['match']['away'])} Win"
            else:
                outcome_label = leg["outcome_display"]
            lines.append(f"{i}. {home} vs {away} — {outcome_label}")
        lines.append(f"\nStake: {amount:,}c · Win all → {potential_return:,}c back")
        lines.append(f"Balance: {new_balance:,}c")

        confirm_msg = "\n".join(lines)
        await send_confirmation(update, confirm_msg)

        # POR rule — warn if any leg bets on Portugal to win
        por_legs = [leg for leg in validated_legs if _is_portugal_win(leg["match"], leg["outcome"])]
        if por_legs:
            await _send_por_intervention(update, user.id, amount, por_legs[0]["match"])

    except ValueError as e:
        if placed_bet_ids:
            await _rollback_parlay(placed_bet_ids[0], user.id, amount, parlay_id)
        await update.message.reply_text(str(e))
    except Exception as e:
        if placed_bet_ids:
            await _rollback_parlay(placed_bet_ids[0], user.id, amount, parlay_id)
        logger.error(f"Parlay placement failed for {user.id}: {e}")
        await update.message.reply_text("Something went wrong placing your parlay. Credits refunded.")
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
                f"{count} leg(s) voided · {stake:,}c refunded.\n"
                f"Balance: {new_balance:,}c"
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
                f"{count} leg(s) voided · {stake:,}c refunded.\n"
                f"Balance: {new_balance:,}c"
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
        lines.append(f"{i}. {stake:,}c · {multiplier}x")
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
