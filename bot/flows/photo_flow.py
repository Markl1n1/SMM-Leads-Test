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
    SMART_CHECK_INPUT,
    EDIT_PIN,
    EDIT_MENU,
    EDIT_FULLNAME,
    EDIT_TELEGRAM_NAME,
    EDIT_TELEGRAM_ID,
    EDIT_MANAGER_NAME,
    EDIT_FB_LINK,
    TAG_SELECT_MANAGER,
    TAG_ENTER_NEW,
)
from bot.keyboards import get_main_menu_keyboard, get_navigation_keyboard, get_check_back_keyboard
from bot.logging import logger
from bot.state import rate_limit_handler, user_data_store, user_data_store_access_time
from bot.utils import (
    escape_html,
    format_facebook_link_for_display,
    get_field_format_requirements,
    get_field_label,
    normalize_text_field,
    validate_facebook_link,
)
from bot.flows.add_flow import get_next_add_field, show_add_review
from bot.flows.check_flow import check_by_fullname, check_by_extracted_fields

def extract_data_from_photo_message(update: Update) -> tuple[dict, list]:
    """
    Extract data from regular photo message (not forwarded).
    Returns (extracted_data dict, extracted_info list for display)
    """
    extracted_data = {}
    extracted_info = []
    
    # Extract fullname from text or caption
    if update.message.text:
        text = update.message.text.strip()
        normalized_fullname = normalize_text_field(text)
        if normalized_fullname:
            extracted_data['fullname'] = normalized_fullname
            extracted_info.append(f"• Имя: {normalized_fullname}")
            logger.info(f"[EXTRACT_PHOTO_DATA] Extracted fullname from text: {normalized_fullname}")
    
    if update.message.caption and 'fullname' not in extracted_data:
        caption = update.message.caption.strip()
        normalized_fullname = normalize_text_field(caption)
        if normalized_fullname:
            extracted_data['fullname'] = normalized_fullname
            extracted_info.append(f"• Имя: {normalized_fullname}")
            logger.info(f"[EXTRACT_PHOTO_DATA] Extracted fullname from caption: {normalized_fullname}")
    
    # Extract Facebook link from text/caption
    if update.message.text:
        text = update.message.text.strip()
        is_valid_fb, _, fb_normalized = validate_facebook_link(text)
        if is_valid_fb:
            extracted_data['facebook_link'] = fb_normalized
            extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
            logger.info(f"[EXTRACT_PHOTO_DATA] Extracted facebook_link from text: {fb_normalized}")
    
    if update.message.caption and 'facebook_link' not in extracted_data:
        caption = update.message.caption.strip()
        is_valid_fb, _, fb_normalized = validate_facebook_link(caption)
        if is_valid_fb:
            extracted_data['facebook_link'] = fb_normalized
            extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
            logger.info(f"[EXTRACT_PHOTO_DATA] Extracted facebook_link from caption: {fb_normalized}")
    
    # Extract photo
    if update.message.photo:
        largest_photo = update.message.photo[-1]
        photo_file_id = largest_photo.file_id
        extracted_data['photo_file_id'] = photo_file_id
        extracted_data['had_photo'] = True  # Mark that photo was extracted
        extracted_info.append("• Фото: обнаружено (будет загружено при сохранении)")
        logger.info(f"[EXTRACT_PHOTO_DATA] Extracted photo_file_id: {photo_file_id}, marked had_photo=True")
    
    return extracted_data, extracted_info

@rate_limit_handler

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular (not forwarded) photo messages - start add lead flow"""
    if not update.message:
        return None
    
    user_id = update.effective_user.id
    logger.info(f"[PHOTO_MESSAGE] Processing photo message from user {user_id}")
    
    # Check if message is forwarded - if yes, let handle_forwarded_message handle it
    is_forwarded = (update.message.forward_from is not None or 
                    update.message.forward_from_chat is not None or 
                    update.message.forward_sender_name is not None)
    if is_forwarded:
        logger.info(f"[PHOTO_MESSAGE] Message is forwarded, delegating to handle_forwarded_message")
        return None  # Let handle_forwarded_message handle forwarded messages
    
    # Check if message has photo
    if not update.message.photo:
        logger.info(f"[PHOTO_MESSAGE] Message has no photo, skipping")
        return None
    
    # Check if user is in another active ConversationHandler
    current_state = context.user_data.get('current_state')
    edit_states = {EDIT_PIN, EDIT_MENU, EDIT_FULLNAME, EDIT_TELEGRAM_NAME, EDIT_TELEGRAM_ID, EDIT_MANAGER_NAME}
    if is_facebook_flow_enabled():
        edit_states.add(EDIT_FB_LINK)
    tag_states = {TAG_SELECT_MANAGER, TAG_ENTER_NEW}
    add_states = {ADD_FULLNAME, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    if is_facebook_flow_enabled():
        add_states.add(ADD_FB_LINK)
    
    # Check if user is in edit flow
    if current_state in edit_states or context.user_data.get('editing_lead_id'):
        logger.info(f"[PHOTO_MESSAGE] User {user_id} is in edit flow, ignoring photo message")
        return None
    
    # Check if user is in tag flow
    if current_state in tag_states:
        logger.info(f"[PHOTO_MESSAGE] User {user_id} is in tag flow, ignoring photo message")
        return None
    
    # Check if user is already in add flow
    # If yes, let ConversationHandler handle it (it has photo handler in states)
    # Проверяем как current_state, так и current_field, и внутренние ключи ConversationHandler
    has_conversation_keys = any(
        key.startswith('_conversation_') 
        for key in (context.user_data.keys() if context.user_data else [])
    )
    is_in_add_flow = (
        (user_id in user_data_store and current_state in add_states) or
        (user_id in user_data_store and context.user_data.get('current_field') in ['fullname', 'facebook_link', 'telegram_name', 'telegram_id', 'review']) or
        (has_conversation_keys and current_state in add_states)
    )
    if is_in_add_flow:
        logger.info(
            f"[PHOTO_MESSAGE] User {user_id} is already in add flow "
            f"(state={current_state}, field={context.user_data.get('current_field')}, "
            f"has_conversation_keys={has_conversation_keys}), letting ConversationHandler handle photo"
        )
        return None
    
    # Check if user is in check flow (SMART_CHECK_INPUT)
    if current_state == SMART_CHECK_INPUT:
        logger.info(
            f"[PHOTO_MESSAGE] User {user_id} is in check flow (SMART_CHECK_INPUT), "
            f"letting ConversationHandler handle photo"
        )
        return None  # Let ConversationHandler handle it (handle_photo_during_check)
    
    # If user is in main menu and photo has no text/caption, return to main menu
    has_text = bool(update.message.text and update.message.text.strip())
    has_caption = bool(update.message.caption and update.message.caption.strip())
    if not has_text and not has_caption:
        await update.message.reply_text(
            "⚠️ Скриншот без текста. Возврат в главное меню.",
            reply_markup=get_main_menu_keyboard()
        )
        return None

    # User is in main menu - show action selection
    # Extract photo and data, show selection menu
    logger.info(f"[PHOTO_MESSAGE] User {user_id} is in main menu, showing action selection")
    
    # Extract photo
    largest_photo = update.message.photo[-1]
    photo_file_id = largest_photo.file_id
    logger.info(f"[PHOTO_MESSAGE] Extracted photo_file_id: {photo_file_id} for user {user_id}")
    
    # Protect photo_file_id from being lost if user_data_store is recreated
    saved_photo_file_id = None
    if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
        saved_photo_file_id = user_data_store[user_id]['photo_file_id']
        logger.info(f"[PHOTO_MESSAGE] Preserving existing photo_file_id: {saved_photo_file_id}")
    
    # Initialize user_data_store for photo
    if user_id not in user_data_store:
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
    
    user_data_store[user_id]['photo_file_id'] = photo_file_id
    user_data_store[user_id]['had_photo'] = True
    
    # Log if old photo_file_id was replaced
    if saved_photo_file_id:
        logger.info(f"[PHOTO_MESSAGE] Old photo_file_id was replaced: {saved_photo_file_id} -> {photo_file_id}")
    
    # Extract data using helper function
    extracted_data, extracted_info = extract_data_from_photo_message(update)
    
    # Save extracted data to user_data_store
    for key, value in extracted_data.items():
        user_data_store[user_id][key] = value
    
    # Update access time
    user_data_store_access_time[user_id] = time.time()
    
    # Save extracted data in context for callback handlers
    context.user_data['photo_extracted_data'] = extracted_data
    
    logger.info(f"[PHOTO_MESSAGE] Saved extracted data to user_data_store for user {user_id}: {list(extracted_data.keys())}")
    
    # Check if we have any fields for checking (fullname, telegram_name, telegram_id)
    has_checkable_fields = any(field in extracted_data for field in ['fullname', 'telegram_name', 'telegram_id'])
    
    # Build message
    if extracted_info:
        info_text = "\n".join(extracted_info)
        message = f"✅ <b>Данные извлечены</b> из фото:\n\n{info_text}\n\n💡 <b>Выберите действие:</b>"
    else:
        message = "✅ <b>Фото получено</b>.\n\n💡 <b>Выберите действие:</b>"
    
    # Build keyboard
    keyboard = []
    if has_checkable_fields:
        keyboard.append([
            InlineKeyboardButton("➕ Добавить", callback_data="photo_add"),
            InlineKeyboardButton("✅ Проверить", callback_data="photo_check")
        ])
    else:
        keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="photo_add")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    
    await update.message.reply_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return None

# Command handlers
@rate_limit_handler

async def handle_photo_during_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages during smart check flow using caption text as fullname search"""
    if not update.message:
        logger.error("[PHOTO_DURING_CHECK] update.message is None")
        return ConversationHandler.END

    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"[PHOTO_DURING_CHECK] Processing photo message from user {user_id}")

    # We are in SMART_CHECK_INPUT state, so we only need to process photos
    if not update.message.photo:
        logger.info(f"[PHOTO_DURING_CHECK] Message has no photo, skipping for user {user_id}")
        return SMART_CHECK_INPUT

    # Extract caption text
    caption = update.message.caption or ""
    caption = caption.strip()

    # If there is no caption, ask user to add text for search
    if not caption:
        logger.info(f"[PHOTO_DURING_CHECK] No caption provided for photo from user {user_id}")
        await update.message.reply_text(
            "⚠️ Для поиска по скриншоту необходимо добавить текст к фото.\n\n"
            "💡 Добавьте текст с именем клиента в подпись к фото и отправьте снова.",
            reply_markup=get_check_back_keyboard()
        )
        return SMART_CHECK_INPUT

    # Validate minimum length for fullname search
    if len(caption) < 3:
        logger.info(
            f"[PHOTO_DURING_CHECK] Caption too short for search (len={len(caption)}) "
            f"for user {user_id}"
        )
        await update.message.reply_text(
            "❌ Текст слишком короткий для поиска.\n\n"
            "💡 Для поиска по имени необходимо минимум 3 символа.\n"
            "Попробуйте добавить более полное имя в подпись к фото.",
            reply_markup=get_check_back_keyboard()
        )
        return SMART_CHECK_INPUT

    # Save photo and caption data to context for potential add flow
    if update.message.photo:
        largest_photo = update.message.photo[-1]
        photo_file_id = largest_photo.file_id
        context.user_data['check_photo_file_id'] = photo_file_id
        context.user_data['check_photo_caption'] = caption
        logger.info(f"[PHOTO_DURING_CHECK] Saved photo_file_id and caption to context for user {user_id}")

    # Call existing fullname search logic, passing caption as override parameter
    logger.info(
        f"[PHOTO_DURING_CHECK] Using caption as fullname search value for user {user_id}: "
        f"'{caption[:50]}...'"
    )
    return await check_by_fullname(update, context, search_value_override=caption)

# Old add_field_callback removed - using sequential flow now

async def handle_photo_during_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages during add flow - save photo and continue"""
    if not update.message:
        logger.warning("[PHOTO_DURING_ADD] update.message is None")
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')
    current_field = context.user_data.get('current_field')
    
    # Добавить проверку внутренних ключей ConversationHandler
    has_conversation_keys = any(
        key.startswith('_conversation_') 
        for key in (context.user_data.keys() if context.user_data else [])
    )
    logger.info(
        f"[PHOTO_DURING_ADD] User {user_id} sent photo during add flow "
        f"(state={current_state}, field={current_field}, has_conversation_keys={has_conversation_keys}, "
        f"user_data_store_exists={user_id in user_data_store})"
    )
    
    # Проверить, что user_data_store существует
    if user_id not in user_data_store:
        logger.warning(f"[PHOTO_DURING_ADD] user_data_store does not exist for user {user_id}, creating it")
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
    
    # Extract photo
    largest_photo = update.message.photo[-1]
    photo_file_id = largest_photo.file_id
    
    # Save photo to user_data_store
    user_data_store[user_id]['photo_file_id'] = photo_file_id
    # Mark that user had a photo, so we can detect if it's lost later
    user_data_store[user_id]['had_photo'] = True
    user_data_store_access_time[user_id] = time.time()
    
    logger.info(f"[PHOTO_DURING_ADD] Saved photo_file_id={photo_file_id} for user {user_id} in user_data_store, marked had_photo=True")
    logger.info(f"[PHOTO_DURING_ADD] After saving photo - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
    logger.info(f"[PHOTO_DURING_ADD] photo_file_id verification: {user_data_store[user_id].get('photo_file_id')}")
    
    # Check if we're on the first step (ADD_FULLNAME) and if there's a caption
    if current_state == ADD_FULLNAME and update.message.caption:
        # Extract and normalize caption text as fullname
        caption_text = update.message.caption.strip()
        normalized_fullname = normalize_text_field(caption_text)
        
        if normalized_fullname:
            # Verify photo_file_id is still there before moving to next step
            if 'photo_file_id' not in user_data_store[user_id]:
                logger.error(f"[PHOTO_DURING_ADD] CRITICAL: photo_file_id was lost before moving to next step for user {user_id}")
            else:
                logger.info(f"[PHOTO_DURING_ADD] photo_file_id confirmed before moving to next step: {user_data_store[user_id]['photo_file_id']}")
            
            # Save fullname to user_data_store
            user_data_store[user_id]['fullname'] = normalized_fullname
            logger.info(f"[PHOTO_DURING_ADD] Extracted fullname from caption: '{normalized_fullname}' for user {user_id}")
            logger.info(f"[PHOTO_DURING_ADD] After saving fullname - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}, photo_file_id={user_data_store[user_id].get('photo_file_id')}")
            
            # Use get_next_add_field to determine next field after fullname (respects FACEBOOK_FLOW)
            # skip_facebook_link=False because this is not a forwarded message
            next_field, next_state, current_step, total_steps = get_next_add_field('fullname', skip_facebook_link=False)
            context.user_data['current_field'] = next_field
            context.user_data['current_state'] = next_state
            context.user_data['add_step'] = current_step - 1
            
            # Get next field info
            field_label = get_field_label(next_field)
            requirements = get_field_format_requirements(next_field)
            
            # Notify user and ask for next field
            await update.message.reply_text(
                f"✅ Фото получено. Имя извлечено из текста: <code>{escape_html(normalized_fullname)}</code>\n\n"
                f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
                f"📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=False),
                parse_mode='HTML'
            )
            
            return ADD_TELEGRAM_NAME
        else:
            # Caption couldn't be normalized, stay on current step
            logger.warning(f"[PHOTO_DURING_ADD] Could not normalize caption text: '{caption_text}' for user {user_id}")
            await update.message.reply_text(
                "✅ <b>Фото сохранено</b> и будет загружено при сохранении лида.\n\n"
                "💡 <b>Продолжайте заполнение полей.</b>"
            )
            return ADD_FULLNAME
    else:
        # No caption or not on first step - just save photo and continue
        await update.message.reply_text(
            "✅ Фото сохранено и будет загружено при сохранении лида.\n\n"
            "Продолжайте заполнение полей."
        )
        
        # Return current state to continue the flow
        return current_state

async def handle_document_during_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document messages during add flow - save photo if it's an image and continue"""
    if not update.message or not update.message.document:
        logger.warning("[DOCUMENT_DURING_ADD] update.message or document is None")
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')
    
    document = update.message.document
    logger.info(f"[DOCUMENT_DURING_ADD] User {user_id} sent document during add flow (state={current_state}, field={context.user_data.get('current_field')}, file_name={document.file_name}, mime_type={document.mime_type})")
    
    # Check if document is an image
    is_image = False
    if document.mime_type:
        is_image = document.mime_type.startswith('image/')
    elif document.file_name:
        file_name_lower = document.file_name.lower()
        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']
        is_image = any(file_name_lower.endswith(ext) for ext in image_extensions)
    
    if not is_image:
        logger.info(f"[DOCUMENT_DURING_ADD] Document is not an image, ignoring (file_name={document.file_name}, mime_type={document.mime_type})")
        await update.message.reply_text(
            "⚠️ <b>Ошибка:</b> Пожалуйста, отправьте изображение (JPG, PNG, GIF, WEBP).\n\n"
            "💡 <b>Продолжайте заполнение полей.</b>"
        )
        return current_state
    
    # Protect photo_file_id from being lost if user_data_store is recreated
    saved_photo_file_id = None
    if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
        saved_photo_file_id = user_data_store[user_id]['photo_file_id']
        logger.info(f"[DOCUMENT_DURING_ADD] Preserving existing photo_file_id: {saved_photo_file_id}")
    
    # Проверить, что user_data_store существует
    if user_id not in user_data_store:
        logger.warning(f"[DOCUMENT_DURING_ADD] user_data_store does not exist for user {user_id}, creating it")
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
        
        # Restore photo_file_id if it was saved
        if saved_photo_file_id:
            user_data_store[user_id]['photo_file_id'] = saved_photo_file_id
            logger.info(f"[DOCUMENT_DURING_ADD] Restored photo_file_id: {saved_photo_file_id}")
    
    # Log current state of user_data_store before saving
    logger.info(f"[DOCUMENT_DURING_ADD] Before saving - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
    
    # Extract document file_id
    document_file_id = document.file_id
    
    # Save document file_id to user_data_store (use same key as photo)
    user_data_store[user_id]['photo_file_id'] = document_file_id
    # Mark that user had a photo, so we can detect if it's lost later
    user_data_store[user_id]['had_photo'] = True
    user_data_store_access_time[user_id] = time.time()
    
    # Log after saving to verify
    logger.info(f"[DOCUMENT_DURING_ADD] After saving - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}, photo_file_id={document_file_id}")
    
    logger.info(f"[DOCUMENT_DURING_ADD] Saved photo_file_id={document_file_id} for user {user_id} in user_data_store, marked had_photo=True")
    
    # Notify user that photo was saved
    await update.message.reply_text(
        "✅ Фото сохранено и будет загружено при сохранении лида.\n\n"
        "Продолжайте заполнение полей."
    )
    
    # Return current state to continue the flow
    return current_state

async def photo_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Add' button for photo message - go directly to Review"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    logger.info(f"[PHOTO_ADD] User {user_id} chose to add photo message")
    
    # Get extracted data from context or user_data_store
    extracted_data = context.user_data.get('photo_extracted_data', {})
    if not extracted_data and user_id in user_data_store:
        # Fallback: get from user_data_store
        extracted_data = {k: v for k, v in user_data_store[user_id].items() 
                         if k in ['fullname', 'telegram_name', 'telegram_id', 'facebook_link', 'photo_file_id']}
    
    # Ensure user_data_store has the extracted data
    # Protect photo_file_id from being lost if user_data_store is recreated
    saved_photo_file_id = None
    if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
        saved_photo_file_id = user_data_store[user_id]['photo_file_id']
        logger.info(f"[PHOTO_ADD] Preserving photo_file_id: {saved_photo_file_id}")
    
    if user_id not in user_data_store:
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
    
    # Restore photo_file_id if it was saved
    if saved_photo_file_id and 'photo_file_id' not in user_data_store[user_id]:
        user_data_store[user_id]['photo_file_id'] = saved_photo_file_id
        logger.info(f"[PHOTO_ADD] Restored photo_file_id: {saved_photo_file_id}")
    
    # Save extracted data to user_data_store if not already there
    for key, value in extracted_data.items():
        if key not in user_data_store[user_id] or not user_data_store[user_id][key]:
            user_data_store[user_id][key] = value
    
    # Set state to ADD_REVIEW
    context.user_data['current_state'] = ADD_REVIEW
    context.user_data['current_field'] = 'review'
    
    # Log state transition for diagnostics
    has_conversation_keys = any(
        key.startswith('_conversation_') 
        for key in (context.user_data.keys() if context.user_data else [])
    )
    conversation_keys_list = [k for k in (context.user_data.keys() if context.user_data else []) if k.startswith('_conversation_')]
    logger.info(
        f"[PHOTO_ADD] Set ADD_REVIEW state for user {user_id}, "
        f"has_conversation_keys={has_conversation_keys}, conversation_keys={conversation_keys_list}, "
        f"user_data_store_keys={list(user_data_store.get(user_id, {}).keys())}, "
        f"ConversationHandler should activate via entry points"
    )
    
    # Show review immediately
    await show_add_review(update, context)
    
    return ADD_REVIEW

async def photo_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Check' button for photo message - check by extracted fields"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    logger.info(f"[PHOTO_CHECK] User {user_id} chose to check photo message")
    
    # Get extracted data from context or user_data_store
    extracted_data = context.user_data.get('photo_extracted_data', {})
    if not extracted_data and user_id in user_data_store:
        # Fallback: get from user_data_store
        extracted_data = {k: v for k, v in user_data_store[user_id].items() 
                         if k in ['fullname', 'telegram_name', 'telegram_id']}
    
    # Extract checkable fields
    checkable_fields = {}
    if 'fullname' in extracted_data and extracted_data['fullname']:
        checkable_fields['fullname'] = extracted_data['fullname']
    if 'telegram_name' in extracted_data and extracted_data['telegram_name']:
        checkable_fields['telegram_name'] = extracted_data['telegram_name']
    if 'telegram_id' in extracted_data and extracted_data['telegram_id']:
        checkable_fields['telegram_id'] = extracted_data['telegram_id']
    
    if not checkable_fields:
        # No fields to check
        await query.edit_message_text(
            "❌ Не удалось извлечь данные для проверки из фото.\n\n"
            "Попробуйте добавить лид вручную.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Use check_by_extracted_fields function
    await check_by_extracted_fields(update, context, checkable_fields)
    
    return ConversationHandler.END
