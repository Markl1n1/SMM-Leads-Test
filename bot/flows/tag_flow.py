from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import PIN_CODE
from bot.constants import TAG_PIN, TAG_SELECT_MANAGER, TAG_ENTER_NEW, SMART_CHECK_INPUT
from bot.keyboards import get_main_menu_keyboard
from bot.logging import logger
from bot.services.leads_repo import get_unique_manager_names, count_records_by_manager_name, update_manager_tag_by_name
from bot.services.supabase_client import get_supabase_client
from bot.state import (
    clear_all_conversation_state,
    log_conversation_state,
    user_data_store,
    user_data_store_access_time,
)
from bot.utils import escape_html, get_user_friendly_error, normalize_tag

async def tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tag command - request PIN code before showing manager list
    This command ALWAYS interrupts any current process and starts its own flow."""
    try:
        user_id = update.effective_user.id
        from_user = update.effective_user
        logger.info(
            f"[TAG] /tag command received from user_id={user_id}, "
            f"name='{from_user.first_name} {from_user.last_name}', "
            f"username='{from_user.username}', "
            f"context_keys_before_clear={list(context.user_data.keys()) if context.user_data else []}"
        )
        
        # ALWAYS clear all conversation states to interrupt any current process
        clear_all_conversation_state(context, user_id)
        # Also clear user_data_store to prevent check_add_state_entry from intercepting messages
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]
        # Explicitly clear ALL flow related keys from context.user_data
        for key in ['current_field', 'current_state', 'add_step', 'editing_lead_id', 
                    'tag_manager_name', 'tag_new_tag', 'pin_attempts']:
            context.user_data.pop(key, None)
        
        logger.info(f"[TAG] All states cleared for user {user_id}, starting tag flow, context_keys_after_clear={list(context.user_data.keys()) if context.user_data else []}")
        
        # Reset PIN attempt counter when starting new tag flow
        context.user_data['pin_attempts'] = 0
        # Explicitly set current_state to TAG_PIN to prevent check_add_state_entry from intercepting
        context.user_data['current_state'] = TAG_PIN
        
        # Request PIN code before allowing tag change
        message = "🔒 Для изменения тега менеджера требуется PIN-код.\n\nВведите PIN-код:"
        await update.message.reply_text(message)
        
        # ADD THIS LOGGING
        log_conversation_state(user_id, context, prefix="[TAG_AFTER_PIN_REQUEST]")
        
        logger.info(
            f"[TAG] Requested PIN code from user {user_id}, expecting TAG_PIN, current_state set to TAG_PIN. "
            f"Returning TAG_PIN state."
        )
        return TAG_PIN
    except Exception as e:
        logger.error(f"[TAG] Error in tag_command: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "❌ Произошла ошибка при обработке команды. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def tag_pin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PIN code input for tag command"""
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')
    
    # CRITICAL: If user is in check flow, don't intercept the message
    if current_state == SMART_CHECK_INPUT:
        logger.info(
            f"[TAG_PIN_INPUT] User {user_id} is in check flow (SMART_CHECK_INPUT), "
            "clearing stale tag state and ending conversation"
        )
        # Clear stale tag state
        for key in ['pin_attempts', 'tag_manager_name', 'tag_new_tag']:
            context.user_data.pop(key, None)
        return ConversationHandler.END
    
    # Safety check: if there are no tag flow markers AND we're not in TAG_PIN state with pin_attempts,
    # treat this as a stale PIN flow and do not intercept the message (so that add/edit flows can handle it)
    # Note: tag_manager_name and tag_new_tag appear only AFTER successful PIN input when user selects manager,
    # so we need to check for valid TAG_PIN state as well
    is_valid_tag_pin_state = (
        context.user_data.get('current_state') == TAG_PIN and 
        'pin_attempts' in context.user_data
    )
    has_tag_flow_markers = (
        context.user_data.get('tag_manager_name') or 
        context.user_data.get('tag_new_tag')
    )
    
    if not is_valid_tag_pin_state and not has_tag_flow_markers:
        logger.info(
            f"[TAG_PIN_INPUT] Called without active tag flow markers for user {user_id}, "
            f"current_state={context.user_data.get('current_state')}, "
            f"pin_attempts={'pin_attempts' in context.user_data}, "
            "treating as stale PIN flow and ending conversation"
        )
        if 'pin_attempts' in context.user_data:
            del context.user_data['pin_attempts']
        return ConversationHandler.END
    
    # ADD DETAILED LOGGING AT THE START
    logger.info(
        f"[TAG_PIN_INPUT] ⚡ Function CALLED for user {user_id}, "
        f"message_text='{update.message.text if update.message and update.message.text else None}', "
        f"current_state={context.user_data.get('current_state')}, "
        f"pin_attempts={context.user_data.get('pin_attempts')}, "
        f"conversation_keys={[k for k in (context.user_data.keys() if context.user_data else []) if k.startswith('_conversation_')]}"
    )
    
    # Check if message exists and has text
    if not update.message or not update.message.text:
        if update.message:
            await update.message.reply_text(
                "❌ Ошибка: Неверный формат сообщения. Пожалуйста, введите PIN-код текстом."
            )
        else:
            logger.error("tag_pin_input: update.message is None")
        return TAG_PIN
    
    text = update.message.text.strip()
    
    # PIN code from environment variable
    if text == PIN_CODE:
        # PIN is correct, reset attempt counter
        if 'pin_attempts' in context.user_data:
            del context.user_data['pin_attempts']
        
        # PIN is correct, load manager names and show selection
        # Clear any old field editing state to prevent automatic transitions
        if 'current_field' in context.user_data:
            del context.user_data['current_field']
        if 'current_state' in context.user_data:
            del context.user_data['current_state']
        
        # Get Supabase client
        client = get_supabase_client()
        if not client:
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            logger.error(f"[TAG] get_supabase_client returned None for user {user_id}")
            await update.message.reply_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
        
        # Get unique manager names
        manager_names = get_unique_manager_names(client)
        logger.info(f"[TAG] Loaded {len(manager_names)} unique manager_name values for user {user_id}: {manager_names}")
        
        if not manager_names:
            await update.message.reply_text(
                "❌ В базе данных нет записей с manager_name.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Store manager names in context for later retrieval (to avoid long callback_data)
        context.user_data['tag_manager_names'] = manager_names
        logger.info(f"[TAG] Stored tag_manager_names in context for user {user_id}")
        
        # Create keyboard with manager names
        keyboard = []
        # Add buttons in rows of 2
        # Use index in callback_data instead of full name to avoid exceeding 64 byte limit
        for i in range(0, len(manager_names), 2):
            row = []
            row.append(InlineKeyboardButton(
                manager_names[i],
                callback_data=f"tag_mgr_{i}"
            ))
            if i + 1 < len(manager_names):
                row.append(InlineKeyboardButton(
                    manager_names[i + 1],
                    callback_data=f"tag_mgr_{i + 1}"
                ))
            keyboard.append(row)
        
        # Add main menu button
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🏷️ <b>Выберите менеджера для смены тега:</b>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        
        # Explicitly set current_state to TAG_SELECT_MANAGER to prevent other handlers from intercepting
        context.user_data['current_state'] = TAG_SELECT_MANAGER
        
        logger.info(f"[TAG] Sent manager selection keyboard to user {user_id}, expecting TAG_SELECT_MANAGER, current_state set to TAG_SELECT_MANAGER")
        return TAG_SELECT_MANAGER
    else:
        # PIN is incorrect, increment attempt counter
        pin_attempts = context.user_data.get('pin_attempts', 0) + 1
        context.user_data['pin_attempts'] = pin_attempts
        
        if pin_attempts >= 3:
            # Too many failed attempts, return to main menu
            await update.message.reply_text(
                "❌ Превышено количество попыток ввода PIN-кода (3). Доступ к изменению тега заблокирован.",
                reply_markup=get_main_menu_keyboard()
            )
            # Clear tag state
            if 'pin_attempts' in context.user_data:
                del context.user_data['pin_attempts']
            return ConversationHandler.END
        else:
            # PIN is incorrect, ask again
            remaining_attempts = 3 - pin_attempts
            await update.message.reply_text(
                f"❌ Неверный PIN-код. Осталось попыток: {remaining_attempts}\n\nВведите PIN-код:"
            )
            return TAG_PIN

async def tag_manager_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle selection of manager from tag command"""
    query = update.callback_query
    await query.answer()
    
    try:
        user_id = update.effective_user.id
        callback_data = query.data
        logger.info(
            f"[TAG] tag_manager_callback called for user {user_id}, "
            f"callback_data={callback_data}, "
            f"context_keys={list(context.user_data.keys()) if context.user_data else []}"
        )
        
        # Clear user_data_store to prevent check_add_state_entry from intercepting messages
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]
        # Explicitly clear add flow related keys from context.user_data
        if 'current_field' in context.user_data:
            del context.user_data['current_field']
        if 'current_state' in context.user_data:
            del context.user_data['current_state']
        if 'add_step' in context.user_data:
            del context.user_data['add_step']
        
        # Extract index from callback_data
        # Format: "tag_mgr_{index}"
        if not callback_data.startswith("tag_mgr_"):
            logger.error(f"[TAG] Invalid callback_data: {callback_data}")
            await query.edit_message_text(
                "❌ Ошибка: неверные данные. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Extract index
        try:
            index_str = callback_data.replace("tag_mgr_", "", 1)
            index = int(index_str)
        except (ValueError, IndexError):
            logger.error(f"[TAG] Invalid index in callback_data: {callback_data}")
            await query.edit_message_text(
                "❌ Ошибка: неверный индекс. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Get manager name from stored list
        # If list is not in context (e.g., if called as entry point), reload it
        manager_names = context.user_data.get('tag_manager_names')
        logger.info(f"[TAG] tag_manager_names in context for user {user_id}: {manager_names}")
        if not manager_names:
            logger.info(f"[TAG] manager_names not in context, reloading from database for user {user_id}")
            client = get_supabase_client()
            if not client:
                error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
                await query.edit_message_text(
                    error_msg,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
                return ConversationHandler.END
            manager_names = get_unique_manager_names(client)
            if not manager_names:
                await query.edit_message_text(
                    "❌ В базе данных нет записей с manager_name.",
                    reply_markup=get_main_menu_keyboard(),
                )
                return ConversationHandler.END
            context.user_data['tag_manager_names'] = manager_names
            logger.info(f"[TAG] Reloaded tag_manager_names for user {user_id}: {manager_names}")
        
        if index < 0 or index >= len(manager_names):
            logger.error(f"[TAG] Invalid index {index} for manager_names list of length {len(manager_names)}")
            await query.edit_message_text(
                "❌ Ошибка: неверный индекс. Попробуйте снова с команды /tag",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        manager_name = manager_names[index]
        logger.info(f"[TAG] User {user_id} selected manager_name='{manager_name}' (index={index})")
        
        # Save manager_name to context
        context.user_data['tag_manager_name'] = manager_name
        logger.info(f"[TAG] Saved tag_manager_name in context for user {user_id}, context_keys_now={list(context.user_data.keys()) if context.user_data else []}")
        
        # Ask for new tag
        await query.edit_message_text(
            f"🏷️ <b>Менеджер:</b> {escape_html(manager_name)}\n\n"
            f"📝 Введите новый тег (с @ или без):",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отменить", callback_data="tag_cancel")]
            ])
        )
        
        logger.info(f"[TAG] Prompted user {user_id} to enter new tag, expecting TAG_ENTER_NEW")
        return TAG_ENTER_NEW
    except Exception as e:
        logger.error(f"Error in tag_manager_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def tag_enter_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new tag input"""
    if not update.message or not update.message.text:
        logger.error("[TAG] tag_enter_new: update.message or update.message.text is None")
        return ConversationHandler.END
    
    try:
        user_id = update.effective_user.id
        raw_text = update.message.text
        text = raw_text.strip()
        logger.info(
            f"[TAG] tag_enter_new called for user {user_id}, "
            f"raw_text='{raw_text}', normalized_text='{text}', "
            f"context_keys={list(context.user_data.keys()) if context.user_data else []}"
        )
        
        # Normalize tag
        normalized_tag = normalize_tag(text)
        logger.info(f"[TAG] tag_enter_new: normalized_tag='{normalized_tag}' for user {user_id}")
        
        if not normalized_tag:
            logger.warning(f"[TAG] tag_enter_new: empty normalized_tag for user {user_id}")
            await update.message.reply_text(
                "❌ Тег не может быть пустым. Попробуйте снова:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отменить", callback_data="tag_cancel")]
                ])
            )
            return TAG_ENTER_NEW
        
        # Get manager_name from context
        manager_name = context.user_data.get('tag_manager_name')
        if not manager_name:
            logger.error(f"[TAG] tag_enter_new: manager_name not found in context for user {user_id}")
            await update.message.reply_text(
                "❌ Ошибка: не удалось определить менеджера. Попробуйте снова с команды /tag",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        logger.info(f"[TAG] tag_enter_new: manager_name='{manager_name}' for user {user_id}")
        
        # Get Supabase client
        client = get_supabase_client()
        if not client:
            logger.error(f"[TAG] tag_enter_new: get_supabase_client returned None for user {user_id}")
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            await update.message.reply_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
        
        # Count records that will be updated
        record_count = count_records_by_manager_name(client, manager_name)
        logger.info(f"[TAG] tag_enter_new: record_count={record_count} for manager_name='{manager_name}' and user {user_id}")
        
        # Save new tag to context
        context.user_data['tag_new_tag'] = normalized_tag
        logger.info(f"[TAG] tag_enter_new: saved tag_new_tag='{normalized_tag}' in context for user {user_id}, context_keys_now={list(context.user_data.keys()) if context.user_data else []}")
        
        # Show confirmation
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="tag_confirm")],
            [InlineKeyboardButton("❌ Отменить", callback_data="tag_cancel")]
        ])
        
        await update.message.reply_text(
            f"📋 <b>Подтверждение обновления тега</b>\n\n"
            f"<b>Менеджер:</b> {escape_html(manager_name)}\n"
            f"<b>Новый тег:</b> <code>{escape_html(normalized_tag)}</code>\n"
            f"<b>Будет обновлено записей:</b> {record_count}\n\n"
            f"Подтвердите обновление:",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        
        logger.info(f"[TAG] tag_enter_new: sent confirmation message to user {user_id}, staying in TAG_ENTER_NEW")
        return TAG_ENTER_NEW
    except Exception as e:
        logger.error(f"[TAG] Error in tag_enter_new for user {update.effective_user.id}: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def tag_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation of tag update"""
    query = update.callback_query
    await query.answer()
    
    try:
        # Get manager_name and new_tag from context
        manager_name = context.user_data.get('tag_manager_name')
        new_tag = context.user_data.get('tag_new_tag')
        
        if not manager_name or not new_tag:
            logger.error(f"[TAG] Missing data in context: manager_name={manager_name}, new_tag={new_tag}")
            await query.edit_message_text(
                "❌ Ошибка: не удалось получить данные. Попробуйте снова с команды /tag",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Get Supabase client
        client = get_supabase_client()
        if not client:
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            await query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
        
        # Update manager_tag
        updated_count = update_manager_tag_by_name(client, manager_name, new_tag)
        
        # Clear context
        clear_all_conversation_state(context, update.effective_user.id)
        
        # Show success message
        await query.edit_message_text(
            f"✅ <b>Тег успешно обновлен!</b>\n\n"
            f"<b>Менеджер:</b> {escape_html(manager_name)}\n"
            f"<b>Новый тег:</b> <code>{escape_html(new_tag)}</code>\n"
            f"<b>Обновлено записей:</b> {updated_count}",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in tag_confirm_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка при обновлении тега. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def tag_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancellation of tag update"""
    query = update.callback_query
    await query.answer()
    
    try:
        # Clear context
        clear_all_conversation_state(context, update.effective_user.id)
        
        # Show cancellation message
        await query.edit_message_text(
            "❌ Обновление тега отменено.",
            reply_markup=get_main_menu_keyboard()
        )
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in tag_cancel_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END
