import logging
import pytz
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes

from config import (
    ADMIN_TELEGRAM_ID, SGT, UTC, TEAM_DISPLAY,
    RESULT_OUTCOMES, OU_OUTCOMES, ALL_OUTCOMES,
    SESSION_EXPIRY, BET_LOCK_BUFFER, PARLAY_MULTIPLIERS,
    DAILY_CREDITS, BOT_VERSION
)
import sheet
import scheduler as sched
import api
from helpers import (
    application, dm_admin, is_silent_hours, is_group_message,
    send_confirmation, format_team, format_match_teams, format_outcome_label,
    truncate, get_display_name, session_expired, clear_session,
    clear_pending_bet, clear_admin_pending, ensure_registered,
    resolve_team, find_match_for_team, map_outcome_to_result,
    get_market_for_outcome, _sessions, _pending_bets, _admin_pending,
    get_group_chat_id
)

logger = logging.getLogger(__name__)

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
async def cmd_admin_katerina_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send Katerina's 'I'm back' message to the group."""
    import random
    gid = get_group_chat_id()
    if not gid:
        await update.message.reply_text("Group chat ID not set yet.")
        return
    lines = [
        "Brief intermission. The house is open again. 🎰",
        "Technical difficulties. Very beneath me. Won't happen again. 💅",
        "The house never closes for long. I'm back. 🎰",
        "Sorry for the wait. I had credits to count. 😌",
        "I'm back. Try not to make bad bets while I was away — oh wait, too late. 😏",
        "Brief absence. The ledger still balanced. I'm back. 📒",
        "Small interruption. The books are open. Place your bets. 🎰",
        "Took a moment. The house is back in business. Don't read into it. 💅",
        "I'm back, and the odds haven't changed. Neither has your form. 😏",
        "Short break. Counted the losses. Back to counting more. 📒",
    ]
    msg = random.choice(lines)
    await context.bot.send_message(chat_id=gid, text=msg)
    await update.message.reply_text("✅ Katerina's back message sent.")


async def cmd_admin_stage_hype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger Katerina stage hype. Usage: /admin_stage_hype [current stage] | [next stage]"""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    import katerina as _katerina
    from config import TOURNAMENT_STAGES

    if not context.args:
        stage_names = " / ".join(s["name"] for s in TOURNAMENT_STAGES)
        await update.message.reply_text(
            f"Usage: /admin_stage_hype [current] | [next]\n"
            f"Stages: {stage_names}\n"
            f"Example: /admin_stage_hype Group Stage | Round of 32"
        )
        return

    raw = " ".join(context.args)
    if "|" not in raw:
        await update.message.reply_text("Separate current and next stage with |.\nExample: /admin_stage_hype Group Stage | Round of 32")
        return

    parts = raw.split("|", 1)
    current_stage = parts[0].strip()
    next_stage = parts[1].strip()

    await update.message.reply_text(f"Firing Katerina hype: {current_stage} → {next_stage}...")
    sent = await _katerina.send_stage_hype(current_stage, next_stage, notify_fn=dm_admin)
    if sent:
        await update.message.reply_text("✅ Stage hype sent to group.")
    else:
        await update.message.reply_text("⚠️ Stage hype failed — check admin DM for details.")


async def cmd_admin_silent_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle silent hours on/off. Resets to on on bot restart."""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        current = "OFF" if sheet.cache.get("silent_hours_disabled", False) else "ON"
        await update.message.reply_text(f"Silent hours currently: {current}\nUsage: /admin_silent_hours on|off")
        return
    arg = context.args[0].lower()
    if arg == "off":
        sheet.cache["silent_hours_disabled"] = True
        await update.message.reply_text("✅ Silent hours disabled. Katerina will reply in group during 12AM–7:30AM.")
    elif arg == "on":
        sheet.cache["silent_hours_disabled"] = False
        await update.message.reply_text("✅ Silent hours enabled.")
    else:
        await update.message.reply_text("Usage: /admin_silent_hours on|off")


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

    silent_state = "OFF (disabled)" if sheet.cache.get("silent_hours_disabled", False) else "ON"
    text = (
        f"✅ Degen v{BOT_VERSION} is running\n"
        f"Sheet: Connected\n"
        f"Cache: {len(sheet.cache['users'])} users, "
        f"{len(sheet.cache['matches'])} matches, "
        f"{len(sheet.cache['bets'])} bets\n"
        f"Scheduler: {len(jobs)} active jobs\n"
        f"Last cache refresh: {refresh_str}\n"
        f"Silent hours: {silent_state}\n"
        f"Group chat ID: {get_group_chat_id()}"
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
