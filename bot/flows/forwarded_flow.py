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
from bot.keyboards import get_main_menu_keyboard, get_check_back_keyboard, get_navigation_keyboard
from bot.logging import logger
from bot.state import (
    clear_all_conversation_state,
    user_data_store,
    user_data_store_access_time,
    save_add_message,
    rate_limit_handler,
)
from bot.utils import (
    format_facebook_link_for_display,
    normalize_telegram_id,
    normalize_text_field,
    validate_facebook_link,
    validate_telegram_name,
    get_field_label,
)
from bot.flows.add_flow import get_next_add_field, show_add_review
from bot.flows.check_flow import check_by_extracted_fields

def extract_data_from_forwarded_message(update: Update) -> tuple[dict, list]:
    """
    Extract data from forwarded message.
    Returns (extracted_data dict, extracted_info list for display)
    """
    extracted_data = {}
    extracted_info = []
    
    forward_from = update.message.forward_from
    
    if forward_from:
        # Extract telegram_id
        if forward_from.id:
            telegram_id = normalize_telegram_id(str(forward_from.id))
            if telegram_id:
                extracted_data['telegram_id'] = telegram_id
                extracted_info.append(f"• Telegram ID: {telegram_id}")
                logger.info(f"[EXTRACT_DATA] Extracted telegram_id: {telegram_id}")
        
        # Extract telegram_name
        if forward_from.username:
            is_valid, _, normalized = validate_telegram_name(forward_from.username)
            if is_valid:
                extracted_data['telegram_name'] = normalized
                extracted_info.append(f"• Username: @{normalized}")
                logger.info(f"[EXTRACT_DATA] Extracted telegram_name: {normalized}")
        
        # Extract fullname
        first_name = forward_from.first_name or ""
        last_name = forward_from.last_name or ""
        if first_name or last_name:
            if last_name:
                fullname = f"{first_name} {last_name}".strip()
            else:
                fullname = first_name
            normalized_fullname = normalize_text_field(fullname)
            if normalized_fullname:
                extracted_data['fullname'] = normalized_fullname
                extracted_info.append(f"• Имя: {normalized_fullname}")
                logger.info(f"[EXTRACT_DATA] Extracted fullname: {normalized_fullname}")
    
    # Extract Facebook link from text/caption
    if update.message.text:
        text = update.message.text.strip()
        is_valid_fb, _, fb_normalized = validate_facebook_link(text)
        if is_valid_fb:
            extracted_data['facebook_link'] = fb_normalized
            extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
            logger.info(f"[EXTRACT_DATA] Extracted facebook_link from text: {fb_normalized}")
    
    if update.message.caption and 'facebook_link' not in extracted_data:
        caption = update.message.caption.strip()
        is_valid_fb, _, fb_normalized = validate_facebook_link(caption)
        if is_valid_fb:
            extracted_data['facebook_link'] = fb_normalized
            extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
            logger.info(f"[EXTRACT_DATA] Extracted facebook_link from caption: {fb_normalized}")
    
    # Extract photo
    if update.message.photo:
        largest_photo = update.message.photo[-1]
        photo_file_id = largest_photo.file_id
        extracted_data['photo_file_id'] = photo_file_id
        extracted_data['had_photo'] = True  # Mark that photo was extracted
        extracted_info.append("• Фото: обнаружено (будет загружено при сохранении)")
        logger.info(f"[EXTRACT_DATA] Extracted photo_file_id: {photo_file_id}, marked had_photo=True")
    
    return extracted_data, extracted_info

async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle forwarded messages globally - extract data and start add flow if needed"""
    if not update.message:
        return None  # Not a message update
    
    user_id = update.effective_user.id
    
    # Log message attributes for debugging
    has_photo = bool(update.message.photo)
    has_text = bool(update.message.text)
    has_caption = bool(update.message.caption)
    forward_from = update.message.forward_from
    forward_from_chat = update.message.forward_from_chat
    forward_sender_name = update.message.forward_sender_name
    
    logger.info(
        f"[FORWARD_GLOBAL] Message from user {user_id}: "
        f"photo={has_photo}, text={has_text}, caption={has_caption}, "
        f"forward_from={bool(forward_from)}, forward_from_chat={bool(forward_from_chat)}, "
        f"forward_sender_name={bool(forward_sender_name)}"
    )
    
    # Check if message is forwarded
    # Include photo-only messages if they have forwarding indicators
    is_forwarded = (forward_from is not None or 
                    forward_from_chat is not None or 
                    forward_sender_name is not None)
    
    # Special case: if message has photo but no text/caption, and has forwarding indicators,
    # treat it as forwarded even if forward_from is None (privacy settings)
    if not is_forwarded and has_photo and not has_text and not has_caption:
        # Check if there are any forwarding indicators (even if forward_from is None)
        if forward_from_chat is not None or forward_sender_name is not None:
            is_forwarded = True
            logger.info(f"[FORWARD_GLOBAL] Photo-only forwarded message detected (privacy settings hide forward_from)")
    
    if not is_forwarded:
        logger.info(f"[FORWARD_GLOBAL] Not a forwarded message, skipping")
        return None  # Not a forwarded message, let other handlers process it
    
    logger.info(f"[FORWARD_GLOBAL] Forwarded message detected from user {user_id}")
    
    # Check if user is in another active ConversationHandler (edit, tag, or add flow)
    current_state = context.user_data.get('current_state')
    
    # Edit flow states
    edit_states = {EDIT_PIN, EDIT_MENU, EDIT_FULLNAME, EDIT_TELEGRAM_NAME, EDIT_TELEGRAM_ID, EDIT_MANAGER_NAME}
    if is_facebook_flow_enabled():
        edit_states.add(EDIT_FB_LINK)
    # Tag flow states
    tag_states = {TAG_SELECT_MANAGER, TAG_ENTER_NEW}
    # Add flow states
    add_states = {ADD_FULLNAME, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    if is_facebook_flow_enabled():
        add_states.add(ADD_FB_LINK)
    
    # Check if user is in edit flow
    if current_state in edit_states or context.user_data.get('editing_lead_id'):
        logger.info(f"[FORWARD_GLOBAL] User {user_id} is in edit flow (state={current_state}), ignoring forwarded message")
        await update.message.reply_text(
            "⚠️ Вы находитесь в процессе редактирования лида.\n\n"
            "Завершите редактирование или отмените его, прежде чем добавлять новый лид.",
            reply_markup=get_main_menu_keyboard()
        )
        return None
    
    # Check if user is in tag flow
    if current_state in tag_states:
        logger.info(f"[FORWARD_GLOBAL] User {user_id} is in tag flow (state={current_state}), ignoring forwarded message")
        await update.message.reply_text(
            "⚠️ Вы находитесь в процессе изменения тега менеджера.\n\n"
            "Завершите операцию или отмените её, прежде чем добавлять новый лид.",
            reply_markup=get_main_menu_keyboard()
        )
        return None
    
    # Check if user is already in the process of adding a lead
    # If yes, extract data and go directly to Review
    if user_id in user_data_store:
        # Check if user is in add flow (has current_field or current_state in add_states)
        if context.user_data.get('current_field') or current_state in add_states:
            # User is in add flow - extract data and go directly to Review
            logger.info(f"[FORWARD_GLOBAL] User {user_id} is in add flow, extracting data and going to Review")
            
            # Check if forward_from is available
            if update.message.forward_from is None:
                # Privacy settings - can only extract photo and Facebook link
                extracted_data = {}
                if update.message.photo:
                    largest_photo = update.message.photo[-1]
                    photo_file_id = largest_photo.file_id
                    extracted_data['photo_file_id'] = photo_file_id
                    extracted_data['had_photo'] = True  # Mark that photo was extracted
                
                if update.message.caption:
                    caption = update.message.caption.strip()
                    is_valid_fb, _, fb_normalized = validate_facebook_link(caption)
                    if is_valid_fb:
                        extracted_data['facebook_link'] = fb_normalized
            else:
                # Extract full data
                extracted_data, _ = extract_data_from_forwarded_message(update)
            
            # Save extracted data to user_data_store
            for key, value in extracted_data.items():
                user_data_store[user_id][key] = value
            # Mark had_photo if photo was extracted
            if 'photo_file_id' in extracted_data:
                user_data_store[user_id]['had_photo'] = True
            
            context.user_data['forwarded_extracted_data'] = extracted_data
            context.user_data['is_forwarded_message'] = True
            
            # Go directly to Review
            context.user_data['current_state'] = ADD_REVIEW
            context.user_data['current_field'] = 'review'
            # Log state transition for diagnostics
            has_conversation_keys = any(
                key.startswith('_conversation_') 
                for key in (context.user_data.keys() if context.user_data else [])
            )
            conversation_keys_list = [k for k in (context.user_data.keys() if context.user_data else []) if k.startswith('_conversation_')]
            logger.info(
                f"[FORWARD_GLOBAL] Set ADD_REVIEW state for user {user_id}, "
                f"has_conversation_keys={has_conversation_keys}, conversation_keys={conversation_keys_list}, "
                f"user_data_store_keys={list(user_data_store.get(user_id, {}).keys())}, "
                f"ConversationHandler should activate via entry points"
            )
            await show_add_review(update, context)
            return ADD_REVIEW
    
    # Check if user is in check flow (SMART_CHECK_INPUT state)
    # Проверяем как current_state, так и наличие активного ConversationHandler для check flow
    # ConversationHandler для check flow называется smart_check_conv
    has_smart_check_conv = any(
        key.startswith('_conversation_') 
        for key in (context.user_data.keys() if context.user_data else [])
    ) if context.user_data else False

    is_in_check_flow = (
        current_state == SMART_CHECK_INPUT or
        (has_smart_check_conv and user_id not in user_data_store)  # Если ConversationHandler активен для check, но нет add flow
    )
    if is_in_check_flow:
        # User is in check flow - extract data and check immediately
        logger.info(f"[FORWARD_GLOBAL] User {user_id} is in check flow (state={current_state}, has_conv={has_smart_check_conv}), extracting data and checking immediately")
        
        # Check if forward_from is available
        if update.message.forward_from is None:
            # Privacy settings - cannot extract checkable fields
            await update.message.reply_text(
                "⚠️ <b>Данные отправителя недоступны</b> из-за настроек приватности.\n\n"
                "❌ Не удалось извлечь данные для проверки.\n\n"
                "💡 Попробуйте ввести данные вручную через меню проверки.",
                reply_markup=get_check_back_keyboard()
            )
            return ConversationHandler.END
        
        # Extract data
        extracted_data, extracted_info = extract_data_from_forwarded_message(update)
        
        # Extract checkable fields only
        checkable_fields = {}
        if 'fullname' in extracted_data:
            checkable_fields['fullname'] = extracted_data['fullname']
        if 'telegram_name' in extracted_data:
            checkable_fields['telegram_name'] = extracted_data['telegram_name']
        if 'telegram_id' in extracted_data:
            checkable_fields['telegram_id'] = extracted_data['telegram_id']
        
        # Save extracted data
        if user_id not in user_data_store:
            user_data_store[user_id] = {}
            user_data_store_access_time[user_id] = time.time()
        
        for key, value in extracted_data.items():
            user_data_store[user_id][key] = value
        
        context.user_data['forwarded_extracted_data'] = extracted_data
        
        # Check immediately if we have checkable fields
        if checkable_fields:
            await check_by_extracted_fields(update, context, checkable_fields)
        else:
            await update.message.reply_text(
                "❌ Не удалось извлечь данные для проверки из пересланного сообщения.\n\n"
                "Попробуйте ввести данные вручную.",
                reply_markup=get_check_back_keyboard()
            )
            # Clear state if no checkable fields
            clear_all_conversation_state(context, user_id)
        
        return ConversationHandler.END
    
    # User is not in any active flow - extract data and show action choice
    # Check if forward_from is available (privacy settings may hide it)
    if update.message.forward_from is None:
        # Privacy settings hide the sender info
        clear_all_conversation_state(context, user_id)
        # Protect photo_file_id from being lost if user_data_store is recreated
        saved_photo_file_id = None
        if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
            saved_photo_file_id = user_data_store[user_id]['photo_file_id']
            logger.info(f"[FORWARD_GLOBAL] Preserving photo_file_id (privacy mode): {saved_photo_file_id}")
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
        # Restore photo_file_id if it was saved
        if saved_photo_file_id:
            user_data_store[user_id]['photo_file_id'] = saved_photo_file_id
            logger.info(f"[FORWARD_GLOBAL] Restored photo_file_id (privacy mode): {saved_photo_file_id}")
        
        # Extract data (privacy mode - limited data available)
        extracted_data = {}
        extracted_info = []
        
        # Extract photo if available (even if forward_from is None)
        if update.message.photo:
            # Get largest photo (last in the list)
            largest_photo = update.message.photo[-1]
            photo_file_id = largest_photo.file_id
            extracted_data['photo_file_id'] = photo_file_id
            extracted_info.append("• Фото: обнаружено (будет загружено при сохранении)")
            logger.info(f"[FORWARD_GLOBAL] Extracted photo_file_id (privacy mode) for user {user_id}: {photo_file_id}")
        
        # Parse caption for Facebook link (if available)
        if update.message.caption:
            caption = update.message.caption.strip()
            is_valid_fb, _, fb_normalized = validate_facebook_link(caption)
            if is_valid_fb:
                extracted_data['facebook_link'] = fb_normalized
                extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
                logger.info(f"[FORWARD_GLOBAL] Extracted facebook_link from caption (privacy mode): {fb_normalized}")
        
        # Save extracted data
        for key, value in extracted_data.items():
            user_data_store[user_id][key] = value
        context.user_data['forwarded_extracted_data'] = extracted_data
        
        # Check if we have any fields for checking (fullname, telegram_name, telegram_id)
        has_checkable_fields = any(field in extracted_data for field in ['fullname', 'telegram_name', 'telegram_id'])
        
        # Build message
        if extracted_info:
            info_text = "\n".join(extracted_info)
            message = f"⚠️ <b>Данные отправителя недоступны</b> из-за настроек приватности.\n\n✅ <b>Извлечено из сообщения:</b>\n\n{info_text}\n\n💡 <b>Выберите действие:</b>"
        else:
            message = "⚠️ <b>Данные отправителя недоступны</b> из-за настроек приватности.\n\n❌ Не удалось извлечь данные из пересланного сообщения.\n\n💡 <b>Выберите действие:</b>"
        
        # Build keyboard
        keyboard = []
        if has_checkable_fields:
            keyboard.append([
                InlineKeyboardButton("➕ Добавить", callback_data="forwarded_add"),
                InlineKeyboardButton("✅ Проверить", callback_data="forwarded_check")
            ])
        else:
            keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="forwarded_add")])
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
        
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return None
    else:
        forward_from = update.message.forward_from
        
        # Check if it's a bot
        if forward_from.is_bot:
            await update.message.reply_text(
                "❌ Недоступно. Аккаунт является ботом.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Extract data from forward_from and show action choice
        clear_all_conversation_state(context, user_id)
        # Protect photo_file_id from being lost if user_data_store is recreated
        saved_photo_file_id = None
        if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
            saved_photo_file_id = user_data_store[user_id]['photo_file_id']
            logger.info(f"[FORWARD_GLOBAL] Preserving photo_file_id: {saved_photo_file_id}")
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
        # Restore photo_file_id if it was saved
        if saved_photo_file_id:
            user_data_store[user_id]['photo_file_id'] = saved_photo_file_id
            logger.info(f"[FORWARD_GLOBAL] Restored photo_file_id: {saved_photo_file_id}")
        
        # Extract data using helper function
        extracted_data, extracted_info = extract_data_from_forwarded_message(update)
        
        # Save extracted data to user_data_store
        for key, value in extracted_data.items():
            user_data_store[user_id][key] = value
        
        # Update access time
        user_data_store_access_time[user_id] = time.time()
        
        # Save extracted data in context for callback handlers
        context.user_data['forwarded_extracted_data'] = extracted_data
        
        logger.info(f"[FORWARD_GLOBAL] Saved extracted data to user_data_store for user {user_id}: {list(extracted_data.keys())}")
        
        # Check if we have any fields for checking (fullname, telegram_name, telegram_id)
        has_checkable_fields = any(field in extracted_data for field in ['fullname', 'telegram_name', 'telegram_id'])
        
        # Build message
        if extracted_info:
            info_text = "\n".join(extracted_info)
            message = f"✅ <b>Данные извлечены</b> из пересланного сообщения:\n\n{info_text}\n\n💡 <b>Выберите действие:</b>"
        else:
            message = "⚠️ <b>Не удалось извлечь данные</b> из пересланного сообщения.\n\n💡 <b>Выберите действие:</b>"
        
        # Build keyboard
        keyboard = []
        if has_checkable_fields:
            keyboard.append([
                InlineKeyboardButton("➕ Добавить", callback_data="forwarded_add"),
                InlineKeyboardButton("✅ Проверить", callback_data="forwarded_check")
            ])
        else:
            keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="forwarded_add")])
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
        
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return None

@rate_limit_handler

async def forwarded_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Add' button for forwarded message - go directly to Review"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    logger.info(f"[FORWARDED_ADD] User {user_id} chose to add forwarded message")
    
    # Get extracted data from context or user_data_store
    extracted_data = context.user_data.get('forwarded_extracted_data', {})
    if not extracted_data and user_id in user_data_store:
        # Fallback: get from user_data_store
        extracted_data = {k: v for k, v in user_data_store[user_id].items() 
                         if k in ['fullname', 'telegram_name', 'telegram_id', 'facebook_link', 'photo_file_id']}
    
    # Ensure user_data_store has the extracted data
    # Protect photo_file_id from being lost if user_data_store is recreated
    saved_photo_file_id = None
    if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
        saved_photo_file_id = user_data_store[user_id]['photo_file_id']
        logger.info(f"[FORWARDED_ADD] Preserving photo_file_id: {saved_photo_file_id}")
    
    if user_id not in user_data_store:
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
    
    # Restore photo_file_id if it was saved
    if saved_photo_file_id and 'photo_file_id' not in user_data_store[user_id]:
        user_data_store[user_id]['photo_file_id'] = saved_photo_file_id
        logger.info(f"[FORWARDED_ADD] Restored photo_file_id: {saved_photo_file_id}")
    
    # Save extracted data to user_data_store if not already there
    for key, value in extracted_data.items():
        if key not in user_data_store[user_id] or not user_data_store[user_id][key]:
            user_data_store[user_id][key] = value

    # If fullname is missing, ask for it instead of going to review
    if not user_data_store[user_id].get('fullname'):
        _, _, current_step, total_steps = get_next_add_field('')
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        field_label = get_field_label('fullname')

        context.user_data['current_state'] = ADD_FULLNAME
        context.user_data['current_field'] = 'fullname'
        context.user_data['add_step'] = 0

        await query.edit_message_text(
            f"{progress_text}📝 Введите {field_label}:",
            reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
            parse_mode='HTML'
        )
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        return ADD_FULLNAME
    
    # Mark as forwarded message
    context.user_data['is_forwarded_message'] = True
    
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
        f"[FORWARDED_ADD] Set ADD_REVIEW state for user {user_id}, "
        f"has_conversation_keys={has_conversation_keys}, conversation_keys={conversation_keys_list}, "
        f"user_data_store_keys={list(user_data_store.get(user_id, {}).keys())}, "
        f"ConversationHandler should activate via entry points"
    )
    
    # Show review immediately
    await show_add_review(update, context)
    
    return ADD_REVIEW

async def forwarded_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Check' button for forwarded message - check by extracted fields"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    logger.info(f"[FORWARDED_CHECK] User {user_id} chose to check forwarded message")
    
    # Get extracted data from context or user_data_store
    extracted_data = context.user_data.get('forwarded_extracted_data', {})
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
            "❌ Не удалось извлечь данные для проверки из пересланного сообщения.\n\n"
            "Попробуйте добавить лид вручную.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Use check_by_extracted_fields function
    await check_by_extracted_fields(update, context, checkable_fields)
    
    return ConversationHandler.END
