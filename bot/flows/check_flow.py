import csv
import io
import re
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from bot.config import TABLE_NAME, is_facebook_flow_enabled
from bot.constants import (
    CHECK_BY_TELEGRAM,
    CHECK_BY_FB_LINK,
    CHECK_BY_TELEGRAM_ID,
    CHECK_BY_FULLNAME,
    SMART_CHECK_INPUT,
    ADD_FULLNAME,
    ADD_FB_LINK,
    ADD_TELEGRAM_NAME,
    ADD_TELEGRAM_ID,
    ADD_REVIEW,
)
from bot.keyboards import get_main_menu_keyboard, get_check_back_keyboard
from bot.logging import logger
from bot.services.photos import download_photo_from_supabase
from bot.services.supabase_client import get_supabase_client
from bot.state import (
    clear_all_conversation_state,
    cleanup_check_messages,
    save_check_message,
    rate_limit_handler,
    user_data_store,
)
from bot.utils import (
    detect_search_type,
    escape_html,
    format_facebook_link_for_display,
    get_user_friendly_error,
    normalize_telegram_id,
    retry_telegram_api,
    validate_facebook_link,
    validate_telegram_name,
)

TELEGRAM_MESSAGE_CHAR_LIMIT = 3800


def _make_results_csv_bytes(results: list[dict], field_labels: dict) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([field_labels[key] for key in field_labels.keys()])

    for result in results:
        row = []
        for field_name_key in field_labels.keys():
            value = result.get(field_name_key)
            if value is None:
                row.append("")
                continue
            if field_name_key == 'created_at':
                try:
                    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                    value = dt.strftime('%d.%m.%Y %H:%M')
                except Exception:
                    pass
            elif field_name_key == 'facebook_link':
                value = format_facebook_link_for_display(value)
            elif field_name_key == 'manager_tag':
                tag_value = str(value).strip()
                value = f"@{tag_value}" if tag_value else ""
            row.append(str(value))

        writer.writerow(row)

    return output.getvalue().encode("utf-8-sig")


def _make_results_csv_filename(search_value: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9_-]+', '_', search_value.strip().lower()).strip('_')
    if not safe:
        safe = "results"
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    return f"agent-{safe}-{date_str}.csv"


async def _send_results_as_csv(update: Update, results: list[dict], field_labels: dict, search_value: str):
    csv_bytes = _make_results_csv_bytes(results, field_labels)
    filename = _make_results_csv_filename(search_value)
    file_obj = io.BytesIO(csv_bytes)
    file_obj.name = filename
    caption = "⚠️ Результатов слишком много для сообщения, отправляю файлом."
    return await update.message.reply_document(
        document=file_obj,
        filename=filename,
        caption=caption
    )


async def send_lead_with_photo(update: Update, result: dict, idx: int, total: int, reply_markup: InlineKeyboardMarkup) -> bool:
    """
    Send a single lead with photo as a separate message.
    Returns True if sent successfully, False otherwise.
    """
    field_labels = {
        'fullname': 'Клиент',
        'facebook_link': 'Facebook Ссылка',
        'telegram_user': 'Тег Telegram',
        'telegram_id': 'Telegram ID',
        'manager_name': 'Агент',
        'manager_tag': 'Тег Агента',
        'photo_url': 'Фото',
        'created_at': 'Дата'
    }

    message_parts = [f"✅ <b>Клиент {idx}</b>", ""]

    for field_name_key, field_label in field_labels.items():
        value = result.get(field_name_key)

        if value is None or value == '' or value == 'Не указано':
            continue

        if field_name_key == 'photo_url':
            continue

        if field_name_key == 'created_at':
            try:
                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                value = dt.strftime('%d.%m.%Y %H:%M')
            except Exception:
                pass

        if field_name_key == 'facebook_link':
            value = format_facebook_link_for_display(value)

        if field_name_key == 'manager_tag':
            tag_value = str(value).strip()
            message_parts.append(f"{field_label}: @{tag_value}")
        else:
            escaped_value = escape_html(str(value))
            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

    message = "\n".join(message_parts)

    photo_url = result.get('photo_url')
    if not photo_url:
        logger.warning(f"[SEND_LEAD_PHOTO] No photo_url for lead {result.get('id')}")
        return False

    photo_url = str(photo_url).strip()

    try:
        photo_bytes = await download_photo_from_supabase(photo_url)
        if photo_bytes:
            photo_file = io.BytesIO(photo_bytes)
            await update.message.reply_photo(
                photo=photo_file,
                caption=message,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            return True
        await update.message.reply_text(
            message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return True
    except Exception as e:
        logger.error(f"[SEND_LEAD_PHOTO] Error sending photo: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            return True
        except Exception as e2:
            logger.error(f"[SEND_LEAD_PHOTO] Error sending fallback message: {e2}", exc_info=True)
            return False


@rate_limit_handler
async def check_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle check_menu callback - start smart check input"""
    query = update.callback_query
    await retry_telegram_api(query.answer)

    user_id = query.from_user.id
    logger.info(f"[SMART_CHECK] Starting smart check for user {user_id}")

    clear_all_conversation_state(context, user_id)
    if context.user_data:
        keys_to_remove = [key for key in context.user_data.keys() if key.startswith('_conversation_')]
        for key in keys_to_remove:
            del context.user_data[key]

    for key in ['pin_attempts', 'tag_manager_name', 'tag_new_tag', 'editing_lead_id']:
        context.user_data.pop(key, None)

    await cleanup_check_messages(update, context)

    try:
        await retry_telegram_api(
            query.edit_message_text,
            text="✅ Введите данные для поиска:\n\n"
                 "Бот автоматически определит тип данных.\n\n"
                 "💡 Можно ввести:\n"
                 "• Facebook ссылку\n"
                 "• Telegram username (минимум 5 символов)\n"
                 "• Telegram ID (минимум 5 цифр)\n"
                 "• Имя клиента (минимум 3 символа)",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        logger.warning(f"Could not edit message in check_menu_callback: {e}")
        if query.message:
            await retry_telegram_api(
                query.message.reply_text,
                text="✅ Введите данные для поиска:\n\n"
                     "Можно ввести:\n"
                     "• Telegram username\n"
                     "• Telegram ID\n"
                     "• Имя клиента",
                reply_markup=get_check_back_keyboard()
            )
        else:
            logger.error("check_menu_callback: query.message is None")
            return ConversationHandler.END

    context.user_data['current_state'] = SMART_CHECK_INPUT
    return SMART_CHECK_INPUT


async def check_telegram_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for check by telegram conversation"""
    query = update.callback_query
    if not query:
        logger.error("check_telegram_callback: query is None")
        return ConversationHandler.END

    await query.answer()

    user_id = query.from_user.id
    logger.info(f"[CHECK_TELEGRAM] Clearing state before entry for user {user_id}")

    clear_all_conversation_state(context, user_id)
    await cleanup_check_messages(update, context)

    try:
        await query.edit_message_text(
            "📱 Введите Тег Telegram для проверки:",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        logger.warning(f"Could not edit message in check_telegram_callback: {e}")
        if query.message:
            await query.message.reply_text(
                "📱 Введите Тег Telegram для проверки:",
                reply_markup=get_check_back_keyboard()
            )
        else:
            logger.error("check_telegram_callback: query.message is None")
            return ConversationHandler.END

    return CHECK_BY_TELEGRAM


async def check_fb_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for check by facebook link conversation"""
    query = update.callback_query
    if not query:
        logger.error("check_fb_link_callback: query is None")
        return ConversationHandler.END

    await retry_telegram_api(query.answer)

    user_id = query.from_user.id
    logger.info(f"[CHECK_FB_LINK] Clearing state before entry for user {user_id}")

    clear_all_conversation_state(context, user_id)
    await cleanup_check_messages(update, context)

    try:
        await retry_telegram_api(
            query.edit_message_text,
            text="🔗 Введите Facebook Ссылка для проверки:",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        logger.warning(f"Could not edit message in check_fb_link_callback: {e}")
        if query.message:
            await retry_telegram_api(
                query.message.reply_text,
                text="🔗 Введите Facebook Ссылка для проверки:",
                reply_markup=get_check_back_keyboard()
            )
        else:
            logger.error("check_fb_link_callback: query.message is None")
            return ConversationHandler.END

    return CHECK_BY_FB_LINK


async def check_fullname_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for check by fullname conversation"""
    query = update.callback_query
    if not query:
        logger.error("check_fullname_callback: query is None")
        return ConversationHandler.END

    await retry_telegram_api(query.answer)

    user_id = query.from_user.id
    logger.info(f"[CHECK_FULLNAME] Clearing state before entry for user {user_id}")

    clear_all_conversation_state(context, user_id)
    await cleanup_check_messages(update, context)

    try:
        await retry_telegram_api(
            query.edit_message_text,
            text="👤 <b>Введите имя клиента для поиска:</b>\n\n"
                 "💡 <b>Требования:</b>\n"
                 "• Минимум 3 символа\n"
                 "• Можно ввести имя, фамилию или часть имени\n\n"
                 "Бот будет искать по всем полям (имя, Telegram, Facebook).",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        logger.warning(f"Could not edit message in check_fullname_callback: {e}")
        if query.message:
            await retry_telegram_api(
                query.message.reply_text,
                text="👤 <b>Введите имя клиента для поиска:</b>\n\n"
                     "💡 <b>Требования:</b>\n"
                     "• Минимум 3 символа\n"
                     "• Можно ввести имя, фамилию или часть имени\n\n"
                     "Бот будет искать по всем полям (имя, Telegram, Facebook).",
                reply_markup=get_check_back_keyboard()
            )
        else:
            logger.error("check_fullname_callback: query.message is None")
            return ConversationHandler.END

    return CHECK_BY_FULLNAME


async def check_telegram_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for check by telegram ID conversation"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    logger.info(f"[CHECK_TELEGRAM_ID] Clearing state before entry for user {user_id}")

    clear_all_conversation_state(context, user_id)
    await cleanup_check_messages(update, context)

    try:
        await query.edit_message_text(
            "🆔 Введите Telegram ID для проверки:",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        logger.warning(f"Could not edit message in check_telegram_id_callback: {e}")
        await query.message.reply_text(
            "🆔 Введите Telegram ID для проверки:",
            reply_markup=get_check_back_keyboard()
        )

    return CHECK_BY_TELEGRAM_ID


async def check_telegram_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle telegram username input for checking"""
    if not update.message:
        logger.error(f"[CHECK_TELEGRAM_INPUT] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        return ConversationHandler.END
    return await check_by_field(update, context, "telegram_user", "Тег Telegram", CHECK_BY_TELEGRAM)


async def check_fb_link_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Facebook link input for checking"""
    if not update.message:
        logger.error(f"[CHECK_FB_LINK_INPUT] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        return ConversationHandler.END
    return await check_by_field(update, context, "facebook_link", "Facebook Ссылка", CHECK_BY_FB_LINK)


async def check_telegram_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram ID input for checking"""
    if not update.message:
        logger.error(f"[CHECK_TELEGRAM_ID_INPUT] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        return ConversationHandler.END
    return await check_by_field(update, context, "telegram_id", "Telegram ID", CHECK_BY_TELEGRAM_ID)


async def check_fullname_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle fullname input for checking"""
    if not update.message:
        logger.error(f"[CHECK_FULLNAME_INPUT] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        return ConversationHandler.END
    return await check_by_fullname(update, context)


@rate_limit_handler
async def smart_check_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart check input handler - automatically detects type and searches accordingly"""
    if not update.message or not update.message.text:
        logger.error(f"[SMART_CHECK] update.message or update.message.text is None")
        return ConversationHandler.END

    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')

    add_states = {ADD_FULLNAME, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    if is_facebook_flow_enabled():
        add_states.add(ADD_FB_LINK)
    if user_id in user_data_store and current_state in add_states:
        logger.info(f"[SMART_CHECK] User {user_id} is in ADD flow (state={current_state}), not processing as check")
        return None

    search_value = update.message.text.strip()

    if not search_value:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
        sent_message = await update.message.reply_text(
            "❌ <b>Ошибка:</b> Значение для поиска не может быть пустым.\n\n"
            "💡 Введите данные для поиска:\n"
            "• Facebook ссылку\n"
            "• Telegram username (минимум 5 символов)\n"
            "• Telegram ID (минимум 5 цифр)\n"
            "• Имя клиента (минимум 3 символа)",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        await save_check_message(update, context, sent_message.message_id)
        return ConversationHandler.END

    field_type, normalized_value = detect_search_type(search_value)

    logger.info(f"[SMART_CHECK] Detected type: '{field_type}' for value: '{search_value}' (normalized: '{normalized_value}')")

    value_lower = search_value.lower()
    has_url_patterns = (
        'facebook.com' in value_lower or
        'http://' in value_lower or
        'https://' in value_lower or
        'www.' in value_lower
    )

    is_ambiguous = False
    if field_type == 'facebook_link' and not has_url_patterns:
        username_candidate = search_value.replace('@', '').strip()
        if username_candidate and not ' ' in username_candidate:
            if len(username_candidate) >= 5 and all(c.isalnum() or c in ['_', '.', '-'] for c in username_candidate):
                is_valid_tg, _, _ = validate_telegram_name(username_candidate)
                if is_valid_tg:
                    is_ambiguous = True
                    logger.info(f"[SMART_CHECK] Ambiguous value: could be both telegram_user and facebook_link, using multi-field search")

    if field_type == 'facebook_link' and is_ambiguous:
        return await check_by_multiple_fields(update, context, search_value)
    if field_type == 'facebook_link':
        return await check_by_field(update, context, "facebook_link", "Facebook Ссылка", SMART_CHECK_INPUT)
    if field_type == 'telegram_id':
        return await check_by_field(update, context, "telegram_id", "Telegram ID", SMART_CHECK_INPUT)
    if field_type == 'telegram_user':
        return await check_by_field(update, context, "telegram_user", "Тег Telegram", SMART_CHECK_INPUT)
    if field_type == 'fullname':
        return await check_by_fullname(update, context)

    logger.info(f"[SMART_CHECK] Type unknown, searching across multiple fields")
    return await check_by_multiple_fields(update, context, search_value)


async def check_by_multiple_fields(update: Update, context: ContextTypes.DEFAULT_TYPE, search_value: str):
    """
    Search across multiple fields simultaneously using OR conditions.
    Searches in: telegram_user, telegram_id, fullname, facebook_link
    """
    if not update.message:
        logger.error(f"[MULTI_FIELD_SEARCH] update.message is None")
        return ConversationHandler.END

    logger.info(f"[MULTI_FIELD_SEARCH] Starting multi-field search with value: '{search_value}'")

    client = get_supabase_client()
    if not client:
        error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        return ConversationHandler.END

    try:
        normalized_tg_user = None
        normalized_tg_id = None
        normalized_fullname = None
        normalized_facebook_link = None

        value_lower = search_value.lower()
        has_url_patterns = (
            'facebook.com' in value_lower or
            'http://' in value_lower or
            'https://' in value_lower or
            'www.' in value_lower
        )

        is_valid_tg_user, _, tg_user_normalized = validate_telegram_name(search_value)
        if is_valid_tg_user:
            normalized_tg_user = tg_user_normalized
            logger.info(f"[MULTI_FIELD_SEARCH] Value can be normalized as telegram_user: '{tg_user_normalized}'")

        is_valid_fb, _, fb_normalized = validate_facebook_link(search_value)
        if is_valid_fb:
            normalized_facebook_link = fb_normalized
            logger.info(f"[MULTI_FIELD_SEARCH] Value can be normalized as facebook_link: '{fb_normalized}'")

            if has_url_patterns:
                logger.info(f"[MULTI_FIELD_SEARCH] Facebook URL detected, will also search telegram_user with original value")

        if is_valid_tg_user and is_valid_fb and not has_url_patterns:
            logger.info(f"[MULTI_FIELD_SEARCH] Ambiguous value: valid as both telegram_user and facebook_link (without URL), will search in both columns")

        if search_value.isdigit():
            digit_length = len(search_value)
            if digit_length == 10:
                normalized_tg_id = normalize_telegram_id(search_value)
            elif 5 <= digit_length < 10:
                normalized_tg_id = normalize_telegram_id(search_value)

        if len(search_value.strip()) >= 3:
            normalized_fullname = re.sub(r'\s+', ' ', search_value.strip())
            normalized_fullname = normalized_fullname.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

        all_results = []
        seen_ids = set()

        if normalized_tg_user:
            try:
                logger.info(f"[MULTI_FIELD_SEARCH] Searching telegram_user with normalized value: '{normalized_tg_user}'")
                response = client.table(TABLE_NAME).select("*").eq("telegram_user", normalized_tg_user).limit(50).execute()
                if response.data:
                    logger.info(f"[MULTI_FIELD_SEARCH] Found {len(response.data)} results in telegram_user with normalized value")
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching telegram_user: {e}")

        if is_valid_fb and has_url_patterns:
            try:
                logger.info(f"[MULTI_FIELD_SEARCH] Searching telegram_user with original value (Facebook URL): '{search_value}'")
                response = client.table(TABLE_NAME).select("*").eq("telegram_user", search_value).limit(50).execute()
                if response.data:
                    logger.info(f"[MULTI_FIELD_SEARCH] Found {len(response.data)} results in telegram_user with original value")
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching telegram_user with original value: {e}")

        if normalized_tg_id:
            try:
                response = client.table(TABLE_NAME).select("*").eq("telegram_id", normalized_tg_id).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching telegram_id: {e}")

        if normalized_fullname:
            try:
                pattern = f"%{normalized_fullname}%"
                response = client.table(TABLE_NAME).select("*").ilike("fullname", pattern).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching fullname: {e}")

        if normalized_fullname:
            try:
                pattern = f"%{normalized_fullname}%"
                response = client.table(TABLE_NAME).select("*").ilike("manager_name", pattern).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching manager_name: {e}")

        if normalized_facebook_link:
            try:
                response = client.table(TABLE_NAME).select("*").eq("facebook_link", normalized_facebook_link).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
                    logger.info(f"[MULTI_FIELD_SEARCH] Found {len(response.data)} results in facebook_link for '{normalized_facebook_link}'")
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching facebook_link: {e}")

        all_results = all_results[:50]

        field_labels = {
            'fullname': 'Клиент',
            'facebook_link': 'Facebook Ссылка',
            'telegram_user': 'Тег Telegram',
            'telegram_id': 'Telegram ID',
            'manager_name': 'Агент',
            'manager_tag': 'Тег Агента',
            'photo_url': 'Фото',
            'created_at': 'Дата'
        }

        if all_results:
            if len(all_results) == 1:
                result = all_results[0]
                photo_url = result.get('photo_url')

                message_parts = [f"✅ <b>Найдено клиентов: 1</b>", ""]

                for field_name_key, field_label in field_labels.items():
                    value = result.get(field_name_key)

                    if value is None or value == '' or value == 'Не указано':
                        continue

                    if field_name_key == 'photo_url':
                        continue

                    if field_name_key == 'created_at':
                        try:
                            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                            value = dt.strftime('%d.%m.%Y %H:%M')
                        except Exception:
                            pass

                    if field_name_key == 'facebook_link':
                        value = format_facebook_link_for_display(value)

                    if field_name_key == 'manager_tag':
                        tag_value = str(value).strip()
                        message_parts.append(f"{field_label}: @{tag_value}")
                    else:
                        escaped_value = escape_html(str(value))
                        message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

                message = "\n".join(message_parts)

                keyboard = []
                lead_id = result.get('id')
                if lead_id is not None:
                    keyboard.append([InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_lead_{lead_id}")])
                keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
                reply_markup = InlineKeyboardMarkup(keyboard)

                if photo_url and str(photo_url).strip():
                    try:
                        photo_bytes = await download_photo_from_supabase(str(photo_url).strip())
                        if photo_bytes:
                            photo_file = io.BytesIO(photo_bytes)
                            sent_message = await update.message.reply_photo(
                                photo=photo_file,
                                caption=message,
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                        else:
                            sent_message = await update.message.reply_text(
                                message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                    except Exception as e:
                        logger.error(f"[MULTI_FIELD_SEARCH] Error sending photo: {e}", exc_info=True)
                        sent_message = await update.message.reply_text(
                            message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                else:
                    sent_message = await update.message.reply_text(
                        message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                await save_check_message(update, context, sent_message.message_id)
            else:
                message_parts = [f"✅ <b>Найдено клиентов: {len(all_results)}</b>\n"]

                for idx, result in enumerate(all_results, 1):
                    if idx > 1:
                        message_parts.append("")
                    message_parts.append(f"<b>━━━ Клиент {idx} ━━━</b>")
                    for field_name_key, field_label in field_labels.items():
                        value = result.get(field_name_key)

                        if value is None or value == '' or value == 'Не указано':
                            continue

                        if field_name_key == 'photo_url':
                            continue

                        if field_name_key == 'created_at':
                            try:
                                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                                value = dt.strftime('%d.%m.%Y %H:%M')
                            except Exception:
                                pass

                        if field_name_key == 'facebook_link':
                            value = format_facebook_link_for_display(value)

                        if field_name_key == 'manager_tag':
                            tag_value = str(value).strip()
                            message_parts.append(f"{field_label}: @{tag_value}")
                        else:
                            escaped_value = escape_html(str(value))
                            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

                message = "\n".join(message_parts)

                keyboard = [
                    [InlineKeyboardButton("✅ Добавить лид", callback_data="add_new")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                if len(message) > TELEGRAM_MESSAGE_CHAR_LIMIT:
                    sent_message = await _send_results_as_csv(
                        update,
                        all_results,
                        field_labels,
                        search_value
                    )
                else:
                    try:
                        sent_message = await update.message.reply_text(
                            message,
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                    except BadRequest as e:
                        if "Message is too long" in str(e):
                            sent_message = await _send_results_as_csv(
                                update,
                                all_results,
                                field_labels,
                                search_value
                            )
                        else:
                            raise

                await save_check_message(update, context, sent_message.message_id)
        else:
            message = (
                "❌ <b>Клиенты не найдены</b>\n\n"
                "💡 Возможные причины:\n"
                "• Данные введены с ошибкой\n"
                "• Клиент ещё не добавлен в базу\n"
                "• Неполные данные\n\n"
                "Что можно сделать:\n"
                "• Проверить правильность введенных данных\n"
                "• Использовать другой способ поиска\n"
                "• Убедиться, что данные введены полностью"
            )
            reply_markup = get_main_menu_keyboard()
            sent_message = await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            await save_check_message(update, context, sent_message.message_id)

    except Exception as e:
        logger.error(f"[MULTI_FIELD_SEARCH] ❌ Error in multi-field search: {e}", exc_info=True)
        error_msg = get_user_friendly_error(e, "проверке")
        sent_message = await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        await save_check_message(update, context, sent_message.message_id)

    return ConversationHandler.END


async def check_by_field(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str, field_label: str, current_state: int):
    """Universal function to check by any field"""
    if not update.message:
        logger.error(f"[CHECK_BY_FIELD] update.message is None for field '{field_name}'. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        logger.error(f"[CHECK_BY_FIELD] Context keys: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        user_id = update.effective_user.id if update.effective_user else None
        if user_id:
            logger.error(f"[CHECK_BY_FIELD] User ID: {user_id}")
        return ConversationHandler.END

    if not update.message.text:
        logger.error(f"[CHECK_BY_FIELD] update.message.text is None for field '{field_name}'")
        return ConversationHandler.END

    search_value = update.message.text.strip()

    if not search_value:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
        sent_message = await update.message.reply_text(
            f"❌ {field_label} не может быть пустым.",
            reply_markup=keyboard
        )
        await save_check_message(update, context, sent_message.message_id)
        return ConversationHandler.END

    FIELD_NAME_MAPPING = {
        'telegram_name': 'telegram_user',
    }

    db_field_name = FIELD_NAME_MAPPING.get(field_name, field_name)

    search_type_map = {
        'fullname': 'FULLNAME SEARCH',
        'facebook_link': 'FACEBOOK SEARCH',
        'telegram_user': 'TELEGRAM USER SEARCH',
        'telegram_id': 'TELEGRAM ID SEARCH'
    }
    search_type = search_type_map.get(field_name, f'{field_name.upper()} SEARCH')
    logger.info(f"[{search_type}] Starting search with value: '{search_value}' (length: {len(search_value)}, type: {type(search_value)})")

    if field_name == "facebook_link":
        is_valid, error_msg, normalized = validate_facebook_link(search_value)
        if not is_valid:
            logger.warning(f"[{search_type}] ❌ Validation failed: {error_msg}")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            await save_check_message(update, context, sent_message.message_id)
            return ConversationHandler.END
        search_value = normalized
        logger.info(f"[{search_type}] Normalized Facebook link: '{normalized}'")

    elif field_name == "telegram_user":
        is_fb_url, _, fb_normalized = validate_facebook_link(search_value)
        response_data = None

        if is_fb_url:
            client = get_supabase_client()
            if not client:
                error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
                await update.message.reply_text(
                    error_msg,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
                return ConversationHandler.END

            all_results = []
            seen_ids = set()

            logger.info(f"[{search_type}] Searching telegram_user with original value: '{search_value}'")
            try:
                response = client.table(TABLE_NAME).select("*").eq("telegram_user", search_value).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
                    logger.info(f"[{search_type}] Found {len(response.data)} results in telegram_user with original value")
            except Exception as e:
                logger.warning(f"[{search_type}] Error searching telegram_user with original value: {e}")

            logger.info(f"[{search_type}] Searching facebook_link with normalized value: '{fb_normalized}'")
            try:
                response = client.table(TABLE_NAME).select("*").eq("facebook_link", fb_normalized).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
                    logger.info(f"[{search_type}] Found {len(response.data)} results in facebook_link with normalized value")
            except Exception as e:
                logger.warning(f"[{search_type}] Error searching facebook_link with normalized value: {e}")

            response_data = all_results
            logger.info(f"[{search_type}] Total unique results: {len(response_data)}")
        else:
            is_valid, error_msg, normalized = validate_telegram_name(search_value)
            if not is_valid:
                logger.warning(f"[{search_type}] ❌ Validation failed: {error_msg}")
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
                sent_message = await update.message.reply_text(
                    f"❌ {error_msg}",
                    reply_markup=keyboard
                )
                await save_check_message(update, context, sent_message.message_id)
                return ConversationHandler.END
            search_value = normalized
            logger.info(f"[{search_type}] Normalized Telegram username: '{normalized}'")

            client = get_supabase_client()
            if not client:
                error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
                await update.message.reply_text(
                    error_msg,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
                return ConversationHandler.END

            logger.info(f"[{search_type}] Executing query: SELECT * FROM {TABLE_NAME} WHERE {db_field_name} = '{search_value}' LIMIT 50")
            response = client.table(TABLE_NAME).select("*").eq(db_field_name, search_value).limit(50).execute()
            logger.info(f"[{search_type}] Query executed. Response type: {type(response)}, has data: {hasattr(response, 'data')}")
            logger.info(f"[{search_type}] Response.data length: {len(response.data) if hasattr(response, 'data') and response.data else 0}")
            response_data = response.data if response.data else []

    if field_name != "telegram_user":
        client = get_supabase_client()
        if not client:
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            await update.message.reply_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END

    try:
        if field_name != "telegram_user":
            logger.info(f"[{search_type}] Executing query: SELECT * FROM {TABLE_NAME} WHERE {db_field_name} = '{search_value}' LIMIT 50")
            response = client.table(TABLE_NAME).select("*").eq(db_field_name, search_value).limit(50).execute()
            logger.info(f"[{search_type}] Query executed. Response type: {type(response)}, has data: {hasattr(response, 'data')}")
            logger.info(f"[{search_type}] Response.data length: {len(response.data) if hasattr(response, 'data') and response.data else 0}")
            response_data = response.data if response.data else []
        elif field_name == "telegram_user" and not is_fb_url:
            pass

        field_labels = {
            'fullname': 'Клиент',
            'facebook_link': 'Facebook Ссылка',
            'telegram_user': 'Тег Telegram',
            'telegram_id': 'Telegram ID',
            'manager_name': 'Агент',
            'manager_tag': 'Тег Агента',
            'photo_url': 'Фото',
            'created_at': 'Дата'
        }

        if response_data and len(response_data) > 0:
            if len(response_data) == 1:
                result = response_data[0]
                photo_url = result.get('photo_url')

                message_parts = [f"✅ <b>Найдено клиентов: 1</b>", ""]

                for field_name_key, field_label in field_labels.items():
                    value = result.get(field_name_key)

                    if value is None or value == '' or value == 'Не указано':
                        continue

                    if field_name_key == 'photo_url':
                        continue

                    if field_name_key == 'created_at':
                        try:
                            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                            value = dt.strftime('%d.%m.%Y %H:%M')
                        except Exception:
                            pass

                    if field_name_key == 'facebook_link':
                        value = format_facebook_link_for_display(value)

                    if field_name_key == 'manager_tag':
                        tag_value = str(value).strip()
                        message_parts.append(f"{field_label}: @{tag_value}")
                    else:
                        escaped_value = escape_html(str(value))
                        message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

                message = "\n".join(message_parts)

                keyboard = []
                lead_id = result.get('id')
                if lead_id is not None:
                    keyboard.append([InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_lead_{lead_id}")])
                keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
                reply_markup = InlineKeyboardMarkup(keyboard)

                if photo_url and str(photo_url).strip():
                    try:
                        photo_bytes = await download_photo_from_supabase(str(photo_url).strip())
                        if photo_bytes:
                            photo_file = io.BytesIO(photo_bytes)
                            sent_message = await update.message.reply_photo(
                                photo=photo_file,
                                caption=message,
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                        else:
                            sent_message = await update.message.reply_text(
                                message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                    except Exception as e:
                        logger.error(f"[{search_type}] Error sending photo: {e}", exc_info=True)
                        sent_message = await update.message.reply_text(
                            message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                else:
                    sent_message = await update.message.reply_text(
                        message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                await save_check_message(update, context, sent_message.message_id)
            else:
                message_parts = [f"✅ <b>Найдено клиентов: {len(response_data)}</b>\n"]

                for idx, result in enumerate(response_data, 1):
                    if idx > 1:
                        message_parts.append("")
                    message_parts.append(f"<b>━━━ Клиент {idx} ━━━</b>")
                    for field_name_key, field_label in field_labels.items():
                        value = result.get(field_name_key)

                        if value is None or value == '' or value == 'Не указано':
                            continue

                        if field_name_key == 'photo_url':
                            continue

                        if field_name_key == 'created_at':
                            try:
                                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                                value = dt.strftime('%d.%m.%Y %H:%M')
                            except Exception:
                                pass

                        if field_name_key == 'facebook_link':
                            value = format_facebook_link_for_display(value)

                        if field_name_key == 'manager_tag':
                            tag_value = str(value).strip()
                            message_parts.append(f"{field_label}: @{tag_value}")
                        else:
                            escaped_value = escape_html(str(value))
                            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

                message = "\n".join(message_parts)

                keyboard = [
                    [InlineKeyboardButton("✅ Добавить лид", callback_data="add_new")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                sent_message = await update.message.reply_text(
                    message,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
                await save_check_message(update, context, sent_message.message_id)
        else:
            message = (
                "❌ <b>Клиенты не найдены</b>\n\n"
                "💡 Возможные причины:\n"
                "• Данные введены с ошибкой\n"
                "• Клиент ещё не добавлен в базу\n"
                "• Неполные данные\n\n"
                "Что можно сделать:\n"
                "• Проверить правильность введенных данных\n"
                "• Использовать другой способ поиска\n"
                "• Убедиться, что данные введены полностью"
            )
            reply_markup = get_main_menu_keyboard()
            sent_message = await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            await save_check_message(update, context, sent_message.message_id)

    except Exception as e:
        logger.error(f"[{search_type}] ❌ Error checking by {field_name}: {e}", exc_info=True)
        error_msg = get_user_friendly_error(e, "проверке")
        sent_message = await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        await save_check_message(update, context, sent_message.message_id)

    return ConversationHandler.END


async def check_by_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE, search_value_override: str | None = None):
    """Check by fullname using contains search with limit of 30 results"""
    if search_value_override is not None:
        search_value_raw = search_value_override
    else:
        if not update.message:
            logger.error(f"[FULLNAME SEARCH] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
            logger.error(f"[FULLNAME SEARCH] Context keys: {list(context.user_data.keys()) if context.user_data else 'empty'}")
            user_id = update.effective_user.id if update.effective_user else None
            if user_id:
                logger.error(f"[FULLNAME SEARCH] User ID: {user_id}")
            return ConversationHandler.END

        if not update.message.text:
            logger.error(f"[FULLNAME SEARCH] update.message.text is None")
            return ConversationHandler.END

        search_value_raw = update.message.text

    search_value = search_value_raw.strip()

    logger.info(f"[FULLNAME SEARCH] Starting search with value: '{search_value}' (length: {len(search_value)}, type: {type(search_value)})")

    if not search_value:
        await update.message.reply_text(
            "❌ <b>Ошибка:</b> Имя не может быть пустым.\n\n"
            "💡 Введите имя клиента для поиска (минимум 3 символа):",
            parse_mode='HTML',
            reply_markup=get_check_back_keyboard()
        )
        return CHECK_BY_FULLNAME

    if len(search_value) < 3:
        await update.message.reply_text(
            "❌ <b>Ошибка:</b> Для поиска по имени необходимо минимум 3 символа.\n\n"
            "💡 Введите имя клиента (минимум 3 символа):",
            parse_mode='HTML',
            reply_markup=get_check_back_keyboard()
        )
        return CHECK_BY_FULLNAME

    search_value = re.sub(r'\s+', ' ', search_value).strip()
    escaped_search_value = search_value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    logger.info(f"[FULLNAME SEARCH] Normalized search value: '{search_value}' -> escaped: '{escaped_search_value}'")

    client = get_supabase_client()
    if not client:
        logger.error("[FULLNAME SEARCH] Failed to get Supabase client")
        error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        return ConversationHandler.END

    try:
        normalized_tg_user = None
        normalized_tg_id = None
        normalized_fb_link = None
        normalized_fullname = escaped_search_value

        is_valid_tg_user, _, tg_user_normalized = validate_telegram_name(search_value)
        if is_valid_tg_user:
            normalized_tg_user = tg_user_normalized
            logger.info(f"[FULLNAME SEARCH] Value can also be normalized as telegram_user: '{normalized_tg_user}'")

        if search_value.isdigit() and len(search_value) >= 5:
            normalized_tg_id = normalize_telegram_id(search_value)
            if normalized_tg_id:
                logger.info(f"[FULLNAME SEARCH] Value can also be normalized as telegram_id: '{normalized_tg_id}'")

        is_valid_fb, _, fb_normalized = validate_facebook_link(search_value)
        if is_valid_fb:
            normalized_fb_link = fb_normalized
            logger.info(f"[FULLNAME SEARCH] Value can also be normalized as facebook_link: '{normalized_fb_link}'")
            if not normalized_tg_user:
                normalized_tg_user = search_value
                logger.info(f"[FULLNAME SEARCH] Will also search in telegram_user with original value: '{search_value}'")

        all_results = []
        seen_ids = set()

        pattern = f"%{normalized_fullname}%"
        logger.info(f"[FULLNAME SEARCH] Using pattern: '{pattern}' for field 'fullname'")
        try:
            response = client.table(TABLE_NAME).select("*").ilike("fullname", pattern).order("created_at", desc=True).limit(30).execute()
            if response.data:
                for item in response.data:
                    if item.get('id') not in seen_ids:
                        all_results.append(item)
                        seen_ids.add(item.get('id'))
                logger.info(f"[FULLNAME SEARCH] ✅ Found {len(response.data)} results in fullname field")
        except Exception as e:
            logger.warning(f"[FULLNAME SEARCH] Error searching fullname: {e}")

        if normalized_tg_user:
            try:
                response = client.table(TABLE_NAME).select("*").eq("telegram_user", normalized_tg_user).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
                    logger.info(f"[FULLNAME SEARCH] ✅ Found {len(response.data)} results in telegram_user field")
            except Exception as e:
                logger.warning(f"[FULLNAME SEARCH] Error searching telegram_user: {e}")

        if normalized_tg_id:
            try:
                response = client.table(TABLE_NAME).select("*").eq("telegram_id", normalized_tg_id).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
                    logger.info(f"[FULLNAME SEARCH] ✅ Found {len(response.data)} results in telegram_id field")
            except Exception as e:
                logger.warning(f"[FULLNAME SEARCH] Error searching telegram_id: {e}")

        if normalized_fb_link:
            try:
                response = client.table(TABLE_NAME).select("*").eq("facebook_link", normalized_fb_link).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
                    logger.info(f"[FULLNAME SEARCH] ✅ Found {len(response.data)} results in facebook_link field")
            except Exception as e:
                logger.warning(f"[FULLNAME SEARCH] Error searching facebook_link: {e}")

        if normalized_fullname:
            try:
                pattern = f"%{normalized_fullname}%"
                logger.info(f"[FULLNAME SEARCH] Using pattern: '{pattern}' for field 'manager_name'")
                response = client.table(TABLE_NAME).select("*").ilike("manager_name", pattern).order("created_at", desc=True).limit(30).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
                    logger.info(f"[FULLNAME SEARCH] ✅ Found {len(response.data)} results in manager_name field")
            except Exception as e:
                logger.warning(f"[FULLNAME SEARCH] Error searching manager_name: {e}")

        all_results.sort(key=lambda x: x.get('created_at', ''), reverse=True)

        field_labels = {
            'fullname': 'Клиент',
            'facebook_link': 'Facebook Ссылка',
            'telegram_user': 'Тег Telegram',
            'telegram_id': 'Telegram ID',
            'manager_name': 'Агент',
            'manager_tag': 'Тег Агента',
            'photo_url': 'Фото',
            'created_at': 'Дата'
        }

        if all_results:
            if len(all_results) == 1:
                result = all_results[0]
                photo_url = result.get('photo_url')

                message_parts = [f"✅ <b>Найдено клиентов: 1</b>", ""]

                for field_name_key, field_label in field_labels.items():
                    value = result.get(field_name_key)

                    if value is None or value == '' or value == 'Не указано':
                        continue

                    if field_name_key == 'photo_url':
                        continue

                    if field_name_key == 'created_at':
                        try:
                            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                            value = dt.strftime('%d.%m.%Y %H:%M')
                        except Exception:
                            pass

                    if field_name_key == 'facebook_link':
                        value = format_facebook_link_for_display(value)

                    if field_name_key == 'manager_tag':
                        tag_value = str(value).strip()
                        message_parts.append(f"{field_label}: @{tag_value}")
                    else:
                        escaped_value = escape_html(str(value))
                        message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

                message = "\n".join(message_parts)

                keyboard = []
                lead_id = result.get('id')
                if lead_id is not None:
                    keyboard.append([InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_lead_{lead_id}")])
                keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
                reply_markup = InlineKeyboardMarkup(keyboard)

                if photo_url and str(photo_url).strip():
                    try:
                        photo_bytes = await download_photo_from_supabase(str(photo_url).strip())
                        if photo_bytes:
                            photo_file = io.BytesIO(photo_bytes)
                            sent_message = await update.message.reply_photo(
                                photo=photo_file,
                                caption=message,
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                        else:
                            sent_message = await update.message.reply_text(
                                message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                    except Exception as e:
                        logger.error(f"[FULLNAME SEARCH] Error sending photo: {e}", exc_info=True)
                        sent_message = await update.message.reply_text(
                            message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                else:
                    sent_message = await update.message.reply_text(
                        message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                await save_check_message(update, context, sent_message.message_id)
            else:
                message_parts = [f"✅ <b>Найдено клиентов: {len(all_results)}</b>\n"]

                for idx, result in enumerate(all_results, 1):
                    if idx > 1:
                        message_parts.append("")
                    message_parts.append(f"<b>━━━ Клиент {idx} ━━━</b>")
                    for field_name_key, field_label in field_labels.items():
                        value = result.get(field_name_key)

                        if value is None or value == '' or value == 'Не указано':
                            continue

                        if field_name_key == 'photo_url':
                            continue

                        if field_name_key == 'created_at':
                            try:
                                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                                value = dt.strftime('%d.%m.%Y %H:%M')
                            except Exception:
                                pass

                        if field_name_key == 'facebook_link':
                            value = format_facebook_link_for_display(value)

                        if field_name_key == 'manager_tag':
                            tag_value = str(value).strip()
                            message_parts.append(f"{field_label}: @{tag_value}")
                        else:
                            escaped_value = escape_html(str(value))
                            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

                message = "\n".join(message_parts)

                keyboard = [
                    [InlineKeyboardButton("✅ Добавить лид", callback_data="add_new")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                # Проверка длины сообщения перед отправкой
                if len(message) > TELEGRAM_MESSAGE_CHAR_LIMIT:
                    sent_message = await _send_results_as_csv(
                        update,
                        all_results,
                        field_labels,
                        search_value
                    )
                else:
                    try:
                        sent_message = await update.message.reply_text(
                            message,
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                    except BadRequest as e:
                        if "Message is too long" in str(e):
                            logger.warning(f"[FULLNAME SEARCH] Message too long, sending as CSV instead")
                            sent_message = await _send_results_as_csv(
                                update,
                                all_results,
                                field_labels,
                                search_value
                            )
                        else:
                            raise
                await save_check_message(update, context, sent_message.message_id)
        else:
            message = (
                "❌ <b>Клиенты не найдены</b>\n\n"
                "💡 Возможные причины:\n"
                "• Данные введены с ошибкой\n"
                "• Клиент ещё не добавлен в базу\n"
                "• Неполные данные\n\n"
                "Что можно сделать:\n"
                "• Проверить правильность введенных данных\n"
                "• Использовать другой способ поиска\n"
                "• Убедиться, что данные введены полностью"
            )
            reply_markup = get_main_menu_keyboard()
            sent_message = await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            await save_check_message(update, context, sent_message.message_id)

    except Exception as e:
        logger.error(f"[FULLNAME SEARCH] ❌ Error in fullname search: {e}", exc_info=True)
        error_msg = get_user_friendly_error(e, "проверке")
        sent_message = await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        await save_check_message(update, context, sent_message.message_id)

    return ConversationHandler.END


async def check_by_extracted_fields(update: Update, context: ContextTypes.DEFAULT_TYPE, extracted_data: dict):
    """
    Check leads by extracted fields from forwarded message.
    Searches in: telegram_user, telegram_id, fullname (based on what's available)
    """
    user_id = update.effective_user.id
    logger.info(f"[EXTRACTED_FIELDS_CHECK] Starting check with extracted data: {list(extracted_data.keys())}")

    client = get_supabase_client()
    if not client:
        error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
        if update.callback_query:
            await update.callback_query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
        elif update.message:
            await update.message.reply_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
        return

    try:
        all_results = []
        seen_ids = set()

        if 'telegram_name' in extracted_data and extracted_data['telegram_name']:
            telegram_name = extracted_data['telegram_name']
            try:
                response = client.table(TABLE_NAME).select("*").eq("telegram_user", telegram_name).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[EXTRACTED_FIELDS_CHECK] Error searching telegram_user: {e}")

        if 'telegram_id' in extracted_data and extracted_data['telegram_id']:
            telegram_id = extracted_data['telegram_id']
            try:
                response = client.table(TABLE_NAME).select("*").eq("telegram_id", telegram_id).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[EXTRACTED_FIELDS_CHECK] Error searching telegram_id: {e}")

        if 'fullname' in extracted_data and extracted_data['fullname']:
            fullname = extracted_data['fullname']
            if len(fullname.strip()) >= 3:
                escaped_fullname = fullname.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                pattern = f"%{escaped_fullname}%"
                try:
                    response = client.table(TABLE_NAME).select("*").ilike("fullname", pattern).limit(50).execute()
                    if response.data:
                        for item in response.data:
                            if item.get('id') not in seen_ids:
                                all_results.append(item)
                                seen_ids.add(item.get('id'))
                except Exception as e:
                    logger.warning(f"[EXTRACTED_FIELDS_CHECK] Error searching fullname: {e}")
            else:
                logger.info(f"[EXTRACTED_FIELDS_CHECK] Skipping fullname search - value too short (length: {len(fullname.strip())})")

        all_results = all_results[:50]

        field_labels = {
            'fullname': 'Клиент',
            'facebook_link': 'Facebook Ссылка',
            'telegram_user': 'Тег Telegram',
            'telegram_id': 'Telegram ID',
            'manager_name': 'Агент',
            'manager_tag': 'Тег Агента',
            'photo_url': 'Фото',
            'created_at': 'Дата'
        }

        if all_results:
            photo_url = None
            if len(all_results) > 1:
                message_parts = [f"✅ <b>Найдено клиентов: {len(all_results)}</b>\n"]

                for idx, result in enumerate(all_results, 1):
                    if idx > 1:
                        message_parts.append("")
                    message_parts.append(f"<b>━━━ Клиент {idx} ━━━</b>")
                    for field_name_key, field_label in field_labels.items():
                        value = result.get(field_name_key)

                        if value is None or value == '' or value == 'Не указано':
                            continue

                        if field_name_key == 'photo_url':
                            continue

                        if field_name_key == 'created_at':
                            try:
                                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                                value = dt.strftime('%d.%m.%Y %H:%M')
                            except Exception:
                                pass

                        if field_name_key == 'facebook_link':
                            value = format_facebook_link_for_display(value)

                        if field_name_key == 'manager_tag':
                            tag_value = str(value).strip()
                            message_parts.append(f"{field_label}: @{tag_value}")
                        else:
                            escaped_value = escape_html(str(value))
                            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

                message = "\n".join(message_parts)
                keyboard = [
                    [InlineKeyboardButton("✅ Добавить лид", callback_data="add_new")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                elif update.message:
                    await update.message.reply_text(
                        message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
            else:
                result = all_results[0]
                message_parts = [f"✅ <b>Найдено клиентов: 1</b>", ""]
                for field_name_key, field_label in field_labels.items():
                    value = result.get(field_name_key)

                    if value is None or value == '' or value == 'Не указано':
                        continue

                    if field_name_key == 'photo_url':
                        continue

                    if field_name_key == 'created_at':
                        try:
                            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                            value = dt.strftime('%d.%m.%Y %H:%M')
                        except Exception:
                            pass

                    if field_name_key == 'facebook_link':
                        value = format_facebook_link_for_display(value)

                    if field_name_key == 'manager_tag':
                        tag_value = str(value).strip()
                        message_parts.append(f"{field_label}: @{tag_value}")
                    else:
                        escaped_value = escape_html(str(value))
                        message_parts.append(f"{field_label}: <code>{escaped_value}</code>")

                message = "\n".join(message_parts)

                keyboard = []
                lead_id = result.get('id')
                if lead_id is not None:
                    keyboard.append([InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_lead_{lead_id}")])
                keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
                reply_markup = InlineKeyboardMarkup(keyboard)

                photo_url = result.get('photo_url')
                if photo_url and str(photo_url).strip() and update.message:
                    try:
                        photo_bytes = await download_photo_from_supabase(str(photo_url).strip())
                        if photo_bytes:
                            photo_file = io.BytesIO(photo_bytes)
                            await update.message.reply_photo(
                                photo=photo_file,
                                caption=message,
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                        else:
                            await update.message.reply_text(
                                message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                    except Exception as e:
                        logger.error(f"[EXTRACTED_FIELDS_CHECK] Error sending photo: {e}", exc_info=True)
                        await update.message.reply_text(
                            message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                elif update.callback_query:
                    await update.callback_query.edit_message_text(
                        message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                elif update.message:
                    await update.message.reply_text(
                        message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
        else:
            message = (
                "❌ <b>Клиенты не найдены</b>\n\n"
                "💡 Возможные причины:\n"
                "• Данные введены с ошибкой\n"
                "• Клиент ещё не добавлен в базу\n"
                "• Неполные данные\n\n"
                "Что можно сделать:\n"
                "• Проверить правильность введенных данных\n"
                "• Использовать другой способ поиска\n"
                "• Убедиться, что данные введены полностью"
            )
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    message,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            elif update.message:
                await update.message.reply_text(
                    message,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
    except Exception as e:
        logger.error(f"[EXTRACTED_FIELDS_CHECK] ❌ Error in extracted fields check: {e}", exc_info=True)
        error_msg = get_user_friendly_error(e, "проверке")
        if update.callback_query:
            await update.callback_query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
        elif update.message:
            await update.message.reply_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )

