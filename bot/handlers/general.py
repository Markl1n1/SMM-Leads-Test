import os
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import is_facebook_flow_enabled
from bot.constants import (
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
    SMART_CHECK_INPUT,
    TRANSFER_PIN,
    TRANSFER_SELECT_FROM,
    TRANSFER_SELECT_TO,
    TRANSFER_CONFIRM,
)
from bot.keyboards import get_main_menu_keyboard, get_navigation_keyboard, get_add_menu_keyboard
from bot.logging import logger
from bot.state import (
    clear_all_conversation_state,
    cleanup_all_messages_before_main_menu,
    cleanup_add_messages,
    log_conversation_state,
    rate_limit_handler,
    user_data_store,
    user_data_store_access_time,
)
from bot.utils import get_field_label, retry_telegram_api
from bot.flows.check_flow import check_menu_callback
from bot.flows.add_flow import (
    add_new_callback,
    add_field_input,
    add_skip_callback,
    add_back_callback,
    add_cancel_callback,
    add_save_callback,
    add_edit_field_fullname_from_review_callback,
    add_edit_field_telegram_name_from_review_callback,
    add_edit_field_telegram_id_from_review_callback,
    add_edit_field_fb_link_from_review_callback,
    add_edit_back_to_review_callback,
    edit_fullname_from_review_callback,
    show_add_review,
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show main menu"""
    try:
        user_id = update.effective_user.id
        logger.info(f"[START] Clearing all conversation state for user {user_id}")
        
        # Clear all conversation state including internal ConversationHandler keys
        clear_all_conversation_state(context, user_id)
        
        # Clean up all intermediate messages before showing main menu
        await cleanup_all_messages_before_main_menu(update, context)
        
        welcome_message = (
            "👋 Добро пожаловать в ClientsBot!\n\n"
            "Главное меню\n\n"
            "Выберите действие:"
        )
        await retry_telegram_api(
            update.message.reply_text,
            text=welcome_message,
            reply_markup=get_main_menu_keyboard()
        )
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in start_command: {e}", exc_info=True)
        try:
            await retry_telegram_api(
                update.message.reply_text,
                text="❌ Произошла ошибка при обработке команды. Попробуйте позже."
            )
        except:
            pass
        return ConversationHandler.END

async def quit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /q command - return to main menu from any state"""
    try:
        user_id = update.effective_user.id
        update_type = "message" if update.message else "callback_query" if update.callback_query else "unknown"
        logger.info(f"[QUIT] /q command received from user {user_id} (update_type: {update_type})")
        logger.info(f"[QUIT] Context keys before quit: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        
        # Подготовить текст главного меню
        welcome_message = (
            "👋 Главное меню\n\n"
            "Выберите действие:"
        )
        
        # 1) Показать главное меню (чтобы пользователь сразу увидел результат /q)
        if update.message:
            sent_message = await retry_telegram_api(
                update.message.reply_text,
                text=welcome_message,
                reply_markup=get_main_menu_keyboard()
            )
        elif update.callback_query:
            query = update.callback_query
            await retry_telegram_api(query.answer)
            try:
                await retry_telegram_api(
                    query.edit_message_text,
                    text=welcome_message,
                    reply_markup=get_main_menu_keyboard()
                )
            except Exception as e:
                # If edit fails, send new message
                logger.warning(f"[QUIT] Could not edit message while handling /q: {e}")
                if query.message:
                    await retry_telegram_api(
                        query.message.reply_text,
                        text=welcome_message,
                        reply_markup=get_main_menu_keyboard()
                    )
        else:
            logger.error(f"[QUIT] No message or callback_query in update")
        
        # 2) Очистить все промежуточные сообщения до главного меню.
        # Здесь важно ДО очищения состояния, чтобы списки message_ids ещё были в context.user_data.
        try:
            if update.message or update.callback_query:
                await cleanup_all_messages_before_main_menu(update, context)
        except Exception as e:
            logger.warning(f"[QUIT] Error while cleaning up messages on /q: {e}", exc_info=True)
        
        # 3) Теперь полностью очистить все состояния (context.user_data, ConversationHandler ключи и user_data_store)
        logger.info(f"[QUIT] Clearing all conversation state for user {user_id} after /q")
        clear_all_conversation_state(context, user_id)
        logger.info(f"[QUIT] Context keys after quit clear: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"[QUIT] Error in quit_command: {e}", exc_info=True)
        try:
            if update.message:
                await retry_telegram_api(
                    update.message.reply_text,
                    text="❌ Произошла ошибка. Попробуйте позже.",
                    reply_markup=get_main_menu_keyboard()
                )
            elif update.callback_query:
                await retry_telegram_api(update.callback_query.answer, text="❌ Произошла ошибка. Попробуйте позже.", show_alert=True)
        except:
            pass
        return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command - send guide file"""
    try:
        user_id = update.effective_user.id
        logger.info(f"[HELP] Help command received from user {user_id}")
        
        # Path to the guide file (relative to the script location)
        guide_path = "BOT_GUIDE.html"
        
        # Check if file exists
        if not os.path.exists(guide_path):
            logger.error(f"[HELP] Guide file not found at {guide_path}")
            await update.message.reply_text(
                "❌ Файл руководства временно недоступен.\n\n"
                "Обратитесь к администратору.",
                reply_markup=get_main_menu_keyboard()
            )
            return
        
        # Send the file as document
        # Telegram will allow user to download and open it
        # Using file path directly - python-telegram-bot will handle file opening
        await retry_telegram_api(
            update.message.reply_document,
            document=guide_path,
            filename="BOT_GUIDE.html",
            caption="📖 Руководство пользователя ClientsBot\n\n"
                   "Скачайте файл и откройте его в браузере для просмотра.",
            reply_markup=get_main_menu_keyboard()
        )
        
        logger.info(f"[HELP] Guide file sent successfully to user {user_id}")
        
    except FileNotFoundError:
        logger.error(f"[HELP] Guide file not found")
        await update.message.reply_text(
            "❌ Файл руководства не найден.\n\n"
            "Обратитесь к администратору.",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"[HELP] Error sending guide file: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Произошла ошибка при отправке руководства.\n\n"
            "Попробуйте позже или обратитесь к администратору.",
            reply_markup=get_main_menu_keyboard()
        )

@rate_limit_handler

async def unknown_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries that don't match any pattern"""
    query = update.callback_query
    if query:
        callback_data = query.data if query.data else ""
        user_id = query.from_user.id if query.from_user else None
        
        # Log initial state for diagnostics
        current_state = context.user_data.get('current_state') if context.user_data else None
        has_conversation_keys = any(
            key.startswith('_conversation_') 
            for key in (context.user_data.keys() if context.user_data else [])
        )
        user_data_store_exists = user_id in user_data_store if user_id else False
        
        logger.info(
            f"[UNKNOWN_CALLBACK] Processing unknown callback: '{callback_data}' for user {user_id}, "
            f"current_state={current_state}, has_conversation_keys={has_conversation_keys}, "
            f"user_data_store_exists={user_data_store_exists}"
        )
        
        # Special handling for check_menu - try to activate ConversationHandler
        if callback_data == "check_menu":
            logger.info(f"[UNKNOWN_CALLBACK] check_menu callback not handled by ConversationHandler, trying to activate for user {user_id}")
            # Answer callback and force fallback to check_menu_callback
            try:
                await retry_telegram_api(query.answer)
            except:
                pass
            try:
                return await check_menu_callback(update, context)
            except Exception as e:
                logger.error(f"[UNKNOWN_CALLBACK] check_menu fallback failed: {e}", exc_info=True)
                try:
                    await retry_telegram_api(
                        query.edit_message_text,
                        text="⚠️ Не удалось открыть меню проверки. Попробуйте снова из главного меню.",
                        reply_markup=get_main_menu_keyboard()
                    )
                except:
                    pass
                return ConversationHandler.END
        
        # Special handling for add flow callbacks - try to activate ConversationHandler
        # Includes all callbacks from add flow states (ADD_FULLNAME, ADD_FB_LINK, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW)
        add_flow_callbacks = [
            "add_skip", "add_back", "add_cancel", "add_save", "add_save_force",
            "edit_fullname_from_review", "add_edit_field_fullname", 
            "add_edit_field_telegram_name", "add_edit_field_telegram_id", 
            "add_edit_field_fb_link", "add_edit_back_to_review"
        ]
        if callback_data in add_flow_callbacks:
            logger.info(f"[UNKNOWN_CALLBACK] {callback_data} callback not handled by ConversationHandler, checking for add state for user {user_id}")
            # Check if user has add state initialized
            add_states = {ADD_FULLNAME, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
            if is_facebook_flow_enabled():
                add_states.add(ADD_FB_LINK)
            
            logger.info(
                f"[UNKNOWN_CALLBACK] Checking add state - current_state={current_state}, "
                f"is_add_state={current_state in add_states if current_state else False}, "
                f"user_data_store_exists={user_data_store_exists}"
            )
            
            if current_state in add_states and user_id in user_data_store:
                logger.info(
                    f"[UNKNOWN_CALLBACK] User {user_id} has add state {current_state}, "
                    f"has_conversation_keys={has_conversation_keys}, user_data_store_exists={user_data_store_exists}, "
                    f"trying to activate ConversationHandler for callback: {callback_data}"
                )
                # Обработка всех callbacks в состоянии ADD_REVIEW
                if current_state == ADD_REVIEW and callback_data in [
                    "add_save",
                    "add_back",
                    "add_cancel",
                    "edit_fullname_from_review",
                    "add_edit_field_fullname",
                    "add_edit_field_telegram_name",
                    "add_edit_field_telegram_id",
                    "add_edit_field_fb_link",
                    "add_edit_back_to_review",
                ]:
                    logger.info(f"[UNKNOWN_CALLBACK] Explicitly processing {callback_data} for ADD_REVIEW state via check_add_state_entry_callback")
                    # Answer callback first
                    try:
                        await retry_telegram_api(query.answer)
                    except:
                        pass
                    # Process via check_add_state_entry_callback which will delegate to appropriate callback
                    result = await check_add_state_entry_callback(update, context)
                    logger.info(f"[UNKNOWN_CALLBACK] check_add_state_entry_callback returned: {result}")
                    if result is not None:
                        return result
                    # If check_add_state_entry_callback returned None, let ConversationHandler try
                    logger.info(f"[UNKNOWN_CALLBACK] check_add_state_entry_callback returned None, letting ConversationHandler process")
                    return None
                
                # Clear stale ConversationHandler internal keys
                if context.user_data:
                    keys_to_remove = [key for key in context.user_data.keys() if key.startswith('_conversation_')]
                    logger.info(f"[UNKNOWN_CALLBACK] Clearing {len(keys_to_remove)} stale ConversationHandler keys")
                    for key in keys_to_remove:
                        del context.user_data[key]
                # Answer callback and let ConversationHandler process it
                try:
                    await retry_telegram_api(query.answer)
                except:
                    pass
                # Return None to let ConversationHandler process it
                logger.info(f"[UNKNOWN_CALLBACK] Returning None to let ConversationHandler process callback: {callback_data}")
                return None
            else:
                logger.warning(
                    f"[UNKNOWN_CALLBACK] Cannot activate ConversationHandler for {callback_data} - "
                    f"current_state={current_state} (not in add_states: {current_state not in add_states if current_state else 'None'}), "
                    f"user_data_store_exists={user_data_store_exists}"
                )
                # No valid add state - clear and show main menu
                if user_id:
                    clear_all_conversation_state(context, user_id)
                try:
                    await retry_telegram_api(query.answer, text="⚠️ Сессия истекла. Начните заново.", show_alert=True)
                    if query.message:
                        await retry_telegram_api(
                            query.edit_message_text,
                            text="⚠️ Сессия истекла.\n\nИспользуйте кнопки меню для навигации.",
                            reply_markup=get_main_menu_keyboard()
                        )
                except:
                    pass
                return ConversationHandler.END
        
        # Special handling for edit flow callbacks - try to activate ConversationHandler
        # Includes all callbacks from edit flow states (EDIT_PIN, EDIT_MENU, EDIT_FULLNAME, EDIT_FB_LINK, EDIT_TELEGRAM_NAME, EDIT_TELEGRAM_ID, EDIT_MANAGER_NAME)
        edit_flow_callbacks = [
            "edit_field_fullname", "edit_field_telegram_name", "edit_field_telegram_id",
            "edit_field_fb_link", "edit_field_manager", "edit_save", "edit_cancel"
        ]
        # Check if callback matches edit_lead_<id> pattern
        is_edit_lead_entry = callback_data.startswith("edit_lead_") and callback_data.replace("edit_lead_", "").isdigit()
        
        if callback_data in edit_flow_callbacks or is_edit_lead_entry:
            logger.info(f"[UNKNOWN_CALLBACK] {callback_data} callback not handled by ConversationHandler, checking for edit state for user {user_id}")
            # Check if user has edit state initialized
            edit_states = {EDIT_PIN, EDIT_MENU, EDIT_FULLNAME, EDIT_TELEGRAM_NAME, EDIT_TELEGRAM_ID, EDIT_MANAGER_NAME}
            if is_facebook_flow_enabled():
                edit_states.add(EDIT_FB_LINK)
            
            editing_lead_id = context.user_data.get('editing_lead_id') if context.user_data else None
            
            logger.info(
                f"[UNKNOWN_CALLBACK] Checking edit state - current_state={current_state}, "
                f"is_edit_state={current_state in edit_states if current_state else False}, "
                f"editing_lead_id={editing_lead_id}"
            )
            
            # For edit_lead_<id> entry point, always try to activate ConversationHandler
            # For other edit callbacks, check if user is in edit flow
            if is_edit_lead_entry or (current_state in edit_states) or editing_lead_id:
                logger.info(
                    f"[UNKNOWN_CALLBACK] User {user_id} has edit state or editing_lead_id, "
                    f"current_state={current_state}, editing_lead_id={editing_lead_id}, "
                    f"trying to activate ConversationHandler for callback: {callback_data}"
                )
                
                # Clear stale ConversationHandler internal keys
                if context.user_data:
                    keys_to_remove = [key for key in context.user_data.keys() if key.startswith('_conversation_')]
                    logger.info(f"[UNKNOWN_CALLBACK] Clearing {len(keys_to_remove)} stale ConversationHandler keys for edit flow")
                    for key in keys_to_remove:
                        del context.user_data[key]
                
                # Answer callback and let ConversationHandler process it
                try:
                    await retry_telegram_api(query.answer)
                except:
                    pass
                
                # Return None to let ConversationHandler process it
                logger.info(f"[UNKNOWN_CALLBACK] Returning None to let ConversationHandler process edit callback: {callback_data}")
                return None
            else:
                logger.warning(
                    f"[UNKNOWN_CALLBACK] Cannot activate ConversationHandler for {callback_data} - "
                    f"current_state={current_state} (not in edit_states: {current_state not in edit_states if current_state else 'None'}), "
                    f"editing_lead_id={editing_lead_id}"
                )
                # No valid edit state - clear and show main menu
                if user_id:
                    clear_all_conversation_state(context, user_id)
                try:
                    await retry_telegram_api(query.answer, text="⚠️ Сессия истекла. Начните редактирование заново.", show_alert=True)
                    if query.message:
                        await retry_telegram_api(
                            query.edit_message_text,
                            text="⚠️ Сессия редактирования истекла.\n\nИспользуйте кнопки меню для навигации.",
                            reply_markup=get_main_menu_keyboard()
                        )
                except:
                    pass
                return ConversationHandler.END
        else:
            # No valid add state - clear and show main menu
            logger.warning(f"[UNKNOWN_CALLBACK] {callback_data} callback but no valid add/edit state for user {user_id}")
            if user_id:
                clear_all_conversation_state(context, user_id)
            try:
                await retry_telegram_api(query.answer, text="⚠️ Сессия истекла. Начните заново.", show_alert=True)
                if query.message:
                    await retry_telegram_api(
                        query.edit_message_text,
                        text="⚠️ Сессия истекла.\n\nИспользуйте кнопки меню для навигации.",
                        reply_markup=get_main_menu_keyboard()
                    )
            except:
                pass
            return ConversationHandler.END
        
        # Check if we're in a stale ConversationHandler state
        if context.user_data:
            has_conversation_keys = any(key.startswith('_conversation_') for key in context.user_data.keys())
            if has_conversation_keys:
                logger.warning(f"[UNKNOWN_CALLBACK] Stale ConversationHandler state detected for callback '{callback_data}'. Clearing state for user {user_id}")
                if user_id:
                    clear_all_conversation_state(context, user_id)
                # Show message explaining that previous scenario was completed
                try:
                    await retry_telegram_api(query.answer, text="⚠️ Предыдущий сценарий был завершён.", show_alert=True)
                    if query.message:
                        message_text = query.message.text or ""
                        has_main_menu_buttons = "Проверить" in message_text or "Добавить" in message_text
                        if not has_main_menu_buttons:
                            await retry_telegram_api(
                                query.edit_message_text,
                                text="⚠️ <b>Предыдущий сценарий был завершён, начнём сначала.</b>\n\n"
                                "Используйте кнопки меню для навигации.",
                                reply_markup=get_main_menu_keyboard(),
                                parse_mode='HTML'
                            )
                except:
                    pass
                return ConversationHandler.END
        
        # Log the unknown callback for debugging, with state
        log_conversation_state(user_id or -1, context, prefix="[UNKNOWN_CALLBACK_STATE]")
        logger.warning(f"[UNKNOWN_CALLBACK] Unknown callback query: '{callback_data}' from user {user_id}")
        
        try:
            await retry_telegram_api(query.answer, text="⚠️ Эта кнопка больше неактуальна.", show_alert=True)
            if query.message:
                # Check if message already shows main menu to avoid "Message is not modified" error
                message_text = query.message.text or ""
                has_main_menu_buttons = "Проверить" in message_text or "Добавить" in message_text
                
                # Only edit if message doesn't already show main menu
                if not has_main_menu_buttons:
                    await retry_telegram_api(
                        query.edit_message_text,
                        text="⚠️ <b>Эта кнопка больше неактуальна.</b>\n\n"
                        "Пожалуйста, начните сценарий заново из главного меню.",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    # Message already shows main menu, just answer callback
                    logger.info(f"[UNKNOWN_CALLBACK] Message already shows main menu, skipping edit for callback '{callback_data}'")
        except Exception as e:
            logger.error(f"Error in unknown_callback_handler: {e}", exc_info=True)
    return ConversationHandler.END

async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle commands sent during ConversationHandler (except /q, /start, /skip, and /tag)"""
    if not update.message or not update.message.text:
        return
    
    command = update.message.text.strip().split()[0] if update.message.text else ""
    
    # CRITICAL: Never intercept /start, /q, /skip, or /tag - they must be handled by their dedicated handlers
    # /tag should always interrupt current process and start its own flow
    # This prevents issues when ConversationHandler is in a stale state after deploy
    if command in ["/q", "/start", "/skip", "/tag"]:
        logger.warning(f"[UNKNOWN_CMD] Ignoring {command} - should be handled by dedicated handler. Context keys: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        return None  # Return None to let other handlers process it
    
    # Check if we're in a stale ConversationHandler state (has _conversation_ keys but shouldn't)
    has_conversation_keys = any(key.startswith('_conversation_') for key in (context.user_data.keys() if context.user_data else []))
    if has_conversation_keys:
        logger.warning(f"[UNKNOWN_CMD] Command {command} intercepted but ConversationHandler appears stale. Clearing state.")
        user_id = update.effective_user.id
        clear_all_conversation_state(context, user_id)
        # After clearing, let the command be processed by its handler
        return None
    
    # Show message that command is not available during conversation
    try:
        logger.info(f"[UNKNOWN_CMD] Command {command} not available during active conversation")
        await update.message.reply_text(
            f"⚠️ Команда {command} недоступна во время выполнения операции.\n\n"
            "Используйте /q для возврата в главное меню или завершите текущую операцию.",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Error in unknown_command_handler: {e}", exc_info=True)
    
    # Don't end conversation, let user continue or use /q
    return None

# Add callback - new sequential flow
@rate_limit_handler

async def check_add_state_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for add flow when it was initialized by forwarded message.

    If user already has add state (set by handle_forwarded_message or add_field_input),
    we immediately delegate this *same* update to the proper handler so the user
    doesn't have to send the message twice.
    
    IMPORTANT: This should NOT intercept messages if user is in another active ConversationHandler
    (e.g., tag_conv, edit_conv, etc.)
    """
    if not update.message:
        return None
    
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')
    
    # ADD LOGGING
    logger.info(
        f"[CHECK_ADD_STATE_ENTRY] Called for user {user_id}, "
        f"message_text='{update.message.text if update.message and update.message.text else None}', "
        f"current_state={current_state}, "
        f"pin_attempts={context.user_data.get('pin_attempts')}, "
        f"conversation_keys={[k for k in (context.user_data.keys() if context.user_data else []) if k.startswith('_conversation_')]}"
    )
    
    # PRIORITY CHECK: Check for indicators of other active flows BEFORE checking add flow
    # These checks use context.user_data keys that are set during tag/edit flows
    
    # Check for tag flow indicators (even if current_state is not set correctly)
    tag_manager_name = context.user_data.get('tag_manager_name')
    tag_new_tag = context.user_data.get('tag_new_tag')
    # Check if pin_attempts key exists (not just if it's not None, as 0 is valid)
    has_pin_attempts = 'pin_attempts' in context.user_data
    # If user has pin_attempts, they're likely in tag flow (PIN input stage)
    if tag_manager_name or tag_new_tag or has_pin_attempts:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in tag flow "
            f"(tag_manager_name={bool(tag_manager_name)}, tag_new_tag={bool(tag_new_tag)}, has_pin_attempts={has_pin_attempts}), "
            "not intercepting message"
        )
        return None

    # Check for transfer flow indicators
    transfer_from_manager = context.user_data.get('transfer_from_manager')
    transfer_to_manager = context.user_data.get('transfer_to_manager')
    transfer_manager_names = context.user_data.get('transfer_manager_names')
    if transfer_from_manager or transfer_to_manager or transfer_manager_names:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in transfer flow "
            f"(from={bool(transfer_from_manager)}, to={bool(transfer_to_manager)}), "
            "not intercepting message"
        )
        return None
    
    # Check for edit flow indicators (even if current_state is not set correctly)
    editing_lead_id = context.user_data.get('editing_lead_id')
    if editing_lead_id:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in edit flow "
            f"(editing_lead_id={editing_lead_id}), not intercepting message"
        )
        return None
    
    # Check if user is in another active ConversationHandler by state
    # Tag flow states - INCLUDING TAG_PIN to prevent intercepting PIN input!
    tag_states = {TAG_PIN, TAG_SELECT_MANAGER, TAG_ENTER_NEW}
    if current_state in tag_states:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in tag flow (state={current_state}), "
            "not intercepting message"
        )
        return None

    # Transfer flow states
    transfer_states = {TRANSFER_PIN, TRANSFER_SELECT_FROM, TRANSFER_SELECT_TO, TRANSFER_CONFIRM}
    if current_state in transfer_states:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in transfer flow (state={current_state}), "
            "not intercepting message"
        )
        return None
    
    # Edit flow states
    edit_states = {EDIT_PIN, EDIT_MENU, EDIT_FULLNAME, EDIT_TELEGRAM_NAME, EDIT_TELEGRAM_ID, EDIT_MANAGER_NAME}
    if is_facebook_flow_enabled():
        edit_states.add(EDIT_FB_LINK)
    if current_state in edit_states:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in edit flow (state={current_state}), "
            "not intercepting message"
        )
        return None
    
    # Conversation states that belong to the add flow
    add_states = {ADD_FULLNAME, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    if is_facebook_flow_enabled():
        add_states.add(ADD_FB_LINK)
    
    # If user has add data and current_state points to add flow – handle this update there
    if user_id in user_data_store and current_state in add_states:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} has existing add state {current_state}, "
            "delegating update to add flow handler"
        )
        # If we're already at review stage, just show review
        if current_state == ADD_REVIEW:
            return await show_add_review(update, context)
        # For all other add states, process input via add_field_input
        return await add_field_input(update, context)
    
    # No pre-initialized add state – let other handlers process update
    return None

async def check_add_state_entry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for add flow via callback query when state was initialized by forwarded message.
    
    This allows callback queries (like add_skip, add_back, add_save) to work even when
    the ConversationHandler wasn't explicitly activated via add_new button.
    """
    if not update.callback_query:
        return None
    
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')
    callback_data = update.callback_query.data if update.callback_query.data else ""
    
    # Check for indicators of other active flows BEFORE checking add flow
    tag_manager_name = context.user_data.get('tag_manager_name')
    tag_new_tag = context.user_data.get('tag_new_tag')
    editing_lead_id = context.user_data.get('editing_lead_id')
    
    if tag_manager_name or tag_new_tag or editing_lead_id:
        # User is in tag/edit flow, don't activate add flow
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY_CALLBACK] User {user_id} is in tag/edit flow, "
            "not activating add flow"
        )
        return None

    # Transfer flow indicators
    transfer_from_manager = context.user_data.get('transfer_from_manager')
    transfer_to_manager = context.user_data.get('transfer_to_manager')
    transfer_manager_names = context.user_data.get('transfer_manager_names')
    if transfer_from_manager or transfer_to_manager or transfer_manager_names:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY_CALLBACK] User {user_id} is in transfer flow, "
            "not activating add flow"
        )
        return None
    
    # Conversation states that belong to the add flow
    add_states = {ADD_FULLNAME, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    if is_facebook_flow_enabled():
        add_states.add(ADD_FB_LINK)
    
    # If user has add state, activate ConversationHandler AND process the callback
    if user_id in user_data_store and current_state in add_states:
        # Add check for internal ConversationHandler keys
        has_conversation_keys = any(
            key.startswith('_conversation_') 
            for key in (context.user_data.keys() if context.user_data else [])
        )
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY_CALLBACK] user_id={user_id}, current_state={current_state}, "
            f"has_conversation_keys={has_conversation_keys}, user_data_store_exists={user_id in user_data_store}, "
            f"callback={callback_data}, conversation_keys={[k for k in (context.user_data.keys() if context.user_data else []) if k.startswith('_conversation_')]}"
        )
        
        # Process the callback immediately based on its type
        if callback_data == "add_skip":
            # Delegate to add_skip_callback, which will return the next state
            result = await add_skip_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_back":
            # Delegate to add_back_callback
            result = await add_back_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_cancel":
            # Delegate to add_cancel_callback
            result = await add_cancel_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_save":
            # Delegate to add_save_callback
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] Processing add_save callback for user {user_id} in state {current_state}")
            result = await add_save_callback(update, context)
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] add_save_callback returned: {result}")
            return result if result is not None else ConversationHandler.END
        elif callback_data == "add_save_force":
            # Force save without photo (when photo was lost)
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] Processing add_save_force callback for user {user_id} in state {current_state}")
            # Clear had_photo flag to allow saving without photo
            if user_id in user_data_store:
                user_data_store[user_id].pop('had_photo', None)
            result = await add_save_callback(update, context)
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] add_save_callback (force) returned: {result}")
            return result if result is not None else ConversationHandler.END
        elif callback_data == "edit_fullname_from_review":
            # Delegate to edit_fullname_from_review_callback
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] Processing edit_fullname_from_review callback for user {user_id} in state {current_state}")
            result = await edit_fullname_from_review_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_edit_field_fullname":
            # Delegate to add_edit_field_fullname_from_review_callback
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] Processing add_edit_field_fullname callback for user {user_id} in state {current_state}")
            result = await add_edit_field_fullname_from_review_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_edit_field_telegram_name":
            # Delegate to add_edit_field_telegram_name_from_review_callback
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] Processing add_edit_field_telegram_name callback for user {user_id} in state {current_state}")
            result = await add_edit_field_telegram_name_from_review_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_edit_field_telegram_id":
            # Delegate to add_edit_field_telegram_id_from_review_callback
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] Processing add_edit_field_telegram_id callback for user {user_id} in state {current_state}")
            result = await add_edit_field_telegram_id_from_review_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_edit_field_fb_link":
            # Delegate to add_edit_field_fb_link_from_review_callback
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] Processing add_edit_field_fb_link callback for user {user_id} in state {current_state}")
            result = await add_edit_field_fb_link_from_review_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_edit_back_to_review":
            # Delegate to add_edit_back_to_review_callback
            logger.info(f"[CHECK_ADD_STATE_ENTRY_CALLBACK] Processing add_edit_back_to_review callback for user {user_id} in state {current_state}")
            result = await add_edit_back_to_review_callback(update, context)
            return result if result is not None else current_state
        
        # If callback doesn't match, just activate ConversationHandler
        return current_state
    
    # No pre-initialized add state – let other handlers process callback
    return None


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks for menu navigation"""
    query = update.callback_query
    await retry_telegram_api(query.answer)
    
    data = query.data
    
    # Note: edit_lead_* callbacks are now handled by ConversationHandler (edit_lead_entry_callback)
    
    if data == "main_menu":
        # Get current message ID to exclude from cleanup
        current_message_id = query.message.message_id if query.message else None
        
        # IMPORTANT: Remove current message from cleanup lists BEFORE cleanup
        # This prevents it from being deleted even if cleanup runs quickly
        if current_message_id:
            if 'add_message_ids' in context.user_data and current_message_id in context.user_data['add_message_ids']:
                context.user_data['add_message_ids'].remove(current_message_id)
            if 'last_check_messages' in context.user_data and current_message_id in context.user_data['last_check_messages']:
                context.user_data['last_check_messages'].remove(current_message_id)
        
        try:
            # Show main menu FIRST (fast response)
            await retry_telegram_api(
                query.edit_message_text,
                text="👋 <b>Главное меню</b>\n\n"
                "Вы вернулись в главное меню. Текущие сценарии сброшены.\n\n"
                "Выберите действие:",
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            
            # Clean up messages AFTER showing menu (in background, don't wait)
            # This ensures fast response to user
            # Add small delay to ensure edit completes before cleanup starts
            async def delayed_cleanup():
                import asyncio
                await asyncio.sleep(0.2)  # Small delay to ensure edit completes
                await cleanup_all_messages_before_main_menu(update, context, exclude_message_id=current_message_id)
            
            # Use application.create_task to ensure it runs in the correct event loop
            context.application.create_task(delayed_cleanup())
        except Exception as e:
            # If edit fails (message was deleted), send new message
            if "not found" in str(e) or "BadRequest" in str(type(e).__name__):
                try:
                    await retry_telegram_api(
                        query.message.reply_text,
                        text="👋 Главное меню\n\nВыберите действие:",
                        reply_markup=get_main_menu_keyboard()
                    )
                    # Clean up in background with delay
                    async def delayed_cleanup_fallback():
                        import asyncio
                        await asyncio.sleep(0.2)
                        await cleanup_all_messages_before_main_menu(update, context, exclude_message_id=current_message_id)
                    context.application.create_task(delayed_cleanup_fallback())
                except Exception as reply_error:
                    logger.error(f"Error sending reply message: {reply_error}", exc_info=True)
                    # Last resort: try to send without cleanup
                    try:
                        await retry_telegram_api(
                            context.bot.send_message,
                            chat_id=query.message.chat_id,
                            text="👋 Главное меню\n\nВыберите действие:",
                            reply_markup=get_main_menu_keyboard()
                        )
                    except Exception as send_error:
                        logger.error(f"Error sending message directly: {send_error}", exc_info=True)
            else:
                logger.error(f"Error in main_menu callback: {e}", exc_info=True)
                raise
    
    # check_menu is now handled by smart_check_conv ConversationHandler
    # elif data == "check_menu":
    #     return await check_menu_callback(update, context)
    
    elif data == "add_menu":
        await retry_telegram_api(
            query.edit_message_text,
            text="➕ Добавить клиента\n\nВыберите способ добавления:",
            reply_markup=get_add_menu_keyboard()
        )
    
    elif data == "add_new":
        # Fallback: if ConversationHandler doesn't catch this, handle it here
        logger.warning(f"add_new callback received in button_callback (should be handled by ConversationHandler)")
        try:
            # Try to call add_new_callback directly as fallback
            result = await add_new_callback(update, context)
            if result:
                return result
        except Exception as e:
            logger.error(f"Error in add_new fallback handler: {e}", exc_info=True)
            await retry_telegram_api(query.answer, text="❌ Произошла ошибка. Попробуйте снова.")
            await retry_telegram_api(
                query.edit_message_text,
                text="❌ Произошла ошибка при запуске добавления лида.\n\n"
                "Попробуйте снова или обратитесь к администратору.",
                reply_markup=get_main_menu_keyboard()
            )
    else:
        # Unknown callback data - should not happen, but handle gracefully
        logger.warning(f"Unknown callback data received: {data}")
        await retry_telegram_api(query.answer, text="⚠️ Неизвестная команда. Используйте меню.", show_alert=True)
        try:
            if query.message:
                await retry_telegram_api(
                    query.edit_message_text,
                    text="❌ Неизвестная команда.\n\n"
                    "Используйте кнопки меню для навигации.",
                    reply_markup=get_main_menu_keyboard()
                )
        except Exception as e:
            logger.error(f"Error handling unknown callback: {e}", exc_info=True)

# Global fallback handlers
