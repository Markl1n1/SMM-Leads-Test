import asyncio
import time
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import RATE_LIMIT_ENABLED, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW
from bot.logging import logger


# Store user data during conversation - isolated per user_id for concurrent access
user_data_store = {}
user_data_store_access_time = {}
USER_DATA_STORE_TTL = 3600
USER_DATA_STORE_MAX_SIZE = 1000


rate_limit_store = {}


def clear_all_conversation_state(context: ContextTypes.DEFAULT_TYPE, user_id: int = None):
    """Clear all conversation state including internal ConversationHandler keys (_conversation_*)"""
    if context.user_data:
        keys_to_remove = [
            'current_field', 'current_state', 'add_step', 'editing_lead_id',
            'last_check_messages', 'add_message_ids', 'check_by', 'check_value',
            'check_results', 'selected_lead_id', 'original_lead_data',
            'pin_attempts', 'tag_manager_name', 'tag_new_tag'
        ]
        for key in keys_to_remove:
            if key in context.user_data:
                del context.user_data[key]

        conversation_keys = [key for key in context.user_data.keys() if key.startswith('_conversation_')]
        for key in conversation_keys:
            del context.user_data[key]
            logger.debug(f"Cleared ConversationHandler internal key: {key}")

        context.user_data.clear()
        logger.info(f"Cleared all conversation state for user {user_id if user_id else 'unknown'}")

    if user_id:
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]


def log_conversation_state(user_id: int, context: ContextTypes.DEFAULT_TYPE, prefix: str = "[STATE]") -> None:
    """Log current conversation-related state for diagnostics."""
    try:
        user_keys = list(context.user_data.keys()) if context.user_data else []
        conversation_keys = [key for key in user_keys if key.startswith("_conversation_")]
        in_user_store = user_id in user_data_store
        logger.info(
            f"{prefix} user_id={user_id}, "
            f"user_keys={user_keys}, "
            f"conversation_keys={conversation_keys}, "
            f"in_user_data_store={in_user_store}"
        )
    except Exception as e:
        logger.error(f"{prefix} Failed to log conversation state for user_id={user_id}: {e}", exc_info=True)


async def cleanup_check_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean up old check messages when starting a new check"""
    if 'last_check_messages' in context.user_data and context.user_data['last_check_messages']:
        chat_id = update.effective_chat.id
        bot = context.bot
        message_ids = context.user_data['last_check_messages']

        for msg_id in message_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

        context.user_data['last_check_messages'] = []


async def save_check_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Save message ID for later cleanup"""
    if 'last_check_messages' not in context.user_data:
        context.user_data['last_check_messages'] = []
    context.user_data['last_check_messages'].append(message_id)


async def cleanup_add_messages(update: Update, context: ContextTypes.DEFAULT_TYPE, exclude_message_id: int = None):
    """Clean up all add flow messages except the final success message and optionally exclude a specific message"""
    if 'add_message_ids' in context.user_data and context.user_data['add_message_ids']:
        chat_id = update.effective_chat.id
        bot = context.bot
        message_ids = context.user_data['add_message_ids'].copy()

        for msg_id in message_ids:
            if exclude_message_id and msg_id == exclude_message_id:
                continue
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

        context.user_data['add_message_ids'] = []


async def save_add_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Save message ID for later cleanup after successful add"""
    if 'add_message_ids' not in context.user_data:
        context.user_data['add_message_ids'] = []
    context.user_data['add_message_ids'].append(message_id)


async def cleanup_all_messages_before_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, exclude_message_id: int = None):
    """Clean up all intermediate bot messages before showing main menu"""
    chat_id = update.effective_chat.id
    bot = context.bot

    message_ids_to_delete = []

    if 'add_message_ids' in context.user_data and context.user_data['add_message_ids']:
        message_ids_to_delete.extend(context.user_data['add_message_ids'])
        context.user_data['add_message_ids'] = []

    if 'last_check_messages' in context.user_data and context.user_data['last_check_messages']:
        message_ids_to_delete.extend(context.user_data['last_check_messages'])
        context.user_data['last_check_messages'] = []

    if exclude_message_id:
        while exclude_message_id in message_ids_to_delete:
            message_ids_to_delete.remove(exclude_message_id)

    if message_ids_to_delete:
        delete_tasks = []
        for msg_id in message_ids_to_delete:
            delete_tasks.append(
                bot.delete_message(chat_id=chat_id, message_id=msg_id)
            )
        try:
            await asyncio.gather(*delete_tasks, return_exceptions=True)
        except Exception:
            pass


def cleanup_user_data_store(exclude_user_id: int = None):
    """Clean up old entries from user_data_store to optimize memory."""
    current_time = time.time()
    users_to_remove = []

    for user_id, access_time in user_data_store_access_time.items():
        if user_id == exclude_user_id:
            continue
        if current_time - access_time > USER_DATA_STORE_TTL:
            users_to_remove.append(user_id)

    for user_id in users_to_remove:
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]

    if len(user_data_store) > USER_DATA_STORE_MAX_SIZE:
        sorted_users = sorted(user_data_store_access_time.items(), key=lambda x: x[1])
        users_to_remove = []
        for user_id, _ in sorted_users:
            if user_id == exclude_user_id:
                continue
            users_to_remove.append(user_id)
            if len(user_data_store) - len(users_to_remove) <= USER_DATA_STORE_MAX_SIZE:
                break
        for user_id in users_to_remove:
            if user_id in user_data_store:
                del user_data_store[user_id]
            if user_id in user_data_store_access_time:
                del user_data_store_access_time[user_id]


async def async_cleanup_user_data_store():
    """Async wrapper for cleanup_user_data_store to be used in scheduler"""
    try:
        cleanup_user_data_store()
        cleaned_count = len(user_data_store)
        logger.info(f"[CLEANUP] Automatic cleanup completed. Current user_data_store size: {cleaned_count}")
    except Exception as e:
        logger.error(f"[CLEANUP] Error during automatic cleanup: {e}", exc_info=True)


def cleanup_rate_limit_store():
    """Clean up old entries from rate_limit_store to prevent memory leaks"""
    if not RATE_LIMIT_ENABLED:
        return
    current_time = time.time()
    window_start = current_time - RATE_LIMIT_WINDOW
    users_to_remove = []
    for user_id, timestamps in rate_limit_store.items():
        rate_limit_store[user_id] = [ts for ts in timestamps if ts > window_start]
        if not rate_limit_store[user_id]:
            users_to_remove.append(user_id)
    for user_id in users_to_remove:
        del rate_limit_store[user_id]


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """Check if user has exceeded rate limit."""
    if not RATE_LIMIT_ENABLED:
        return True, RATE_LIMIT_REQUESTS

    current_time = time.time()
    window_start = current_time - RATE_LIMIT_WINDOW

    if user_id not in rate_limit_store:
        rate_limit_store[user_id] = []

    rate_limit_store[user_id] = [
        ts for ts in rate_limit_store[user_id]
        if ts > window_start
    ]

    if len(rate_limit_store[user_id]) >= RATE_LIMIT_REQUESTS:
        oldest_request = min(rate_limit_store[user_id])
        wait_seconds = int(RATE_LIMIT_WINDOW - (current_time - oldest_request)) + 1
        return False, wait_seconds

    rate_limit_store[user_id].append(current_time)
    remaining = RATE_LIMIT_REQUESTS - len(rate_limit_store[user_id])
    return True, remaining


def rate_limit_handler(func):
    """Decorator to add rate limiting to handler functions"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return await func(update, context)

        user_id = update.effective_user.id
        is_allowed, remaining_or_wait = check_rate_limit(user_id)

        if not is_allowed:
            wait_seconds = remaining_or_wait
            logger.warning(f"[RATE_LIMIT] User {user_id} exceeded rate limit. Wait {wait_seconds}s")
            try:
                if update.message:
                    await update.message.reply_text(
                        f"⚠️ <b>Превышен лимит запросов</b>\n\n"
                        f"Пожалуйста, подождите {wait_seconds} секунд перед следующим запросом.\n\n"
                        f"Это защита от злоупотреблений.",
                        parse_mode='HTML'
                    )
                elif update.callback_query:
                    await update.callback_query.answer(
                        text=f"⚠️ Превышен лимит. Подождите {wait_seconds} секунд."
                    )
            except Exception as e:
                logger.warning(f"[RATE_LIMIT] Failed to send rate limit message: {e}")
            return ConversationHandler.END

        return await func(update, context)

    return wrapper

