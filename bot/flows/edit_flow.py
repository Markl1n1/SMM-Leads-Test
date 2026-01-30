import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import PIN_CODE, TABLE_NAME, is_facebook_flow_enabled
from bot.constants import (
    EDIT_PIN,
    EDIT_MENU,
    EDIT_FULLNAME,
    EDIT_FB_LINK,
    EDIT_TELEGRAM_NAME,
    EDIT_TELEGRAM_ID,
    EDIT_MANAGER_NAME,
    SMART_CHECK_INPUT,
)
from bot.keyboards import get_main_menu_keyboard, get_edit_field_keyboard
from bot.logging import logger
from bot.services.supabase_client import get_supabase_client
from bot.services.leads_repo import ensure_lead_identifiers_unique, UNIQUENESS_FIELD_LABELS
from bot.state import clear_all_conversation_state, user_data_store, user_data_store_access_time, log_conversation_state
from bot.utils import (
    escape_html,
    format_facebook_link_for_display,
    get_field_label,
    get_user_friendly_error,
    validate_facebook_link,
    validate_telegram_id,
    validate_telegram_name,
)

async def edit_lead_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, lead_id: int):
    """Start editing a lead"""
    query = update.callback_query
    await query.answer()
    
    # Get lead from database
    client = get_supabase_client()
    if not client:
        await query.edit_message_text(
            "❌ Ошибка: Не удалось подключиться к базе данных.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    try:
        response = client.table(TABLE_NAME).select("*").eq("id", lead_id).execute()
        if not response.data or len(response.data) == 0:
            await query.edit_message_text(
                "❌ Ошибка: Лид не найден.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        lead = response.data[0]
        user_id = query.from_user.id
        
        # Store lead data for editing
        # Map telegram_user to telegram_name for consistency
        lead_data = lead.copy()
        if 'telegram_user' in lead_data and lead_data.get('telegram_user'):
            # Map telegram_user to telegram_name for consistency in UI
            if 'telegram_name' not in lead_data or not lead_data.get('telegram_name'):
                lead_data['telegram_name'] = lead_data.get('telegram_user')
        
        # Ensure all fields are present (even if None/empty) for proper display
        # This ensures indicators work correctly
        for field in ['fullname', 'manager_name', 'facebook_link', 'telegram_name', 'telegram_id']:
            if field not in lead_data:
                lead_data[field] = None
        
        user_data_store[user_id] = lead_data
        user_data_store_access_time[user_id] = time.time()
        context.user_data['editing_lead_id'] = lead_id
        
        # Reset PIN attempt counter when starting new edit
        context.user_data['pin_attempts'] = 0
        
        # Request PIN code before allowing editing
        message = f"🔒 Для редактирования лида (ID: {lead_id}) требуется PIN-код.\n\nВведите PIN-код:"
        # Use reply_text instead of edit_message_text to ensure ConversationHandler works correctly
        await query.message.reply_text(message)
        return EDIT_PIN
        
    except Exception as e:
        logger.error(f"Error loading lead for editing: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке лида. Попробуйте позже.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END

async def edit_pin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PIN code input for editing"""
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')
    
    # CRITICAL: If user is in check flow, don't intercept the message
    if current_state == SMART_CHECK_INPUT:
        logger.info(
            f"[EDIT_PIN_INPUT] User {user_id} is in check flow (SMART_CHECK_INPUT), "
            "clearing stale edit state and ending conversation"
        )
        # Clear stale edit state
        context.user_data.pop('editing_lead_id', None)
        context.user_data.pop('pin_attempts', None)
        return ConversationHandler.END
    
    # Safety check: if there is no editing_lead_id in context, this is likely a
    # stale PIN flow; do not intercept the message so that other flows can handle it
    if not context.user_data.get('editing_lead_id'):
        logger.info(
            f"[EDIT_PIN_INPUT] Called without editing_lead_id for user {user_id}, "
            "treating as stale PIN flow and ending conversation"
        )
        if 'pin_attempts' in context.user_data:
            del context.user_data['pin_attempts']
        return ConversationHandler.END
    
    # Check if message exists and has text
    if not update.message or not update.message.text:
        if update.message:
            await update.message.reply_text(
                "❌ Ошибка: Неверный формат сообщения. Пожалуйста, введите PIN-код текстом."
            )
        else:
            logger.error("edit_pin_input: update.message is None")
        return EDIT_PIN
    
    text = update.message.text.strip()
    lead_id = context.user_data.get('editing_lead_id')
    
    if not lead_id:
        await update.message.reply_text(
            "❌ Ошибка: ID лида не найден. Пожалуйста, начните редактирование заново.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # PIN code from environment variable
    if text == PIN_CODE:
        # PIN is correct, reset attempt counter
        if 'pin_attempts' in context.user_data:
            del context.user_data['pin_attempts']
        
        # PIN is correct, show edit menu
        # Clear any old field editing state to prevent automatic transitions
        if 'current_field' in context.user_data:
            del context.user_data['current_field']
        if 'current_state' in context.user_data:
            del context.user_data['current_state']
        
        # Always reload lead data to ensure we have the latest from DB
        client = get_supabase_client()
        if client:
            try:
                response = client.table(TABLE_NAME).select("*").eq("id", lead_id).execute()
                if response.data and len(response.data) > 0:
                    lead = response.data[0]
                    lead_data = lead.copy()
                    if 'telegram_user' in lead_data and lead_data.get('telegram_user'):
                        if 'telegram_name' not in lead_data or not lead_data.get('telegram_name'):
                            lead_data['telegram_name'] = lead_data.get('telegram_user')
                    # Ensure all fields are present (even if None)
                    for field in ['fullname', 'manager_name', 'facebook_link', 'telegram_name', 'telegram_id']:
                        if field not in lead_data:
                            lead_data[field] = None
                    
                    # Save original data for comparison (deep copy)
                    context.user_data['original_lead_data'] = lead_data.copy()
                    
                    # Initialize user_data_store with current data
                    user_data_store[user_id] = lead_data.copy()
                    user_data_store_access_time[user_id] = time.time()
                else:
                    await update.message.reply_text(
                        "❌ Ошибка: Лид не найден в базе данных.",
                        reply_markup=get_main_menu_keyboard()
                    )
                    return ConversationHandler.END
            except Exception as e:
                logger.error(f"Error reloading lead data in PIN handler: {e}", exc_info=True)
                await update.message.reply_text(
                    "❌ Ошибка: Не удалось загрузить данные лида.",
                    reply_markup=get_main_menu_keyboard()
                )
                return ConversationHandler.END
        else:
            await update.message.reply_text(
                "❌ Ошибка: Не удалось подключиться к базе данных.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        message = (
            f"✏️ <b>Редактирование лида</b> (ID: {lead_id})\n\n"
            "Выберите поле для редактирования.\n\n"
            "💡 <b>Подсказка:</b> Кнопка «◀️ Назад» вернёт вас к списку полей редактирования."
        )
        await update.message.reply_text(
            message,
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store)
        )
        return EDIT_MENU
    else:
        # PIN is incorrect, increment attempt counter
        pin_attempts = context.user_data.get('pin_attempts', 0) + 1
        context.user_data['pin_attempts'] = pin_attempts
        
        if pin_attempts >= 3:
            # Too many failed attempts, return to main menu
            await update.message.reply_text(
                "❌ Превышено количество попыток ввода PIN-кода (3). Доступ к редактированию заблокирован.",
                reply_markup=get_main_menu_keyboard()
            )
            # Clear editing state
            if 'editing_lead_id' in context.user_data:
                del context.user_data['editing_lead_id']
            if 'pin_attempts' in context.user_data:
                del context.user_data['pin_attempts']
            return ConversationHandler.END
        else:
            # PIN is incorrect, ask again
            remaining_attempts = 3 - pin_attempts
            await update.message.reply_text(
                f"❌ Неверный PIN-код. Осталось попыток: {remaining_attempts}\n\nВведите PIN-код:"
            )
            return EDIT_PIN

# Edit field callbacks (must be defined before create_telegram_app)

async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str, field_label: str, next_state: int):
    """Universal callback for editing field selection"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    # If field is already filled, ask if user wants to change it
    if user_data_store.get(user_id, {}).get(field_name):
        await query.edit_message_text(
            f"📝 {field_label} текущее значение: {user_data_store[user_id][field_name]}\n"
            f"Введите новое значение или отправьте /skip чтобы оставить текущее:"
        )
    else:
        await query.edit_message_text(
            f"📝 Введите {field_label}:\n\n"
            "💡 <b>Подсказка:</b> Кнопка «◀️ Назад» вернёт к списку полей редактирования.",
            parse_mode='HTML'
        )
    
    context.user_data['current_field'] = field_name
    context.user_data['current_state'] = next_state
    return next_state

async def edit_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Universal handler for edit field input"""
    if not update.message or not update.message.text:
        logger.error("edit_field_input: update.message or update.message.text is None")
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    
    # Check for /skip command first
    if update.message.text.strip() == "/skip":
        # User wants to skip changing this field, return to edit menu
        await update.message.reply_text(
            "✏️ Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store)
        )
        # Clear current_field to prevent issues
        if 'current_field' in context.user_data:
            del context.user_data['current_field']
        if 'current_state' in context.user_data:
            del context.user_data['current_state']
        return EDIT_MENU
    
    text = update.message.text.strip()
    
    # Check length for text fields (fullname, manager_name)
    field_name = context.user_data.get('current_field')
    if field_name in ['fullname', 'manager_name'] and len(text) > 500:
        field_label = get_field_label(field_name)
        await update.message.reply_text(
            f"❌ {field_label} слишком длинное (максимум 500 символов).\n\n"
            f"Попробуйте снова:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store)
        )
        return context.user_data.get('current_state', EDIT_MENU)
    
    # Update access time BEFORE cleanup to prevent deletion
    # Update access time BEFORE cleanup to protect from race conditions
    user_data_store_access_time[user_id] = time.time()
    
    # Ensure user_data_store entry exists before cleanup
    if user_id not in user_data_store:
        # Re-initialize from database if missing
        lead_id = context.user_data.get('editing_lead_id')
        if lead_id:
            client = get_supabase_client()
            if client:
                try:
                    response = client.table(TABLE_NAME).select("*").eq("id", lead_id).execute()
                    if response.data and len(response.data) > 0:
                        lead = response.data[0]
                        lead_data = lead.copy()
                        if 'telegram_user' in lead_data and lead_data.get('telegram_user'):
                            if 'telegram_name' not in lead_data or not lead_data.get('telegram_name'):
                                lead_data['telegram_name'] = lead_data.get('telegram_user')
                        for field in ['fullname', 'manager_name', 'facebook_link', 'telegram_name', 'telegram_id']:
                            if field not in lead_data:
                                lead_data[field] = None
                        # Save original data if not already saved
                        if 'original_lead_data' not in context.user_data:
                            context.user_data['original_lead_data'] = lead_data.copy()
                        user_data_store[user_id] = lead_data
                        user_data_store_access_time[user_id] = time.time()
                except Exception as e:
                    logger.error(f"Error reloading lead data: {e}", exc_info=True)
        else:
            # If no lead_id, create empty entry
            user_data_store[user_id] = {}
    
    # Cleanup with exclusion of current user to prevent race conditions
    cleanup_user_data_store(exclude_user_id=user_id)
    
    if not field_name:
        await update.message.reply_text(
            "❌ Ошибка: поле не определено.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Ensure user_data_store[user_id] exists
    if user_id not in user_data_store:
        user_data_store[user_id] = {}
    
    # Validate and normalize based on field type (same logic as add_field_input)
    validation_passed = False
    normalized_value = text
    
    if field_name == 'facebook_link':
        is_valid, error_msg, extracted = validate_facebook_link(text)
        if is_valid:
            validation_passed = True
            normalized_value = extracted
        else:
            await update.message.reply_text(f"❌ {error_msg}\n\nПопробуйте снова:")
            return context.user_data.get('current_state', EDIT_MENU)
    
    elif field_name == 'telegram_name':
        is_valid, error_msg, normalized = validate_telegram_name(text)
        if is_valid:
            validation_passed = True
            normalized_value = normalized
        else:
            await update.message.reply_text(f"❌ {error_msg}\n\nПопробуйте снова:")
            return context.user_data.get('current_state', EDIT_MENU)
    
    elif field_name == 'telegram_id':
        is_valid, error_msg, normalized = validate_telegram_id(text)
        if is_valid:
            validation_passed = True
            normalized_value = normalized
        else:
            await update.message.reply_text(f"❌ {error_msg}\n\nПопробуйте снова:")
            return context.user_data.get('current_state', EDIT_MENU)
    
    else:
        # For other fields (fullname, manager_name), normalize text and check not empty
        if text:
            # Normalize text fields (fullname, manager_name)
            normalized_value = normalize_text_field(text)
            if normalized_value:
                validation_passed = True
            else:
                # Text was only whitespace
                validation_passed = False
        else:
            await update.message.reply_text(f"❌ Поле не может быть пустым.\n\nПопробуйте снова:")
            return context.user_data.get('current_state', EDIT_MENU)
    
    # Save value only if validation passed
    if validation_passed and normalized_value:
        # Ensure user_data_store[user_id] exists before assignment (protection against race conditions)
        if user_id not in user_data_store:
            user_data_store[user_id] = {}
        user_data_store[user_id][field_name] = normalized_value
        # Update access time after saving to protect from cleanup
        user_data_store_access_time[user_id] = time.time()
        
        # Show confirmation message
        field_label = get_field_label(field_name)
        await update.message.reply_text(
            f"✅ <b>{field_label}</b> успешно изменено!\n\n"
            f"Новое значение: <code>{normalized_value}</code>\n\n"
            f"Выберите следующее поле для редактирования:",
            parse_mode='HTML',
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store)
        )
    else:
        # Show edit menu again (validation failed or value is empty)
        await update.message.reply_text(
            "✏️ Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store)
        )
    return EDIT_MENU

async def edit_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save edited lead"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lead_id = context.user_data.get('editing_lead_id')
    
    if not lead_id:
        await query.edit_message_text(
            "❌ Ошибка: ID лида не найден.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Get Supabase client first
    client = get_supabase_client()
    if not client:
        await query.edit_message_text(
            "❌ Ошибка: Не удалось подключиться к базе данных.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Load current data from database for merge logic
    try:
        response = client.table(TABLE_NAME).select("*").eq("id", lead_id).execute()
        if not response.data or len(response.data) == 0:
            await query.edit_message_text(
                "❌ Ошибка: Лид не найден в базе данных.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        current_db_data = response.data[0].copy()
        # Map telegram_user to telegram_name for comparison
        if 'telegram_user' in current_db_data and current_db_data.get('telegram_user'):
            if 'telegram_name' not in current_db_data or not current_db_data.get('telegram_name'):
                current_db_data['telegram_name'] = current_db_data.get('telegram_user')
    except Exception as e:
        logger.error(f"Error loading current lead data in save: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Ошибка: Не удалось загрузить данные лида из базы.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Get user_data (changes made by user)
    user_data = user_data_store.get(user_id, {})
    
    # Validation (same as add_save_callback)
    # Check if fullname is empty or None
    fullname_value = user_data.get('fullname')
    if not fullname_value or (isinstance(fullname_value, str) and not fullname_value.strip()):
        await query.edit_message_text(
            "❌ <b>Ошибка:</b> Имя Фамилия обязателен для заполнения!\n\n"
            "⚠️ Пожалуйста, заполните это поле перед сохранением.\n\n"
            "Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store),
            parse_mode='HTML'
        )
        return EDIT_MENU
    
    # Check if manager_name is empty or None
    manager_value = user_data.get('manager_name')
    if not manager_value or (isinstance(manager_value, str) and not manager_value.strip()):
        await query.edit_message_text(
            "❌ <b>Ошибка:</b> Агент обязателен для заполнения!\n\n"
            "⚠️ Пожалуйста, заполните это поле перед сохранением.\n\n"
            "Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store),
            parse_mode='HTML'
        )
        return EDIT_MENU
    
    # Check if at least one identifier is present
    required_fields = ['telegram_name', 'telegram_id']
    if is_facebook_flow_enabled():
        required_fields.insert(0, 'facebook_link')
    # Also check telegram_user for backward compatibility
    has_identifier = any(user_data.get(field) for field in required_fields) or user_data.get('telegram_user')
    
    if not has_identifier:
        error_msg = "❌ <b>Ошибка:</b> Необходимо указать минимум одно из полей для идентификации клиента:\n\n"
        if is_facebook_flow_enabled():
            error_msg += "• <b>Facebook Ссылка</b> - ссылка на профиль Facebook\n"
        error_msg += "• <b>Тег Telegram</b> - username клиента (минимум 5 символов)\n"
        error_msg += "• <b>Telegram ID</b> - числовой идентификатор (минимум 5 цифр)\n\n"
        error_msg += "Выберите поле для редактирования:"
        await query.edit_message_text(
            error_msg,
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store),
            parse_mode='HTML'
        )
        return EDIT_MENU
    
    # Merge logic: Start with current DB data, apply only changes from user_data_store
    # This ensures unchanged fields are not lost
    update_data = current_db_data.copy()

    # Remove fields that shouldn't be updated
    for field in ['id', 'created_at']:
        update_data.pop(field, None)

    # Apply only fields that were changed by the user
    # Compare with original data to determine what was actually changed
    original_data = context.user_data.get('original_lead_data', {})

    # Fields that can be edited
    editable_fields = ['fullname', 'manager_name', 'facebook_link', 'telegram_name', 'telegram_id']

    for field in editable_fields:
        if field in user_data:
            # Get current value from user_data (what user entered/changed)
            current_value = user_data.get(field)
            # Get original value (when editing started)
            original_value = original_data.get(field)
            # Handle telegram_user/telegram_name mapping for original
            if field == 'telegram_name':
                original_value = original_data.get('telegram_name') or original_data.get('telegram_user')

            # Normalize values for comparison (handle None, empty strings, whitespace)
            def normalize_for_compare(val):
                if val is None:
                    return ''
                if isinstance(val, str):
                    return val.strip()
                return str(val).strip()

            current_normalized = normalize_for_compare(current_value)
            original_normalized = normalize_for_compare(original_value)

            # If value changed, update it
            if current_normalized != original_normalized:
                update_data[field] = current_value

    # Map telegram_name to telegram_user for database (backward compatibility)
    if 'telegram_name' in update_data:
        telegram_name_value = update_data.pop('telegram_name')
        # Only set telegram_user if telegram_name has a value
        if telegram_name_value:
            update_data['telegram_user'] = telegram_name_value
        # If telegram_name is empty, also clear telegram_user
        elif 'telegram_user' in update_data:
            update_data['telegram_user'] = None

    # Build fields_to_check for uniqueness (only non-empty identifiers)
    fields_to_check = {}
    for field_name in ['facebook_link', 'telegram_name', 'telegram_id']:
        field_value = update_data.get(field_name)
        if field_value and str(field_value).strip():
            fields_to_check[field_name] = field_value

    # Run uniqueness check for edit flow (ignore current lead id)
    if fields_to_check:
        is_unique, conflicting_field = ensure_lead_identifiers_unique(client, fields_to_check, current_lead_id=lead_id)
        if not is_unique:
            field_label = UNIQUENESS_FIELD_LABELS.get(conflicting_field, conflicting_field)

            await query.edit_message_text(
                f"❌ <b>Ошибка:</b> {field_label} уже существует в базе данных у другого лида, изменения не были сохранены.\n\n"
                "💡 <b>Что можно сделать:</b>\n"
                "• Изменить значение поля и попробовать снова\n"
                "• Вернуться к выбору поля для редактирования",
                reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}), user_data_store),
                parse_mode='HTML'
            )
            # Остаёмся в edit‑флоу, возвращаемся к меню выбора поля
            return EDIT_MENU
    
    try:

        # This ensures we update all fields that were in user_data
        clean_update_data = {}
        for k, v in update_data.items():
            # Keep all values except None (None means field wasn't set)
            # Empty strings are valid and should be saved
            if v is not None:
                clean_update_data[k] = v
        
        response = client.table(TABLE_NAME).update(clean_update_data).eq("id", lead_id).execute()
        
        if response.data:
            await query.edit_message_text(
                "✅ <b>Лид успешно обновлен!</b>",
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(
                "❌ Ошибка: Данные не были обновлены. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
    
    except Exception as e:
        logger.error(f"Error updating lead: {e}", exc_info=True)
        error_msg = "❌ Произошла ошибка при обновлении данных. Попробуйте позже или обратитесь к администратору."
        await query.edit_message_text(
            error_msg,
            reply_markup=get_main_menu_keyboard()
        )
    
    # Clean up
    if user_id in user_data_store:
        del user_data_store[user_id]
    if user_id in user_data_store_access_time:
        del user_data_store_access_time[user_id]
    if 'editing_lead_id' in context.user_data:
        del context.user_data['editing_lead_id']
    if 'original_lead_data' in context.user_data:
        del context.user_data['original_lead_data']
    
    return ConversationHandler.END

async def edit_lead_entry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for editing a lead - parses lead_id from callback_data"""
    query = update.callback_query
    if not query or not query.data:
        logger.error("edit_lead_entry_callback: query or query.data is None")
        return ConversationHandler.END
    
    data = query.data
    
    # Parse lead_id from callback_data (format: "edit_lead_123")
    try:
        lead_id = int(data.split("_")[-1])
        return await edit_lead_callback(update, context, lead_id)
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing edit_lead callback: {e}")
        await query.answer("❌ Ошибка: Неверный формат запроса.", show_alert=True)
        return ConversationHandler.END

async def edit_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel editing lead"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id in user_data_store:
        del user_data_store[user_id]
    if user_id in user_data_store_access_time:
        del user_data_store_access_time[user_id]
    if 'editing_lead_id' in context.user_data:
        del context.user_data['editing_lead_id']
    if 'original_lead_data' in context.user_data:
        del context.user_data['original_lead_data']
    
    await query.edit_message_text(
        "❌ Редактирование отменено.",
        reply_markup=get_main_menu_keyboard()
    )
    return ConversationHandler.END

# Edit field callbacks

async def edit_field_fullname_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'fullname', 'Клиент', EDIT_FULLNAME)

async def edit_field_fb_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'facebook_link', 'Facebook Ссылка', EDIT_FB_LINK)

async def edit_field_telegram_name_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'telegram_name', 'Тег Telegram', EDIT_TELEGRAM_NAME)

async def edit_field_telegram_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'telegram_id', 'Telegram ID', EDIT_TELEGRAM_ID)

async def edit_field_manager_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'manager_name', 'Manager Name', EDIT_MANAGER_NAME)

