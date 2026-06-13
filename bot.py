import logging
from telegram import Update
from telegram.ext import (
    CommandHandler, MessageHandler,
    ChatMemberHandler, ContextTypes, filters
)

from config import (
    ADMIN_TELEGRAM_ID, BOT_VERSION,
    GROUP_CHAT_ID as ENV_GROUP_CHAT_ID,
    DAILY_CREDITS, PRIZE_PLAYER_COUNT
)
import sheet
import scheduler as sched
from helpers import (
    application, dm_admin, is_group_message,
    _chat_history, set_group_chat_id, get_group_chat_id,
    _sessions, session_expired, clear_session, truncate
)
import commands_player as cp
import commands_admin as ca
import katerina

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ── Group message handler (chat ID lock + history + player count check) ───────
async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = get_group_chat_id()

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
        if gid is None:
            gid = update.effective_chat.id
            set_group_chat_id(gid)
            sched.init(context.bot, gid)
            logger.info(f"Group chat ID locked: {gid}")
            await dm_admin(f"✅ Degen locked to group chat ID: {gid}")
            try:
                sheet.get_sheet("users")
            except Exception:
                pass
        elif update.effective_chat.id != gid:
            return

        # Append non-bot messages to Katerina's context history
        if (update.message and update.message.text and
                not update.effective_user.is_bot):
            sender = update.effective_user
            sender_name = sender.first_name or sender.username or "Unknown"
            _chat_history.append(f"{sender_name}: {update.message.text}")

        # Player count change detection — DM admin if unexpected
        current_count = len(sheet.cache.get("users", {}))
        if current_count > 0 and current_count != PRIZE_PLAYER_COUNT:
            cache_key = f"_player_count_alerted_{current_count}"
            if not sheet.cache.get(cache_key):
                sheet.cache[cache_key] = True
                await dm_admin(
                    f"⚠️ Player count changed: {current_count} players in the sheet "
                    f"(expected {PRIZE_PLAYER_COUNT}).\n"
                    f"Please confirm the updated prize pool ($80 × {current_count} = ${80 * current_count})."
                )


# ── Welcome new members ───────────────────────────────────────────────────────
async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = get_group_chat_id()
    if update.effective_chat.id != gid:
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
    await context.bot.send_message(chat_id=gid, text=welcome)


# ── Handler registration ──────────────────────────────────────────────────────
def setup_handlers():
    # Katerina mention handler
    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        katerina.handle_katerina_mention
    ), group=0)

    # Group message listener (chat ID lock + history)
    application.add_handler(MessageHandler(filters.ALL, handle_any_message), group=1)

    # New member
    application.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))

    # Player commands
    application.add_handler(CommandHandler("start", cp.cmd_start))
    application.add_handler(CommandHandler("help", cp.cmd_help))
    application.add_handler(CommandHandler("matches", cp.cmd_matches))
    application.add_handler(CommandHandler("balance", cp.cmd_balance))
    application.add_handler(CommandHandler("groups", cp.cmd_groups))
    application.add_handler(CommandHandler("leaderboard", cp.cmd_leaderboard))
    application.add_handler(CommandHandler("mybets", cp.cmd_mybets))
    application.add_handler(CommandHandler("bet", cp.cmd_bet))
    application.add_handler(CommandHandler("cancel", cp.cmd_cancelbet))
    application.add_handler(CommandHandler("parlay", cp.cmd_parlay))
    application.add_handler(CommandHandler("cancelparlay", cp.cmd_cancelparlay))
    application.add_handler(CommandHandler("predict", cp.cmd_predict))
    application.add_handler(CommandHandler("roast", katerina.cmd_roast))

    # Admin commands
    application.add_handler(CommandHandler("admin_announce", ca.cmd_admin_announce))
    application.add_handler(CommandHandler("admin_status", ca.cmd_admin_status))
    application.add_handler(CommandHandler("admin_refresh", ca.cmd_admin_refresh))
    application.add_handler(CommandHandler("admin_result", ca.cmd_admin_result))
    application.add_handler(CommandHandler("admin_cancel_match", ca.cmd_admin_cancel_match))
    application.add_handler(CommandHandler("admin_credits", ca.cmd_admin_credits))
    application.add_handler(CommandHandler("admin_endtournament", ca.cmd_admin_endtournament))
    application.add_handler(CommandHandler("admin_poll", ca.cmd_admin_poll))
    application.add_handler(CommandHandler("admin_result_push", ca.cmd_admin_result_push))
    application.add_handler(CommandHandler("admin_eod_push", ca.cmd_admin_eod_push))
    application.add_handler(CommandHandler("admin_event", ca.cmd_admin_event))
    application.add_handler(CommandHandler("admin_simulate_eod", ca.cmd_admin_simulate_eod))
    application.add_handler(CommandHandler("admin_sim_night", ca.cmd_admin_sim_night))
    application.add_handler(CommandHandler("admin_sim_morning", ca.cmd_admin_sim_morning))
    application.add_handler(CommandHandler("admin_sim_prematch", ca.cmd_admin_sim_prematch))
    application.add_handler(CommandHandler("admin_sim_kickoff", ca.cmd_admin_sim_kickoff))
    application.add_handler(CommandHandler("admin_sim_result", ca.cmd_admin_sim_result))
    application.add_handler(CommandHandler("confirm_admin", ca.cmd_confirm_admin))
    application.add_handler(CommandHandler("cancel_admin", ca.cmd_cancel_admin))


# ── Startup ───────────────────────────────────────────────────────────────────
async def post_init(app):
    """Runs after bot starts — triggers startup sequence."""
    if ENV_GROUP_CHAT_ID:
        set_group_chat_id(ENV_GROUP_CHAT_ID)
        sched.init(app.bot, ENV_GROUP_CHAT_ID)
        logger.info(f"Group chat ID set from env: {ENV_GROUP_CHAT_ID}")
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
        ("roast", "Ask Katerina to roast someone"),
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
