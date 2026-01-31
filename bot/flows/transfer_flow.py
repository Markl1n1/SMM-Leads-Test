from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import PIN_CODE
from bot.constants import (
    TRANSFER_PIN,
    TRANSFER_SELECT_FROM,
    TRANSFER_SELECT_TO,
    TRANSFER_CONFIRM,
    SMART_CHECK_INPUT,
)
from bot.keyboards import get_main_menu_keyboard
from bot.logging import logger
from bot.services.leads_repo import (
    get_unique_manager_names,
    count_records_by_manager_name,
    get_manager_tag_by_name,
    transfer_manager_leads,
)
from bot.services.supabase_client import get_supabase_client
from bot.state import (
    clear_all_conversation_state,
    log_conversation_state,
    user_data_store,
    user_data_store_access_time,
)
from bot.utils import escape_html, get_user_friendly_error, normalize_tag


def _build_manager_keyboard(manager_names: list[str], prefix: str) -> InlineKeyboardMarkup:
    keyboard = []
    for i in range(0, len(manager_names), 2):
        row = [InlineKeyboardButton(manager_names[i], callback_data=f"{prefix}{i}")]
        if i + 1 < len(manager_names):
            row.append(InlineKeyboardButton(manager_names[i + 1], callback_data=f"{prefix}{i + 1}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


async def transfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /transfer command - request PIN code before showing manager list."""
    try:
        user_id = update.effective_user.id
        from_user = update.effective_user
        logger.info(
            f"[TRANSFER] /transfer command received from user_id={user_id}, "
            f"name='{from_user.first_name} {from_user.last_name}', "
            f"username='{from_user.username}', "
            f"context_keys_before_clear={list(context.user_data.keys()) if context.user_data else []}"
        )

        # ALWAYS clear all conversation states to interrupt any current process
        clear_all_conversation_state(context, user_id)
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]
        for key in [
            'current_field', 'current_state', 'add_step', 'editing_lead_id',
            'transfer_from_manager', 'transfer_to_manager', 'transfer_manager_names',
            'transfer_to_tag', 'pin_attempts'
        ]:
            context.user_data.pop(key, None)

        # Reset PIN attempt counter when starting new transfer flow
        context.user_data['pin_attempts'] = 0
        context.user_data['current_state'] = TRANSFER_PIN

        await update.message.reply_text(
            "🔒 Для переноса лидов требуется PIN-код.\n\nВведите PIN-код:"
        )

        log_conversation_state(user_id, context, prefix="[TRANSFER_AFTER_PIN_REQUEST]")
        logger.info(
            f"[TRANSFER] Requested PIN code from user {user_id}, expecting TRANSFER_PIN."
        )
        return TRANSFER_PIN
    except Exception as e:
        logger.error(f"[TRANSFER] Error in transfer_command: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "❌ Произошла ошибка при обработке команды. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END


async def transfer_pin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PIN code input for transfer command."""
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')

    # CRITICAL: If user is in check flow, don't intercept the message
    if current_state == SMART_CHECK_INPUT:
        logger.info(
            f"[TRANSFER_PIN_INPUT] User {user_id} is in check flow (SMART_CHECK_INPUT), "
            "clearing stale transfer state and ending conversation"
        )
        for key in ['pin_attempts', 'transfer_from_manager', 'transfer_to_manager', 'transfer_to_tag']:
            context.user_data.pop(key, None)
        return ConversationHandler.END

    if not update.message or not update.message.text:
        if update.message:
            await update.message.reply_text(
                "❌ Ошибка: Неверный формат сообщения. Пожалуйста, введите PIN-код текстом."
            )
        else:
            logger.error("transfer_pin_input: update.message is None")
        return TRANSFER_PIN

    text = update.message.text.strip()
    if text == PIN_CODE:
        if 'pin_attempts' in context.user_data:
            del context.user_data['pin_attempts']

        client = get_supabase_client()
        if not client:
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            await update.message.reply_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END

        manager_names = get_unique_manager_names(client)
        if not manager_names:
            await update.message.reply_text(
                "❌ В базе данных нет записей с manager_name.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        context.user_data['transfer_manager_names'] = manager_names
        reply_markup = _build_manager_keyboard(manager_names, "transfer_from_")
        await update.message.reply_text(
            "🔁 <b>Выберите менеджера-источника:</b>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        context.user_data['current_state'] = TRANSFER_SELECT_FROM
        return TRANSFER_SELECT_FROM

    # PIN is incorrect, increment attempt counter
    pin_attempts = context.user_data.get('pin_attempts', 0) + 1
    context.user_data['pin_attempts'] = pin_attempts

    if pin_attempts >= 3:
        await update.message.reply_text(
            "❌ Превышено количество попыток ввода PIN-кода (3). Доступ заблокирован.",
            reply_markup=get_main_menu_keyboard()
        )
        if 'pin_attempts' in context.user_data:
            del context.user_data['pin_attempts']
        return ConversationHandler.END

    remaining_attempts = 3 - pin_attempts
    await update.message.reply_text(
        f"❌ Неверный PIN-код. Осталось попыток: {remaining_attempts}\n\nВведите PIN-код:"
    )
    return TRANSFER_PIN


async def transfer_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle selection of source manager."""
    query = update.callback_query
    await query.answer()

    try:
        user_id = update.effective_user.id
        callback_data = query.data or ""
        logger.info(
            f"[TRANSFER] transfer_from_callback called for user {user_id}, "
            f"callback_data={callback_data}, "
            f"context_keys={list(context.user_data.keys()) if context.user_data else []}"
        )

        if not callback_data.startswith("transfer_from_"):
            await query.edit_message_text(
                "❌ Ошибка: неверные данные. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        manager_names = context.user_data.get('transfer_manager_names') or []
        try:
            index_str = callback_data.replace("transfer_from_", "", 1)
            index = int(index_str)
        except (ValueError, IndexError):
            await query.edit_message_text(
                "❌ Ошибка: неверный индекс. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        if index < 0 or index >= len(manager_names):
            await query.edit_message_text(
                "❌ Ошибка: неверный индекс. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        from_manager = manager_names[index]
        context.user_data['transfer_from_manager'] = from_manager

        reply_markup = _build_manager_keyboard(manager_names, "transfer_to_")
        await query.edit_message_text(
            f"🔁 <b>Источник:</b> {escape_html(from_manager)}\n\n"
            "Теперь выберите менеджера‑получателя:",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        context.user_data['current_state'] = TRANSFER_SELECT_TO
        return TRANSFER_SELECT_TO
    except Exception as e:
        logger.error(f"Error in transfer_from_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END


async def transfer_to_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle selection of target manager and show confirmation."""
    query = update.callback_query
    await query.answer()

    try:
        user_id = update.effective_user.id
        callback_data = query.data or ""
        logger.info(
            f"[TRANSFER] transfer_to_callback called for user {user_id}, "
            f"callback_data={callback_data}, "
            f"context_keys={list(context.user_data.keys()) if context.user_data else []}"
        )

        if not callback_data.startswith("transfer_to_"):
            await query.edit_message_text(
                "❌ Ошибка: неверные данные. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        manager_names = context.user_data.get('transfer_manager_names') or []
        try:
            index_str = callback_data.replace("transfer_to_", "", 1)
            index = int(index_str)
        except (ValueError, IndexError):
            await query.edit_message_text(
                "❌ Ошибка: неверный индекс. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        if index < 0 or index >= len(manager_names):
            await query.edit_message_text(
                "❌ Ошибка: неверный индекс. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        from_manager = context.user_data.get('transfer_from_manager')
        to_manager = manager_names[index]
        if not from_manager:
            await query.edit_message_text(
                "❌ Ошибка: источник не выбран. Попробуйте снова с команды /transfer.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        if to_manager == from_manager:
            reply_markup = _build_manager_keyboard(manager_names, "transfer_to_")
            await query.edit_message_text(
                "❌ Нельзя выбрать того же менеджера. Выберите другого:",
                reply_markup=reply_markup
            )
            return TRANSFER_SELECT_TO

        client = get_supabase_client()
        if not client:
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            await query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END

        record_count = count_records_by_manager_name(client, from_manager)
        raw_tag = get_manager_tag_by_name(client, to_manager)
        to_tag = normalize_tag(raw_tag) if raw_tag else ""
        context.user_data['transfer_to_manager'] = to_manager
        context.user_data['transfer_to_tag'] = to_tag

        tag_display = f"@{escape_html(to_tag)}" if to_tag else "—"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="transfer_confirm")],
            [InlineKeyboardButton("❌ Отменить", callback_data="transfer_cancel")]
        ])

        await query.edit_message_text(
            "📋 <b>Подтверждение переноса</b>\n\n"
            f"<b>От:</b> {escape_html(from_manager)}\n"
            f"<b>К:</b> {escape_html(to_manager)}\n"
            f"<b>Тег получателя:</b> {tag_display}\n"
            f"<b>Будет обновлено записей:</b> {record_count}\n\n"
            "Подтвердите перенос:",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        context.user_data['current_state'] = TRANSFER_CONFIRM
        return TRANSFER_CONFIRM
    except Exception as e:
        logger.error(f"Error in transfer_to_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END


async def transfer_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation of transfer."""
    query = update.callback_query
    await query.answer()

    try:
        from_manager = context.user_data.get('transfer_from_manager')
        to_manager = context.user_data.get('transfer_to_manager')
        to_tag = context.user_data.get('transfer_to_tag', "")

        if not from_manager or not to_manager:
            await query.edit_message_text(
                "❌ Ошибка: не удалось получить данные. Попробуйте снова с команды /transfer.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        client = get_supabase_client()
        if not client:
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            await query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END

        updated_count = transfer_manager_leads(client, from_manager, to_manager, to_tag)
        clear_all_conversation_state(context, update.effective_user.id)

        tag_display = f"@{escape_html(to_tag)}" if to_tag else "—"
        await query.edit_message_text(
            "✅ <b>Перенос завершен!</b>\n\n"
            f"<b>От:</b> {escape_html(from_manager)}\n"
            f"<b>К:</b> {escape_html(to_manager)}\n"
            f"<b>Тег получателя:</b> {tag_display}\n"
            f"<b>Обновлено записей:</b> {updated_count}",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in transfer_confirm_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка при переносе. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END


async def transfer_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancellation of transfer."""
    query = update.callback_query
    await query.answer()

    try:
        clear_all_conversation_state(context, update.effective_user.id)
        await query.edit_message_text(
            "❌ Перенос отменен.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in transfer_cancel_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

