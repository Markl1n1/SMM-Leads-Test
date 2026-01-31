import asyncio
import threading
from datetime import datetime

from flask import Flask, request, jsonify

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from telegram.request import HTTPXRequest

from bot.config import (
    TELEGRAM_BOT_TOKEN,
    WEBHOOK_URL,
    CLEANUP_INTERVAL_MINUTES,
    SUPABASE_URL,
    SUPABASE_KEY,
    PIN_CODE,
    PORT,
    is_facebook_flow_enabled,
)
from bot.constants import (
    SMART_CHECK_INPUT,
    CHECK_BY_TELEGRAM,
    CHECK_BY_FB_LINK,
    CHECK_BY_TELEGRAM_ID,
    CHECK_BY_FULLNAME,
    ADD_FULLNAME,
    ADD_FB_LINK,
    ADD_TELEGRAM_NAME,
    ADD_TELEGRAM_ID,
    ADD_REVIEW,
    EDIT_PIN,
    EDIT_MENU,
    EDIT_FULLNAME,
    EDIT_FB_LINK,
    EDIT_TELEGRAM_NAME,
    EDIT_TELEGRAM_ID,
    EDIT_MANAGER_NAME,
    TAG_PIN,
    TAG_SELECT_MANAGER,
    TAG_ENTER_NEW,
    TRANSFER_PIN,
    TRANSFER_SELECT_FROM,
    TRANSFER_SELECT_TO,
    TRANSFER_CONFIRM,
)
from bot.logging import logger
from bot.state import (
    async_cleanup_user_data_store,
    cleanup_rate_limit_store,
    log_conversation_state,
    user_data_store,
    user_data_store_access_time,
)
from bot.services.supabase_client import get_supabase_client
from bot.handlers.general import (
    start_command,
    quit_command,
    help_command,
    unknown_callback_handler,
    unknown_command_handler,
    check_add_state_entry,
    check_add_state_entry_callback,
    button_callback,
)
from bot.flows.check_flow import (
    check_menu_callback,
    check_telegram_callback,
    check_fb_link_callback,
    check_fullname_callback,
    check_telegram_id_callback,
    check_telegram_input,
    check_fb_link_input,
    check_telegram_id_input,
    check_fullname_input,
    smart_check_input,
    check_by_multiple_fields,
    check_by_field,
    check_by_fullname,
    check_by_extracted_fields,
)
from bot.flows.add_flow import (
    add_new_callback,
    add_from_check_photo_callback,
    add_field_input,
    show_add_review,
    add_skip_callback,
    edit_fullname_from_review_callback,
    add_edit_field_fullname_from_review_callback,
    add_edit_field_telegram_name_from_review_callback,
    add_edit_field_telegram_id_from_review_callback,
    add_edit_field_fb_link_from_review_callback,
    add_edit_back_to_review_callback,
    add_back_callback,
    add_save_callback,
    add_cancel_callback,
)
from bot.flows.photo_flow import (
    handle_photo_message,
    handle_photo_during_check,
    handle_photo_during_add,
    handle_document_during_add,
    photo_add_callback,
    photo_check_callback,
)
from bot.flows.forwarded_flow import (
    handle_forwarded_message,
    forwarded_add_callback,
    forwarded_check_callback,
)
from bot.flows.edit_flow import (
    edit_lead_entry_callback,
    edit_pin_input,
    edit_field_input,
    edit_field_fullname_callback,
    edit_field_fb_link_callback,
    edit_field_telegram_name_callback,
    edit_field_telegram_id_callback,
    edit_field_manager_callback,
    edit_save_callback,
    edit_cancel_callback,
)
from bot.flows.tag_flow import (
    tag_command,
    tag_pin_input,
    tag_manager_callback,
    tag_enter_new,
    tag_confirm_callback,
    tag_cancel_callback,
)
from bot.flows.transfer_flow import (
    transfer_command,
    transfer_pin_input,
    transfer_from_callback,
    transfer_to_callback,
    transfer_confirm_callback,
    transfer_cancel_callback,
)

# Try to monkeypatch httpx.Client to intercept proxy argument (silent in production)
try:
    import httpx

    # Store original Client.__init__
    original_httpx_client_init = httpx.Client.__init__

    def patched_httpx_client_init(self, *args, **kwargs):
        """Patched httpx.Client.__init__ to remove proxy argument"""
        if 'proxy' in kwargs:
            kwargs.pop('proxy')
        return original_httpx_client_init(self, *args, **kwargs)

    # Apply monkeypatch
    httpx.Client.__init__ = patched_httpx_client_init
except Exception:
    pass

# Initialize Flask app
app = Flask(__name__)


@app.route('/')
def index():
    """Health check endpoint for Koyeb"""
    return jsonify({
        "status": "ok",
        "service": "telegram-bot",
        "timestamp": datetime.utcnow().isoformat()
    }), 200


@app.route('/health')
def health():
    """Health check endpoint (alias for compatibility)"""
    return jsonify({
        "status": "ok",
        "service": "telegram-bot",
        "timestamp": datetime.utcnow().isoformat()
    }), 200


@app.route('/ready')
def ready():
    """Readiness probe endpoint for Koyeb"""
    supabase_client = get_supabase_client()
    checks = {
        "telegram_app": telegram_app is not None,
        "supabase_client": supabase_client is not None,
        "telegram_event_loop": telegram_event_loop is not None and telegram_event_loop.is_running() if telegram_event_loop else False
    }

    all_ready = all(checks.values())
    status_code = 200 if all_ready else 503

    return jsonify({
        "status": "ready" if all_ready else "not ready",
        "checks": checks,
        "timestamp": datetime.utcnow().isoformat()
    }), status_code


@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming Telegram updates via webhook"""
    try:
        json_data = request.get_json()
        if json_data:

            # Check if telegram_app is initialized
            if telegram_app is None:
                logger.error("Telegram app is not initialized yet")
                return "Service not ready", 503

            update = Update.de_json(json_data, telegram_app.bot)

            # Process update asynchronously using the telegram event loop
            if telegram_event_loop and telegram_event_loop.is_running():
                # Schedule update processing in the telegram event loop
                asyncio.run_coroutine_threadsafe(
                    telegram_app.process_update(update),
                    telegram_event_loop
                )
            else:
                # Fallback: process synchronously if loop not ready
                logger.warning("Telegram event loop not ready, processing update synchronously")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(telegram_app.process_update(update))
                loop.close()
        else:
            logger.warning("Received empty webhook request")
        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return "Error", 500

telegram_app = None
telegram_event_loop = None
scheduler = None

async def setup_webhook():
    """Set up the webhook for Telegram bot"""
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        # Drop pending updates to clear old states on deploy
        await telegram_app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True  # Сбрасывает все ожидающие обновления
        )
        logger.info("Webhook set successfully with pending updates dropped")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")

# Initialize Telegram application
telegram_app = None
telegram_event_loop = None

def initialize_telegram_app():
    """Initialize Telegram app - called on module import (needed for gunicorn)"""
    global telegram_app, telegram_event_loop, user_data_store, user_data_store_access_time
    
    # Clear all user data stores on startup (fresh start after deploy)
    user_data_store.clear()
    user_data_store_access_time.clear()
    logger.info("Cleared user_data_store and user_data_store_access_time on startup")
    
    # Validate environment variables first
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found - Telegram app will not be initialized")
        return
    
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not found - Telegram app will not be initialized")
        return
    
    
    try:
        # Create Telegram app
        create_telegram_app()
    except Exception as e:
        logger.error(f"Failed to create Telegram app: {e}", exc_info=True)
        return
    
    # Initialize Telegram bot in a separate thread
    import asyncio
    import threading
    
    def run_telegram_setup():
        """Run async webhook setup and start update processing in a separate thread"""
        global telegram_event_loop
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            telegram_event_loop = loop  # Save reference for webhook
            
            loop.run_until_complete(telegram_app.initialize())
            
            loop.run_until_complete(setup_webhook())
            
            # Start processing updates
            loop.run_until_complete(telegram_app.start())
            
            # Setup keep-alive scheduler after bot is started
            setup_keep_alive_scheduler()
            
            # Keep the loop running to process updates
            loop.run_forever()
        except Exception as e:
            logger.error(f"Error in Telegram setup thread: {e}", exc_info=True)
    
    # Start webhook setup in background
    setup_thread = threading.Thread(target=run_telegram_setup)
    setup_thread.daemon = True
    setup_thread.start()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors in Telegram handlers"""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    
    # Extra diagnostics for tag-related flows
    try:
        from telegram import Update as TgUpdate  # type: ignore
        if isinstance(update, TgUpdate) and update.effective_user:
            user_id = update.effective_user.id
            log_conversation_state(user_id, context, prefix="[ERROR_STATE]")
    except Exception as state_err:
        logger.error(f"[ERROR_STATE] Failed to log state in error_handler: {state_err}", exc_info=True)
    
    # Try to notify user if update is available
    # Use direct calls without retry to avoid infinite recursion
    if update and isinstance(update, Update):
        try:
            if update.message:
                try:
                    await update.message.reply_text(
                        "⚠️ <b>Произошла временная ошибка.</b>\n\n"
                        "💡 Попробуйте снова через несколько секунд.\n"
                        "Если проблема сохраняется, обратитесь к администратору.",
                        parse_mode='HTML'
                    )
                except Exception:
                    pass  # Silently fail if we can't send error message
            elif update.callback_query:
                try:
                    await update.callback_query.answer(
                        text="⚠️ Временная ошибка. Попробуйте снова. Если проблема сохраняется, обратитесь к администратору.",
                        show_alert=True
                    )
                except Exception:
                    pass  # Silently fail if we can't send error message
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

def create_telegram_app():
    """Create and configure Telegram application"""
    global telegram_app
    
    # Create application with timeout settings
    request = HTTPXRequest(connection_pool_size=8, connect_timeout=10.0, read_timeout=10.0)
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).build()
    
    # Add error handler FIRST (before other handlers)
    telegram_app.add_error_handler(error_handler)
    
    # Add command handlers
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("q", quit_command))
    telegram_app.add_handler(CommandHandler("help", help_command))
    # Note: /q command has high priority and will work from any state
    # /tag is handled via tag_conv ConversationHandler entry_points

    async def debug_log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Global debug logger for all updates (does not interfere with handlers)."""
        try:
            user_id = update.effective_user.id if getattr(update, "effective_user", None) else None
            if getattr(update, "message", None):
                msg = update.message
                is_forwarded = bool(
                    msg.forward_from or msg.forward_from_chat or msg.forward_sender_name
                )
                # ADD DETAILED LOGGING FOR TEXT MESSAGES (especially for tag PIN input)
                if msg.text and not msg.text.startswith('/'):
                    logger.info(
                        f"[UPDATE] type=message user_id={user_id} "
                        f"text='{msg.text}' is_forwarded={is_forwarded} "
                        f"is_command=False"
                    )
                    # Log conversation state for text messages to track tag flow
                    if user_id is not None:
                        log_conversation_state(user_id, context, prefix="[UPDATE_TEXT_MSG]")
                else:
                    logger.info(
                        f"[UPDATE] type=message user_id={user_id} "
                        f"text='{msg.text or ''}' is_forwarded={is_forwarded}"
                    )
            elif getattr(update, "callback_query", None):
                q = update.callback_query
                logger.info(
                    f"[UPDATE] type=callback user_id={user_id} data='{q.data}'"
                )
            else:
                logger.info(f"[UPDATE] type=other raw_update={update}")

            if user_id is not None and not (getattr(update, "message", None) and update.message.text and not update.message.text.startswith('/')):
                # Log state for non-text messages (already logged above for text messages)
                log_conversation_state(user_id, context, prefix="[UPDATE_STATE]")
        except Exception as e:
            logger.error(f"[UPDATE] Failed to log update: {e}", exc_info=True)

    # Global debug logger - register early, but it never returns states so it won't affect flows
    telegram_app.add_handler(MessageHandler(filters.ALL, debug_log_update), group=99)
    
    # Global handler for forwarded messages (register BEFORE ConversationHandlers)
    # This allows forwarding messages to work from any state
    # The handler returns None if user is already in add flow, allowing add_field_input to handle it
    forwarded_message_handler = MessageHandler(
        filters.FORWARDED,
        handle_forwarded_message
    )
    telegram_app.add_handler(forwarded_message_handler)
    
    # Smart check conversation handler (register FIRST to have priority)
    smart_check_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(check_menu_callback, pattern="^check_menu$")],
        states={
            SMART_CHECK_INPUT: [
                CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, smart_check_input),
                MessageHandler(filters.PHOTO & ~filters.FORWARDED, handle_photo_during_check),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ]
        },
        fallbacks=[
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
        ],
        per_message=False,
    )
    
    # Old conversation handlers for checking (kept for backward compatibility, but not registered)
    # These are no longer used but kept in case we need them in the future
    # check_telegram_conv = ConversationHandler(
    #     entry_points=[CallbackQueryHandler(check_telegram_callback, pattern="^check_telegram$")],
    #     states={
    #         CHECK_BY_TELEGRAM: [
    #             MessageHandler(filters.TEXT & ~filters.COMMAND, check_telegram_input),
    #             CommandHandler("q", quit_command),
    #             CommandHandler("start", start_command),
    #         ]
    #     },
    #     fallbacks=[
    #         CommandHandler("q", quit_command), 
    #         CommandHandler("start", start_command),
    #         CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
    #     ],
    #     per_message=False,
    # )
    # 
    # check_fb_link_conv = ConversationHandler(
    #     entry_points=[CallbackQueryHandler(check_fb_link_callback, pattern="^check_fb_link$")],
    #     states={
    #         CHECK_BY_FB_LINK: [
    #             MessageHandler(filters.TEXT & ~filters.COMMAND, check_fb_link_input),
    #             CommandHandler("q", quit_command),
    #             CommandHandler("start", start_command),
    #         ]
    #     },
    #     fallbacks=[
    #         CommandHandler("q", quit_command), 
    #         CommandHandler("start", start_command),
    #         CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
    #     ],
    #     per_message=False,
    # )
    # 
    # check_telegram_id_conv = ConversationHandler(
    #     entry_points=[CallbackQueryHandler(check_telegram_id_callback, pattern="^check_telegram_id$")],
    #     states={
    #         CHECK_BY_TELEGRAM_ID: [
    #             MessageHandler(filters.TEXT & ~filters.COMMAND, check_telegram_id_input),
    #             CommandHandler("q", quit_command),
    #             CommandHandler("start", start_command),
    #         ]
    #     },
    #     fallbacks=[
    #         CommandHandler("q", quit_command), 
    #         CommandHandler("start", start_command),
    #         CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
    #     ],
    #     per_message=False,
    # )
    # 
    # check_fullname_conv = ConversationHandler(
    #     entry_points=[CallbackQueryHandler(check_fullname_callback, pattern="^check_fullname$")],
    #     states={
    #         CHECK_BY_FULLNAME: [
    #             MessageHandler(filters.TEXT & ~filters.COMMAND, check_fullname_input),
    #             CommandHandler("q", quit_command),
    #             CommandHandler("start", start_command),
    #         ]
    #     },
    #     fallbacks=[
    #         CommandHandler("q", quit_command), 
    #         CommandHandler("start", start_command),
    #         CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
    #     ],
    #     per_message=False,
    # )
    
    # Conversation handler for adding - sequential flow
    add_conv_states = {
        ADD_FULLNAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_field_input),
            MessageHandler(filters.PHOTO & ~filters.FORWARDED, handle_photo_during_add),
            MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document_during_add),
            CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
            CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
        ],
    }
    if is_facebook_flow_enabled():
        add_conv_states[ADD_FB_LINK] = [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_field_input),
            MessageHandler(filters.PHOTO & ~filters.FORWARDED, handle_photo_during_add),
            MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document_during_add),
            CallbackQueryHandler(add_skip_callback, pattern="^add_skip$"),
            CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
            CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
        ]
    add_conv_states.update({
        ADD_TELEGRAM_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_field_input),
                MessageHandler(filters.PHOTO & ~filters.FORWARDED, handle_photo_during_add),
                MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document_during_add),
                CallbackQueryHandler(add_skip_callback, pattern="^add_skip$"),
                CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
                CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            ADD_TELEGRAM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_field_input),
                MessageHandler(filters.PHOTO & ~filters.FORWARDED, handle_photo_during_add),
                MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document_during_add),
                CallbackQueryHandler(add_skip_callback, pattern="^add_skip$"),
                CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
                CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            ADD_REVIEW: [
                CallbackQueryHandler(add_save_callback, pattern="^add_save$"),
                CallbackQueryHandler(add_save_callback, pattern="^add_save_force$"),
                # pre-save edit menu and field editing callbacks (без PIN)
                CallbackQueryHandler(edit_fullname_from_review_callback, pattern="^edit_fullname_from_review$"),
                CallbackQueryHandler(add_edit_field_fullname_from_review_callback, pattern="^add_edit_field_fullname$"),
                CallbackQueryHandler(add_edit_field_telegram_name_from_review_callback, pattern="^add_edit_field_telegram_name$"),
                CallbackQueryHandler(add_edit_field_telegram_id_from_review_callback, pattern="^add_edit_field_telegram_id$"),
                CallbackQueryHandler(add_edit_field_fb_link_from_review_callback, pattern="^add_edit_field_fb_link$"),
                CallbackQueryHandler(add_edit_back_to_review_callback, pattern="^add_edit_back_to_review$"),
                MessageHandler(filters.PHOTO & ~filters.FORWARDED, handle_photo_during_add),
                MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document_during_add),
                CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
                CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
    })
    
    add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_new_callback, pattern="^add_new$"),
            CallbackQueryHandler(forwarded_add_callback, pattern="^forwarded_add$"),
            CallbackQueryHandler(photo_add_callback, pattern="^photo_add$"),
            # Allow MessageHandler to enter if user has add state initialized (from forwarded message)
            MessageHandler(filters.TEXT & ~filters.COMMAND, check_add_state_entry),
            # Allow CallbackQueryHandler to enter if user has add state initialized (from forwarded message)
            # This handles callbacks like add_skip, add_back, add_save when flow was started via forwarded message
            CallbackQueryHandler(check_add_state_entry_callback, pattern="^(add_skip|add_back|add_cancel|add_save|add_save_force)$")
        ],
        states=add_conv_states,
        fallbacks=[CommandHandler("q", quit_command), CommandHandler("start", start_command)],
        per_message=False,
    )
    
    telegram_app.add_handler(smart_check_conv)  # Smart check with auto-detection
    
    # Edit conversation handler - register with other ConversationHandlers for priority
    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_lead_entry_callback, pattern="^edit_lead_\\d+$"),
            CallbackQueryHandler(edit_field_fullname_callback, pattern="^edit_field_fullname$"),
            CallbackQueryHandler(edit_field_fb_link_callback, pattern="^edit_field_fb_link$"),
            CallbackQueryHandler(edit_field_telegram_name_callback, pattern="^edit_field_telegram_name$"),
            CallbackQueryHandler(edit_field_telegram_id_callback, pattern="^edit_field_telegram_id$"),
            CallbackQueryHandler(edit_field_manager_callback, pattern="^edit_field_manager$"),
            CallbackQueryHandler(edit_save_callback, pattern="^edit_save$"),
            CallbackQueryHandler(edit_cancel_callback, pattern="^edit_cancel$"),
        ],
        states={
            EDIT_PIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pin_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_MENU: [
                CallbackQueryHandler(edit_field_fullname_callback, pattern="^edit_field_fullname$"),
                CallbackQueryHandler(edit_field_fb_link_callback, pattern="^edit_field_fb_link$"),
                CallbackQueryHandler(edit_field_telegram_name_callback, pattern="^edit_field_telegram_name$"),
                CallbackQueryHandler(edit_field_telegram_id_callback, pattern="^edit_field_telegram_id$"),
                CallbackQueryHandler(edit_field_manager_callback, pattern="^edit_field_manager$"),
                CallbackQueryHandler(edit_save_callback, pattern="^edit_save$"),
                CallbackQueryHandler(edit_cancel_callback, pattern="^edit_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_FULLNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_FB_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_TELEGRAM_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_TELEGRAM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_MANAGER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
        },
        fallbacks=[
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
            CommandHandler("skip", lambda u, c: edit_field_input(u, c)),
        ],
        per_message=False,
    )
    telegram_app.add_handler(edit_conv)
    
    # Transfer conversation handler - for reassigning leads between managers
    transfer_conv = ConversationHandler(
        entry_points=[
            CommandHandler("transfer", transfer_command),
            CallbackQueryHandler(transfer_from_callback, pattern="^transfer_from_\\d+$"),
            CallbackQueryHandler(transfer_to_callback, pattern="^transfer_to_\\d+$"),
        ],
        states={
            TRANSFER_PIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, transfer_pin_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            TRANSFER_SELECT_FROM: [
                CallbackQueryHandler(transfer_from_callback, pattern="^transfer_from_\\d+$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            TRANSFER_SELECT_TO: [
                CallbackQueryHandler(transfer_to_callback, pattern="^transfer_to_\\d+$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            TRANSFER_CONFIRM: [
                CallbackQueryHandler(transfer_confirm_callback, pattern="^transfer_confirm$"),
                CallbackQueryHandler(transfer_cancel_callback, pattern="^transfer_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
        },
        fallbacks=[
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
        ],
        per_message=False,
    )
    telegram_app.add_handler(transfer_conv)

    # Tag conversation handler - for changing manager_tag
    tag_conv = ConversationHandler(
        entry_points=[
            CommandHandler("tag", tag_command),
            CallbackQueryHandler(tag_manager_callback, pattern="^tag_mgr_\\d+$")
        ],
        states={
            TAG_PIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tag_pin_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            TAG_SELECT_MANAGER: [
                CallbackQueryHandler(tag_manager_callback, pattern="^tag_mgr_\\d+$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            TAG_ENTER_NEW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tag_enter_new),
                CallbackQueryHandler(tag_confirm_callback, pattern="^tag_confirm$"),
                CallbackQueryHandler(tag_cancel_callback, pattern="^tag_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
        },
        fallbacks=[
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
        ],
        per_message=False,
    )
    telegram_app.add_handler(tag_conv)

    # Old check handlers are no longer registered (commented out above)
    # IMPORTANT: Order matters. `tag_conv` must be registered BEFORE `add_conv`,
    # otherwise `add_conv` entry_point `check_add_state_entry` can intercept PIN input.
    # `add_conv` must be registered BEFORE `photo_message_handler` so ConversationHandler
    # gets photos first when user is in add flow.
    
    # Register callback handler for adding from check photo scenario (BEFORE add_conv)
    telegram_app.add_handler(CallbackQueryHandler(add_from_check_photo_callback, pattern="^add_from_check_photo$"))
    
    telegram_app.add_handler(add_conv)
    
    # Global handler for regular photo messages (register AFTER add_conv ConversationHandler)
    # This handles regular (not forwarded) photo messages to start add lead flow
    # when user is NOT already in add flow
    photo_message_handler = MessageHandler(
        filters.PHOTO & ~filters.FORWARDED,
        handle_photo_message
    )
    telegram_app.add_handler(photo_message_handler)
    
    # Add callback query handler for forwarded message check action (register BEFORE button_callback)
    # Note: forwarded_add_callback is registered in add_conv entry_points, so no need to register separately
    telegram_app.add_handler(CallbackQueryHandler(forwarded_check_callback, pattern="^forwarded_check$"))
    
    # Add callback query handler for photo message check action (register BEFORE button_callback)
    # Note: photo_add_callback is registered in add_conv entry_points, so no need to register separately
    telegram_app.add_handler(CallbackQueryHandler(photo_check_callback, pattern="^photo_check$"))
    
    # Add callback query handler for menu navigation buttons
    # Registered AFTER ConversationHandlers so they have priority
    # Note: check_menu is now handled by smart_check_conv ConversationHandler
    # Note: add_new is included here as fallback, but ConversationHandler should catch it first
    telegram_app.add_handler(CallbackQueryHandler(button_callback, pattern="^(main_menu|add_menu|add_new)$"))
    
    # Add handler for unknown commands during conversations (must be after command handlers)
    # Exclude /start, /q, /skip, and /tag (skip is handled by ConversationHandlers, tag should interrupt any process)
    telegram_app.add_handler(MessageHandler(filters.COMMAND & ~filters.Regex("^(/start|/q|/skip|/tag|/transfer)$"), unknown_command_handler))
    
    # Add global fallback for unknown callback queries (must be last, after all ConversationHandlers)
    telegram_app.add_handler(CallbackQueryHandler(unknown_callback_handler))
    
    return telegram_app


async def single_keep_alive():
    """Keep bot alive by calling bot.get_me() - works even in Sleeping state"""
    if telegram_app is None or telegram_app.bot is None:
        logger.warning("Keep-alive: Telegram app not initialized")
        return

    try:
        await telegram_app.bot.get_me()
        logger.debug("Keep-alive OK: bot.get_me() successful")
    except Exception as e:
        logger.warning(f"Keep-alive failed: {e}")


def cleanup_on_shutdown():
    """Cleanup resources on shutdown"""
    global scheduler, telegram_event_loop

    try:
        if telegram_app:
            if telegram_event_loop and telegram_event_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    telegram_app.stop(),
                    telegram_event_loop
                )
                asyncio.run_coroutine_threadsafe(
                    telegram_app.shutdown(),
                    telegram_event_loop
                )
    except Exception as e:
        logger.error(f"Error stopping Telegram app: {e}", exc_info=True)


def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown"""
    import signal
    import sys

    def signal_handler(signum, frame):
        cleanup_on_shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

# Setup signal handlers for graceful shutdown
setup_signal_handlers()

def setup_keep_alive_scheduler():
    """Setup APScheduler to keep bot alive"""
    global scheduler, telegram_event_loop
    
    if telegram_event_loop is None:
        logger.warning("Keep-alive scheduler: Telegram event loop not ready, will retry later")
        return
    
    try:
        # Create scheduler with the telegram event loop
        scheduler = AsyncIOScheduler(event_loop=telegram_event_loop)
        
        # Add job to call bot.get_me() every 5 minutes
        scheduler.add_job(
            single_keep_alive,
            trigger=IntervalTrigger(minutes=5),
            id='keep_alive',
            name='Keep bot alive',
            replace_existing=True
        )
        
        # Add job for automatic user_data_store cleanup
        scheduler.add_job(
            async_cleanup_user_data_store,
            trigger=IntervalTrigger(minutes=CLEANUP_INTERVAL_MINUTES),
            id='cleanup_user_data_store',
            name='Cleanup user_data_store',
            replace_existing=True
        )
        
        # Add job for rate limit store cleanup every 5 minutes
        async def async_cleanup_rate_limit_store():
            """Async wrapper for cleanup_rate_limit_store"""
            try:
                cleanup_rate_limit_store()
            except Exception as e:
                logger.error(f"[RATE_LIMIT_CLEANUP] Error: {e}", exc_info=True)
        
        scheduler.add_job(
            async_cleanup_rate_limit_store,
            trigger=IntervalTrigger(minutes=5),
            id='cleanup_rate_limit_store',
            name='Cleanup rate_limit_store',
            replace_existing=True
        )
        
        scheduler.start()
        logger.info("Keep-alive scheduler started: bot.get_me() will be called every 5 minutes")
        logger.info(f"Automatic cleanup scheduler started: user_data_store cleanup every {CLEANUP_INTERVAL_MINUTES} minutes")
        logger.info("Rate limit store cleanup scheduler started: cleanup every 5 minutes")
    except Exception as e:
        logger.error(f"Failed to setup keep-alive scheduler: {e}", exc_info=True)

if __name__ == '__main__':
    # Validate environment variables
    missing_vars = []
    
    if not TELEGRAM_BOT_TOKEN:
        missing_vars.append("TELEGRAM_BOT_TOKEN")
    
    if not WEBHOOK_URL:
        missing_vars.append("WEBHOOK_URL")
    
    if not SUPABASE_URL:
        missing_vars.append("SUPABASE_URL")
    
    if not SUPABASE_KEY:
        missing_vars.append("SUPABASE_KEY")
    
    if not PIN_CODE:
        missing_vars.append("PIN_CODE")
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.error("Please set all required environment variables in Koyeb dashboard")
        exit(1)
    
    # Note: Supabase client will be initialized lazily on first use
    
    # Initialize Telegram app for local dev
    initialize_telegram_app()

    # Give it a moment to initialize
    import time
    time.sleep(2)
    
    # For production, gunicorn will be used (see Procfile)
    # This code is kept for local development
    logger.warning("For production, use: gunicorn -w 1 -b 0.0.0.0:$PORT main:app")
    app.run(host='0.0.0.0', port=PORT, debug=False)
