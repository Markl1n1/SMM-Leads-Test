import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from bot.config import TABLE_NAME, is_facebook_flow_enabled, is_minimal_add_mode_enabled
from bot.constants import (
    ADD_FULLNAME,
    ADD_FB_LINK,
    ADD_TELEGRAM_NAME,
    ADD_TELEGRAM_ID,
    ADD_REVIEW,
)
from bot.keyboards import get_main_menu_keyboard, get_navigation_keyboard
from bot.logging import logger
from bot.services.leads_repo import (
    check_duplicate_realtime,
    check_fields_uniqueness_batch,
    UNIQUENESS_FIELD_LABELS,
)
from bot.services.photos import upload_lead_photo_to_supabase
from bot.services.supabase_client import get_supabase_client
from bot.state import (
    clear_all_conversation_state,
    cleanup_add_messages,
    cleanup_user_data_store,
    log_conversation_state,
    save_add_message,
    user_data_store,
    user_data_store_access_time,
)
from bot.utils import (
    escape_html,
    format_facebook_link_for_display,
    get_field_format_requirements,
    get_field_label,
    get_user_friendly_error,
    normalize_telegram_id,
    normalize_text_field,
    retry_telegram_api,
    validate_facebook_link,
    validate_telegram_name,
)

def is_field_filled(user_data: dict, field_name: str) -> bool:
    """Check if field is filled (exists and has non-empty value)"""
    value = user_data.get(field_name)
    return value is not None and value != '' and str(value).strip() != ''

def get_next_add_field(current_field: str, skip_facebook_link: bool = False) -> tuple[str, int, int, int]:
    """Get next field in the add flow. Returns (field_name, state, current_step, total_steps)
    
    Args:
        current_field: Current field name
        skip_facebook_link: If True, skip facebook_link field (for forwarded messages)
    """
    field_sequence = [
        ('fullname', ADD_FULLNAME),
    ]
    if is_facebook_flow_enabled():
        field_sequence.append(('facebook_link', ADD_FB_LINK))
    field_sequence.extend([
        ('telegram_name', ADD_TELEGRAM_NAME),
        ('telegram_id', ADD_TELEGRAM_ID),
    ])
    
    # Filter out facebook_link if skip_facebook_link is True or Facebook flow is disabled
    if skip_facebook_link or not is_facebook_flow_enabled():
        field_sequence = [f for f in field_sequence if f[0] != 'facebook_link']
    
    total_steps = len(field_sequence) + 1  # +1 for review step
    
    if not current_field:
        return field_sequence[0][0], field_sequence[0][1], 1, total_steps
    
    # If current_field is facebook_link and we're skipping it, treat it as if we're at fullname
    if current_field == 'facebook_link' and skip_facebook_link:
        current_field = 'fullname'
    
    for i, (field, state) in enumerate(field_sequence):
        if field == current_field:
            if i + 1 < len(field_sequence):
                return field_sequence[i + 1][0], field_sequence[i + 1][1], i + 2, total_steps
            else:
                return ('review', ADD_REVIEW, total_steps, total_steps)
    
    return field_sequence[0][0], field_sequence[0][1], 1, total_steps

async def add_new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start adding new lead - sequential flow"""
    query = update.callback_query
    try:
        await query.answer()
        user_id = query.from_user.id
        
        logger.info(f"[ADD_NEW] Clearing state before entry for user {user_id}")
        
        # Explicitly clear all conversation state including internal ConversationHandler keys
        # This prevents issues when re-entering after /q or stale states after deploy
        clear_all_conversation_state(context, user_id)
        
        # Additional safety: explicitly clear any PIN/tag/edit related flags that
        # could cause PIN handlers to intercept add flow input
        for key in ['pin_attempts', 'tag_manager_name', 'tag_new_tag', 'editing_lead_id']:
            if key in context.user_data:
                del context.user_data[key]
        
        # Protect photo_file_id from being lost if user_data_store is recreated
        # This is important if user accidentally clicks "Add" again during add flow
        saved_photo_file_id = None
        if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
            saved_photo_file_id = user_data_store[user_id]['photo_file_id']
            logger.info(f"[ADD_NEW] Preserving photo_file_id: {saved_photo_file_id}")
        
        # Initialize fresh state for new add flow
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
        
        # Restore photo_file_id if it was saved (user might have accidentally clicked "Add" again)
        if saved_photo_file_id:
            user_data_store[user_id]['photo_file_id'] = saved_photo_file_id
            logger.info(f"[ADD_NEW] Restored photo_file_id: {saved_photo_file_id}")
        context.user_data['current_field'] = 'fullname'
        context.user_data['current_state'] = ADD_FULLNAME  # ЯВНО устанавливаем состояние
        context.user_data['add_step'] = 0
        
        # Start with first field: Full Name
        field_label = get_field_label('fullname')
        _, _, current_step, total_steps = get_next_add_field('')
        
        # Убираем описание для первого шага
        message = f"<b>Шаг {current_step} из {total_steps}</b>\n\n📝 Введите {field_label}:"
        
        await retry_telegram_api(
            query.edit_message_text,
            text=message,
            reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
            parse_mode='HTML'
        )
        # Save message ID for cleanup
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        
        logger.info(f"[ADD_NEW] Returning ADD_FULLNAME for user {user_id}, ConversationHandler should be active")
        # Проверить, что user_data_store существует
        if user_id not in user_data_store:
            logger.error(f"[ADD_NEW] CRITICAL: user_data_store[{user_id}] does not exist after initialization!")
        else:
            logger.info(f"[ADD_NEW] user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        
        return ADD_FULLNAME
    except Exception as e:
        logger.error(f"Error in add_new_callback: {e}", exc_info=True)
        try:
            await query.answer("❌ Произошла ошибка. Попробуйте снова.")
            await query.edit_message_text(
                "❌ Произошла ошибка при запуске добавления лида.\n\n"
                "Попробуйте снова или обратитесь к администратору.",
                reply_markup=get_main_menu_keyboard()
            )
        except Exception as fallback_error:
            logger.error(f"Error in add_new_callback fallback: {fallback_error}", exc_info=True)
        return ConversationHandler.END

async def add_from_check_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add flow from check photo scenario - use saved photo and name"""
    query = update.callback_query
    try:
        await query.answer()
        user_id = query.from_user.id
        
        logger.info(f"[ADD_FROM_CHECK_PHOTO] Starting add flow from check photo for user {user_id}")
        
        # Extract saved photo and caption data from context
        photo_file_id = context.user_data.get('check_photo_file_id')
        caption = context.user_data.get('check_photo_caption')
        
        if not photo_file_id or not caption:
            logger.error(f"[ADD_FROM_CHECK_PHOTO] Missing photo data for user {user_id} (photo_file_id={photo_file_id}, caption={bool(caption)})")
            await query.edit_message_text(
                "❌ Ошибка: данные фото не найдены.\n\n"
                "Попробуйте снова отправить фото с именем.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Normalize caption as fullname
        normalized_fullname = normalize_text_field(caption)
        if not normalized_fullname:
            logger.error(f"[ADD_FROM_CHECK_PHOTO] Failed to normalize caption '{caption}' for user {user_id}")
            await query.edit_message_text(
                "❌ Ошибка: не удалось обработать имя из подписи.\n\n"
                "Попробуйте снова отправить фото с именем.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        logger.info(f"[ADD_FROM_CHECK_PHOTO] Extracted data: photo_file_id={photo_file_id}, fullname='{normalized_fullname}'")
        
        # Clear all conversation state
        clear_all_conversation_state(context, user_id)
        
        # Additional safety: explicitly clear any PIN/tag/edit related flags that
        # could cause PIN handlers to intercept add flow input
        for key in ['pin_attempts', 'tag_manager_name', 'tag_new_tag', 'editing_lead_id']:
            if key in context.user_data:
                del context.user_data[key]
        
        # Initialize user_data_store with saved photo and name
        user_data_store[user_id] = {
            'photo_file_id': photo_file_id,
            'fullname': normalized_fullname
        }
        user_data_store_access_time[user_id] = time.time()
        
        # Clean up temporary check photo data from context
        if 'check_photo_file_id' in context.user_data:
            del context.user_data['check_photo_file_id']
        if 'check_photo_caption' in context.user_data:
            del context.user_data['check_photo_caption']
        
        # Use get_next_add_field to determine next field after fullname (respects FACEBOOK_FLOW)
        # skip_facebook_link=False because this is not a forwarded message
        next_field, next_state, current_step, total_steps = get_next_add_field('fullname', skip_facebook_link=False)
        context.user_data['current_field'] = next_field
        context.user_data['current_state'] = next_state
        context.user_data['add_step'] = current_step - 1
        
        # Get next field info
        field_label = get_field_label(next_field)
        requirements = get_field_format_requirements(next_field)
        
        # Show message with saved name and prompt for next field
        message = (
            f"✅ Имя извлечено из подписи: <code>{escape_html(normalized_fullname)}</code>\n\n"
            f"✅ Фото сохранено и будет загружено при сохранении лида.\n\n"
            f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
            f"📝 Введите {field_label}:\n\n{requirements}"
        )
        
        await retry_telegram_api(
            query.edit_message_text,
            text=message,
            reply_markup=get_navigation_keyboard(is_optional=True, show_back=False),
            parse_mode='HTML'
        )
        
        # Save message ID for cleanup
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        
        logger.info(f"[ADD_FROM_CHECK_PHOTO] Returning ADD_TELEGRAM_NAME for user {user_id}, user_data_store keys: {list(user_data_store[user_id].keys())}")
        
        return ADD_TELEGRAM_NAME
        
    except Exception as e:
        logger.error(f"Error in add_from_check_photo_callback: {e}", exc_info=True)
        try:
            await query.answer("❌ Произошла ошибка. Попробуйте снова.")
            await query.edit_message_text(
                "❌ Произошла ошибка при запуске добавления лида.\n\n"
                "Попробуйте снова или обратитесь к администратору.",
                reply_markup=get_main_menu_keyboard()
            )
        except Exception as fallback_error:
            logger.error(f"Error in add_from_check_photo_callback fallback: {fallback_error}", exc_info=True)
        return ConversationHandler.END

# Search by multiple fields function

async def add_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Universal handler for field input - sequential flow"""
    if not update.message:
        logger.error("add_field_input: update.message is None")
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    # Diagnostic: log state when entering add_field_input
    log_conversation_state(user_id, context, prefix="[ADD_FIELD_STATE]")
    
    # PRIORITY 1: Check if message is forwarded (before checking text)
    # Check for forwarded message (either forward_from or forward_from_chat or forward_sender_name)
    is_forwarded = (update.message.forward_from is not None or 
                    update.message.forward_from_chat is not None or 
                    update.message.forward_sender_name is not None)
    
    if is_forwarded:
        logger.info(f"[ADD_FIELD] Forwarded message detected from user {user_id}")
        
        # Check if user is in the process of adding a lead
        if user_id in user_data_store:
            # Check if forward_from is available (privacy settings may hide it)
            if update.message.forward_from is None:
                # Privacy settings hide the sender info
                await update.message.reply_text(
                    "⚠️ Данные отправителя недоступны из-за настроек приватности.\n\n"
                    "💡 <b>Продолжайте заполнение полей вручную.</b>"
                )
                # Continue with normal flow
                if not update.message.text:
                    logger.error("add_field_input: update.message.text is None (and forwarded with privacy)")
                    return ConversationHandler.END
                # Fall through to normal text processing below
            else:
                forward_from = update.message.forward_from
                
                # Check if it's a bot
                if forward_from.is_bot:
                    await update.message.reply_text(
                        "❌ Недоступно. Аккаунт является ботом.",
                        reply_markup=get_main_menu_keyboard()
                    )
                    return ConversationHandler.END
                
                # Extract data from forward_from
                extracted_data = {}
                extracted_info = []
                
                # Extract telegram_id (required if available)
                if forward_from.id:
                    telegram_id = normalize_telegram_id(str(forward_from.id))
                    if telegram_id:
                        extracted_data['telegram_id'] = telegram_id
                        extracted_info.append(f"• Telegram ID: {telegram_id}")
                        logger.info(f"[ADD_FIELD] Extracted telegram_id: {telegram_id}")
                
                # Extract telegram_name (if available)
                if forward_from.username:
                    is_valid, _, normalized = validate_telegram_name(forward_from.username)
                    if is_valid:
                        extracted_data['telegram_name'] = normalized
                        extracted_info.append(f"• Username: @{normalized}")
                        logger.info(f"[ADD_FIELD] Extracted telegram_name: {normalized}")
                
                # Extract fullname (Display Name: first_name + last_name)
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
                        logger.info(f"[ADD_FIELD] Extracted fullname: {normalized_fullname}")
                
                # Parse text message for Facebook link (if available)
                if update.message.text:
                    text = update.message.text.strip()
                    is_valid_fb, _, fb_normalized = validate_facebook_link(text)
                    if is_valid_fb:
                        extracted_data['facebook_link'] = fb_normalized
                        extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
                        logger.info(f"[ADD_FIELD] Extracted facebook_link from message text: {fb_normalized}")
                
                # Extract photo if available
                if update.message.photo:
                    # Get largest photo (last in the list)
                    largest_photo = update.message.photo[-1]
                    photo_file_id = largest_photo.file_id
                    extracted_data['photo_file_id'] = photo_file_id
                    extracted_info.append("• Фото: обнаружено (будет загружено при сохранении)")
                    logger.info(f"[ADD_FIELD] Extracted photo_file_id for user {user_id}: {photo_file_id}")
                
                # Save extracted data to user_data_store
                for key, value in extracted_data.items():
                    user_data_store[user_id][key] = value
                
                # Update access time
                user_data_store_access_time[user_id] = time.time()
                
                # Show user what was extracted
                if extracted_info:
                    info_text = "\n".join(extracted_info)
                    await update.message.reply_text(
                        f"✅ <b>Данные извлечены</b> из пересланного сообщения:\n\n{info_text}\n\n"
                        f"💡 <b>Продолжайте заполнение остальных полей.</b>"
                    )
                else:
                    await update.message.reply_text(
                        "⚠️ <b>Не удалось извлечь данные</b> из пересланного сообщения.\n\n"
                        "💡 <b>Продолжайте заполнение полей вручную.</b>"
                    )
                
                # Determine next field to fill - start from beginning and skip all filled fields
                # Start from first field and skip all already filled fields
                # Use skip_facebook_link=True for forwarded messages
                is_forwarded = context.user_data.get('is_forwarded_message', False)
                next_field, next_state, current_step, total_steps = get_next_add_field('', skip_facebook_link=is_forwarded)
                
                # Skip already filled fields AND skip facebook_link for forwarded messages
                while next_field != 'review':
                    # Always skip facebook_link for forwarded messages
                    if next_field == 'facebook_link' and is_forwarded:
                        logger.info(f"[ADD_FIELD] Skipping facebook_link field for forwarded message")
                        next_field, next_state, current_step, total_steps = get_next_add_field(next_field, skip_facebook_link=True)
                        continue
                    
                    # Skip if field is already filled
                    if is_field_filled(user_data_store[user_id], next_field):
                        logger.info(f"[ADD_FIELD] Skipping already filled field: {next_field}")
                        next_field, next_state, current_step, total_steps = get_next_add_field(next_field, skip_facebook_link=is_forwarded)
                    else:
                        break
                
                # Move to next field or review
                if next_field == 'review':
                    await show_add_review(update, context)
                    return ADD_REVIEW
                else:
                    field_label = get_field_label(next_field)
                    is_optional = next_field not in ['fullname']
                    progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
                    
                    if next_field == 'manager_name':
                        message = f"{progress_text}📝 <b>Введите стейдж менеджера:</b>\n\n⚠️ <b>Важно:</b> Введите имя так, как менеджер записан в отчётности (для корректной группировки данных)"
                    elif next_field == 'fullname':
                        message = f"{progress_text}📝 Введите {field_label}:"
                    else:
                        requirements = get_field_format_requirements(next_field)
                        message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
                    
                    context.user_data['current_field'] = next_field
                    context.user_data['current_state'] = next_state
                    
                    sent_message = await update.message.reply_text(
                        message,
                        reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                        parse_mode='HTML'
                    )
                    await save_add_message(update, context, sent_message.message_id)
                    return next_state
        else:
            # User is not in the process of adding a lead - start adding immediately
            user_id = update.effective_user.id
            logger.info(f"[ADD_FIELD] Starting add flow from forwarded message for user {user_id}")
            
            # Check if forward_from is available (privacy settings may hide it)
            if update.message.forward_from is None:
                # Privacy settings hide the sender info - start normal flow
                clear_all_conversation_state(context, user_id)
                user_data_store[user_id] = {}
                user_data_store_access_time[user_id] = time.time()
                context.user_data['current_field'] = 'fullname'
                context.user_data['current_state'] = ADD_FULLNAME
                context.user_data['add_step'] = 0
                context.user_data['is_forwarded_message'] = True  # Mark as forwarded message
                
                field_label = get_field_label('fullname')
                _, _, current_step, total_steps = get_next_add_field('', skip_facebook_link=True)
                message = f"<b>Шаг {current_step} из {total_steps}</b>\n\n📝 Введите {field_label}:"
                
                await update.message.reply_text(
                    "⚠️ Данные отправителя недоступны из-за настроек приватности.\n\n" + message,
                    reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
                    parse_mode='HTML'
                )
                return ADD_FULLNAME
            else:
                forward_from = update.message.forward_from
                
                # Check if it's a bot
                if forward_from.is_bot:
                    await update.message.reply_text(
                        "❌ Недоступно. Аккаунт является ботом.",
                        reply_markup=get_main_menu_keyboard()
                    )
                    return ConversationHandler.END
                
                # Initialize add flow
                clear_all_conversation_state(context, user_id)
                user_data_store[user_id] = {}
                user_data_store_access_time[user_id] = time.time()
                context.user_data['current_field'] = 'fullname'
                context.user_data['current_state'] = ADD_FULLNAME
                context.user_data['add_step'] = 0
                context.user_data['is_forwarded_message'] = True  # Mark as forwarded message
                
                # Extract data from forward_from
                extracted_data = {}
                extracted_info = []
                
                # Extract telegram_id (required if available)
                if forward_from.id:
                    telegram_id = normalize_telegram_id(str(forward_from.id))
                    if telegram_id:
                        extracted_data['telegram_id'] = telegram_id
                        extracted_info.append(f"• Telegram ID: {telegram_id}")
                        logger.info(f"[ADD_FIELD] Extracted telegram_id: {telegram_id}")
                
                # Extract telegram_name (if available)
                if forward_from.username:
                    is_valid, _, normalized = validate_telegram_name(forward_from.username)
                    if is_valid:
                        extracted_data['telegram_name'] = normalized
                        extracted_info.append(f"• Username: @{normalized}")
                        logger.info(f"[ADD_FIELD] Extracted telegram_name: {normalized}")
                
                # Extract fullname (Display Name: first_name + last_name)
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
                        logger.info(f"[ADD_FIELD] Extracted fullname: {normalized_fullname}")
                
                # Parse text message for Facebook link (if available) - extract but skip the step
                if update.message.text:
                    text = update.message.text.strip()
                    is_valid_fb, _, fb_normalized = validate_facebook_link(text)
                    if is_valid_fb:
                        extracted_data['facebook_link'] = fb_normalized
                        extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
                        logger.info(f"[ADD_FIELD] Extracted facebook_link from message text: {fb_normalized}")
                
                # Save extracted data to user_data_store
                for key, value in extracted_data.items():
                    user_data_store[user_id][key] = value
                
                # Show user what was extracted
                if extracted_info:
                    info_text = "\n".join(extracted_info)
                    await update.message.reply_text(
                        f"✅ <b>Данные извлечены</b> из пересланного сообщения:\n\n{info_text}\n\n"
                        f"💡 <b>Продолжайте заполнение остальных полей.</b>"
                    )
                
                # Determine next field to fill - start from beginning and skip all filled fields
                # Start from first field and skip all already filled fields
                # Use skip_facebook_link=True for forwarded messages
                is_forwarded = context.user_data.get('is_forwarded_message', False)
                next_field, next_state, current_step, total_steps = get_next_add_field('', skip_facebook_link=is_forwarded)
                
                # Skip already filled fields AND skip facebook_link for forwarded messages
                while next_field != 'review':
                    # Always skip facebook_link for forwarded messages
                    if next_field == 'facebook_link' and is_forwarded:
                        logger.info(f"[ADD_FIELD] Skipping facebook_link field for forwarded message")
                        next_field, next_state, current_step, total_steps = get_next_add_field(next_field, skip_facebook_link=True)
                        continue
                    
                    # Skip if field is already filled
                    if is_field_filled(user_data_store[user_id], next_field):
                        logger.info(f"[ADD_FIELD] Skipping already filled field: {next_field}")
                        next_field, next_state, current_step, total_steps = get_next_add_field(next_field, skip_facebook_link=is_forwarded)
                    else:
                        break
                
                # Move to next field or review
                if next_field == 'review':
                    await show_add_review(update, context)
                    return ADD_REVIEW
                else:
                    field_label = get_field_label(next_field)
                    is_optional = next_field not in ['fullname']
                    progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
                    
                    if next_field == 'manager_name':
                        message = f"{progress_text}📝 <b>Введите стейдж менеджера:</b>\n\n⚠️ <b>Важно:</b> Введите имя так, как менеджер записан в отчётности (для корректной группировки данных)"
                    elif next_field == 'fullname':
                        message = f"{progress_text}📝 Введите {field_label}:"
                    else:
                        requirements = get_field_format_requirements(next_field)
                        message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
                    
                    context.user_data['current_field'] = next_field
                    context.user_data['current_state'] = next_state
                    
                    sent_message = await update.message.reply_text(
                        message,
                        reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                        parse_mode='HTML'
                    )
                    await save_add_message(update, context, sent_message.message_id)
                    return next_state
    
    # PRIORITY 2: Handle regular text message (existing logic)
    if not update.message.text:
        logger.error("add_field_input: update.message.text is None (and not forwarded)")
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    # Determine field_name from current_state FIRST, then fallback to context.user_data
    # This ensures we use the correct field based on ConversationHandler state
    current_state = context.user_data.get('current_state', ADD_FULLNAME)
    
    # Map ConversationHandler states to field names
    state_to_field = {
        ADD_FULLNAME: 'fullname',
    }
    if is_facebook_flow_enabled():
        state_to_field[ADD_FB_LINK] = 'facebook_link'
    state_to_field.update({
        ADD_TELEGRAM_NAME: 'telegram_name',
        ADD_TELEGRAM_ID: 'telegram_id',
    })
    
    # Get field_name from state first (more reliable)
    field_name = state_to_field.get(current_state)
    
    # Fallback to context.user_data if state mapping doesn't work
    if not field_name:
        field_name = context.user_data.get('current_field')
    
    # Log for debugging
    logger.info(f"[ADD_FIELD] Processing input - current_state: {current_state}, field_name from state: {state_to_field.get(current_state)}, field_name from user_data: {context.user_data.get('current_field')}, field_name determined: {field_name}, text: '{text[:50]}...'")
    
    # Update access time BEFORE cleanup to protect from race conditions
    user_data_store_access_time[user_id] = time.time()
    
    # Ensure user_data_store entry exists before cleanup
    if user_id not in user_data_store:
        user_data_store[user_id] = {}
    
    # Log before cleanup
    if user_id in user_data_store:
        logger.info(f"[ADD_FIELD] Before cleanup - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        if 'telegram_name' in user_data_store[user_id]:
            logger.info(f"[ADD_FIELD] Before cleanup - telegram_name: '{user_data_store[user_id].get('telegram_name')}'")
    
    # Cleanup with exclusion of current user to prevent race conditions
    cleanup_user_data_store(exclude_user_id=user_id)
    
    # Log after cleanup
    if user_id in user_data_store:
        logger.info(f"[ADD_FIELD] After cleanup - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        if 'telegram_name' in user_data_store[user_id]:
            logger.info(f"[ADD_FIELD] After cleanup - telegram_name: '{user_data_store[user_id].get('telegram_name')}'")
    else:
        logger.error(f"[ADD_FIELD] After cleanup - user_data_store[{user_id}] was DELETED!")
    
    if not field_name:
        # Fallback: start from beginning
        await update.message.reply_text(
            "❌ Неизвестная команда. Выберите действие:",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Validate and normalize based on field type
    validation_passed = False
    normalized_value = text
    
    if field_name == 'facebook_link':
        is_valid, error_msg, extracted = validate_facebook_link(text)
        if is_valid:
            validation_passed = True
            normalized_value = extracted
        else:
            field_label = get_field_label('facebook_link')
            requirements = get_field_format_requirements('facebook_link')
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}\n\n📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
    
    elif field_name == 'telegram_name':
        # First check if the input is a Facebook URL - if so, reject it
        text_lower = text.lower()
        has_url_patterns = (
            'facebook.com' in text_lower or
            'http://' in text_lower or
            'https://' in text_lower or
            'www.' in text_lower
        )
        is_fb_url = False
        if has_url_patterns:
            is_fb_url, fb_error_msg, _ = validate_facebook_link(text)
        text_lower = text.lower()
        has_url_patterns = (
            'facebook.com' in text_lower or
            'http://' in text_lower or
            'https://' in text_lower or
            'www.' in text_lower
        )
        is_fb_url = False
        if has_url_patterns:
            is_fb_url, fb_error_msg, _ = validate_facebook_link(text)
        if is_fb_url:
            field_label = get_field_label('telegram_name')
            requirements = get_field_format_requirements('telegram_name')
            sent_message = await update.message.reply_text(
                f"❌ <b>Ошибка:</b> Вы ввели Facebook ссылку в поле \"{field_label}\".\n\n"
                f"💡 <b>Что делать:</b>\n"
                f"• Для Facebook ссылки используйте соответствующее поле (если доступно)\n"
                f"• Для Telegram username введите только username без URL\n\n"
                f"📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
        
        is_valid, error_msg, normalized = validate_telegram_name(text)
        if is_valid:
            validation_passed = True
            normalized_value = normalized
        else:
            field_label = get_field_label('telegram_name')
            requirements = get_field_format_requirements('telegram_name')
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}\n\n📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
    
    elif field_name == 'telegram_id':
        is_valid, error_msg, normalized = validate_telegram_id(text)
        if is_valid:
            validation_passed = True
            normalized_value = normalized
        else:
            field_label = get_field_label('telegram_id')
            requirements = get_field_format_requirements('telegram_id')
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}\n\n📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
    
    else:
        # For other fields (fullname, manager_name), normalize text and check not empty
        if text:
            # Check length before normalization
            if len(text) > 500:
                field_label = get_field_label(field_name)
                await update.message.reply_text(
                    f"❌ {field_label} слишком длинное (максимум 500 символов).\n\n"
                    f"📝 Введите {field_label}:",
                    reply_markup=get_navigation_keyboard(is_optional=(field_name not in ['fullname']), show_back=True),
                    parse_mode='HTML'
                )
                await save_add_message(update, context, update.message.message_id)
                return current_state
            
            # Normalize text fields (fullname, manager_name)
            normalized_value = normalize_text_field(text)
            if normalized_value:
                validation_passed = True
            else:
                # Text was only whitespace
                validation_passed = False
        else:
            field_label = get_field_label(field_name)
            is_optional = field_name not in ['fullname']
            
            # Для обязательных полей (fullname) не показываем требования к формату
            if field_name == 'fullname':
                message = f"❌ Поле не может быть пустым.\n\n📝 Введите {field_label}:"
                use_html = False
            else:
                requirements = get_field_format_requirements(field_name)
                message = f"❌ Поле не может быть пустым.\n\n📝 Введите {field_label}:\n\n{requirements}"
                use_html = True
            
            sent_message = await update.message.reply_text(
                message,
                reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=(field_name != 'fullname')),
                parse_mode='HTML' if use_html else None
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
    
    # Real-time duplicate check for critical fields (removed phone check)
    # Note: Real-time duplicate checking can be added for other fields if needed
    if False:  # Placeholder for future real-time checks
        client = get_supabase_client()
        if client:
            is_unique, existing_fullname = await check_duplicate_realtime(client, field_name, normalized_value)
            if not is_unique:
                field_label = UNIQUENESS_FIELD_LABELS.get(field_name, field_name)
                # If duplicate found, stop adding and return to main menu
                await update.message.reply_text(
                    f"⚠️ Внимание: {field_label} уже существует в базе.\n"
                    f"Существующий лид: {existing_fullname}\n\n"
                    f"Добавление нового лида отменено.",
                    reply_markup=get_main_menu_keyboard()
                )
                # Clean up user data for this user
                if user_id in user_data_store:
                    del user_data_store[user_id]
                if user_id in user_data_store_access_time:
                    del user_data_store_access_time[user_id]
                return ConversationHandler.END
    
    # Save value only if validation passed
    if validation_passed and normalized_value:
        # Log state of photo_file_id before any operations
        if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
            logger.info(f"[ADD_FIELD] photo_file_id exists before field save: {user_data_store[user_id]['photo_file_id']}")
        
        # Protect photo_file_id from being lost if user_data_store is recreated
        saved_photo_file_id = None
        if user_id in user_data_store and 'photo_file_id' in user_data_store[user_id]:
            saved_photo_file_id = user_data_store[user_id]['photo_file_id']
            logger.info(f"[ADD_FIELD] Preserving photo_file_id: {saved_photo_file_id}")
        
        # Ensure user_data_store entry exists (protection against race conditions)
        if user_id not in user_data_store:
            logger.warning(f"[ADD_FIELD] Created new user_data_store[{user_id}] - this should not happen during add flow if document was saved")
            user_data_store[user_id] = {}
            user_data_store_access_time[user_id] = time.time()
        
        # Restore photo_file_id if it was saved
        if saved_photo_file_id and 'photo_file_id' not in user_data_store[user_id]:
            user_data_store[user_id]['photo_file_id'] = saved_photo_file_id
            logger.info(f"[ADD_FIELD] Restored photo_file_id: {saved_photo_file_id}")
        
        user_data_store[user_id][field_name] = normalized_value
        logger.info(f"[ADD_FIELD] Saved {field_name} = '{normalized_value}' for user {user_id}")
        logger.info(f"[ADD_FIELD] user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        
        # Check if this is a forwarded message
        is_forwarded = context.user_data.get('is_forwarded_message', False)
        
        # If Facebook link is valid, automatically skip Telegram fields and go to review
        if is_facebook_flow_enabled() and field_name == 'facebook_link' and validation_passed:
            logger.info(f"[ADD_FIELD] Facebook link is valid, auto-skipping Telegram fields (telegram_name, telegram_id)")
            # Skip telegram_name and telegram_id, go directly to review
            next_field, next_state, current_step, total_steps = get_next_add_field('telegram_id', skip_facebook_link=is_forwarded)
        else:
            # Normal flow - move to next field
            next_field, next_state, current_step, total_steps = get_next_add_field(field_name, skip_facebook_link=is_forwarded)
    else:
        logger.warning(f"[ADD_FIELD] Not saving {field_name}: validation_passed={validation_passed}, normalized_value='{normalized_value}'")
        # Check if this is a forwarded message
        is_forwarded = context.user_data.get('is_forwarded_message', False)
        # If validation failed, stay on current field
        next_field, next_state, current_step, total_steps = get_next_add_field(field_name, skip_facebook_link=is_forwarded)
    
    # Skip already filled fields AND skip facebook_link for forwarded messages
    is_forwarded = context.user_data.get('is_forwarded_message', False)
    while next_field != 'review':
        # Always skip facebook_link for forwarded messages
        if next_field == 'facebook_link' and is_forwarded:
            logger.info(f"[ADD_FIELD] Skipping facebook_link field for forwarded message")
            next_field, next_state, current_step, total_steps = get_next_add_field(next_field, skip_facebook_link=True)
            continue
        
        # Skip if field is already filled
        if is_field_filled(user_data_store.get(user_id, {}), next_field):
            logger.info(f"[ADD_FIELD] Skipping already filled field: {next_field}")
            next_field, next_state, current_step, total_steps = get_next_add_field(next_field, skip_facebook_link=is_forwarded)
        else:
            break
    
    # Log current state before moving to next field
    if user_id in user_data_store:
        logger.info(f"[ADD_FIELD] Before moving to {next_field} - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        if 'telegram_name' in user_data_store[user_id]:
            logger.info(f"[ADD_FIELD] Before moving to {next_field} - telegram_name: '{user_data_store[user_id].get('telegram_name')}'")
    
    if next_field == 'review':
        # Show review and save option
        logger.info(f"[ADD_FIELD] Moving to review - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys()) if user_id in user_data_store else 'N/A'}")
        
        # Check if we're returning to review after editing fullname
        if context.user_data.get('return_to_review'):
            # Clear the flag
            del context.user_data['return_to_review']
            # Show review screen
            await show_add_review(update, context)
            return ADD_REVIEW
        else:
            # Normal flow to review
            await show_add_review(update, context)
            return ADD_REVIEW
    else:
        # Show next field with progress indicator
        field_label = get_field_label(next_field)
        is_optional = next_field not in ['fullname', 'manager_name']
        
        # Add progress indicator
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        
        # Для обязательных полей (fullname, manager_name) не показываем требования к формату
        if next_field == 'manager_name':
            message = f"{progress_text}📝 Введите стейдж менеджера:\n\n ⚠️ Так менеджер записан в отчётности"
        elif next_field == 'fullname':
            message = f"{progress_text}📝 Введите {field_label}:"
        else:
            requirements = get_field_format_requirements(next_field)
            message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
        
        context.user_data['current_field'] = next_field
        context.user_data['current_state'] = next_state
        
        # Работаем как с message, так и с callback_query
        # Always use HTML when progress indicator is present
        if update.callback_query:
            # Проверить, что сообщение не содержит фото
            if update.callback_query.message and update.callback_query.message.photo:
                # Если сообщение содержит фото, отправить новое сообщение вместо редактирования
                sent_message = await update.callback_query.message.reply_text(
                    message,
                    reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                    parse_mode='HTML'
                )
                await save_add_message(update, context, sent_message.message_id)
            else:
                # Сообщение не содержит фото, можно редактировать
                try:
                    await update.callback_query.edit_message_text(
                        message,
                        reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                        parse_mode='HTML'
                    )
                    # Save message ID for cleanup (edit_message_text doesn't return new message, use existing)
                    if update.callback_query.message:
                        await save_add_message(update, context, update.callback_query.message.message_id)
                except Exception as e:
                    # Если редактирование не удалось (например, сообщение содержит фото), отправить новое
                    logger.warning(f"[ADD_FIELD] Failed to edit message, sending new one: {e}")
                    if update.callback_query.message:
                        sent_message = await update.callback_query.message.reply_text(
                            message,
                            reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                            parse_mode='HTML'
                        )
                        await save_add_message(update, context, sent_message.message_id)
        elif update.message:
            sent_message = await retry_telegram_api(
                update.message.reply_text,
                text=message,
                reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
        return next_state

async def show_add_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show review of entered data before saving"""
    user_id = update.effective_user.id
    user_data = user_data_store.get(user_id, {})
    
    logger.info(f"[SHOW_REVIEW] user_data keys: {list(user_data.keys())}")
    
    message_parts = ["✅ <b>Проверьте введенные данные:</b>\n"]
    
    field_labels = {
        'fullname': 'Имя Фамилия',
        'facebook_link': 'Facebook Ссылка',
        'telegram_name': 'Тег Telegram',
        'telegram_id': 'Telegram ID'
    }
    
    for field_name, field_label in field_labels.items():
        value = user_data.get(field_name)
        if value:
            # Format Facebook link to full URL for display
            if field_name == 'facebook_link':
                formatted_value = format_facebook_link_for_display(str(value))
                escaped_value = escape_html(formatted_value)
            else:
                escaped_value = escape_html(str(value))
            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
    
    message = "\n".join(message_parts)
    
    keyboard = [
        [InlineKeyboardButton("💾 Сохранить", callback_data="add_save")],
    ]
    
    # Кнопка pre-save меню редактирования полей (доступна, если есть хотя бы одно заполненное поле)
    if any(user_data.get(field) for field in ['fullname', 'facebook_link', 'telegram_name', 'telegram_id']):
        keyboard.append([InlineKeyboardButton("✏️ Редактировать поля", callback_data="edit_fullname_from_review")])
    
    keyboard.extend([
        [InlineKeyboardButton("◀️ Назад", callback_data="add_back")],
        [InlineKeyboardButton("❌ Отменить", callback_data="add_cancel")]
    ])
    
    # Работаем как с message, так и с callback_query
    if update.callback_query:
        # Проверить, что сообщение не содержит фото
        if update.callback_query.message and update.callback_query.message.photo:
            # Если сообщение содержит фото, отправить новое сообщение вместо редактирования
            sent_message = await update.callback_query.message.reply_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
        else:
            # Сообщение не содержит фото, можно редактировать
            try:
                await update.callback_query.edit_message_text(
                    message,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
                # Save message ID for cleanup
                if update.callback_query.message:
                    await save_add_message(update, context, update.callback_query.message.message_id)
            except Exception as e:
                # Если редактирование не удалось (например, сообщение содержит фото), отправить новое
                logger.warning(f"[SHOW_REVIEW] Failed to edit message, sending new one: {e}")
                if update.callback_query.message:
                    sent_message = await update.callback_query.message.reply_text(
                        message,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='HTML'
                    )
                    await save_add_message(update, context, sent_message.message_id)
    elif update.message:
        sent_message = await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        await save_add_message(update, context, sent_message.message_id)

async def add_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip current optional field"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    field_name = context.user_data.get('current_field')
    current_state = context.user_data.get('current_state', ADD_FULLNAME)
    
    # Check if this is a forwarded message
    is_forwarded = context.user_data.get('is_forwarded_message', False)
    
    # Move to next field
    next_field, next_state, current_step, total_steps = get_next_add_field(field_name, skip_facebook_link=is_forwarded)
    
    # Skip already filled fields AND skip facebook_link for forwarded messages
    while next_field != 'review':
        # Always skip facebook_link for forwarded messages
        if next_field == 'facebook_link' and is_forwarded:
            logger.info(f"[ADD_SKIP] Skipping facebook_link field for forwarded message")
            next_field, next_state, current_step, total_steps = get_next_add_field(next_field, skip_facebook_link=True)
            continue
        
        # Skip if field is already filled
        if is_field_filled(user_data_store.get(user_id, {}), next_field):
            logger.info(f"[ADD_SKIP] Skipping already filled field: {next_field}")
            next_field, next_state, current_step, total_steps = get_next_add_field(next_field, skip_facebook_link=is_forwarded)
        else:
            break
    
    if next_field == 'review':
        # Show review and save option
        await show_add_review(update, context)
        return ADD_REVIEW
    else:
        # Show next field with progress indicator
        field_label = get_field_label(next_field)
        is_optional = next_field not in ['fullname', 'manager_name']
        
        # Add progress indicator
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        
        # Для обязательных полей (fullname, manager_name) не показываем требования к формату
        if next_field == 'manager_name':
            message = f"{progress_text}📝 Введите стейдж менеджера:\n\n ⚠️ Так менеджер записан в отчётности"
        elif next_field == 'fullname':
            message = f"{progress_text}📝 Введите {field_label}:"
        else:
            requirements = get_field_format_requirements(next_field)
            message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
        
        context.user_data['current_field'] = next_field
        context.user_data['current_state'] = next_state
        
        await query.edit_message_text(
            message,
            reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
            parse_mode='HTML'
        )
        # Save message ID for cleanup
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        return next_state

async def edit_fullname_from_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open pre-save edit menu from review screen (edit fields before saving without PIN)"""
    query = update.callback_query
    await retry_telegram_api(query.answer)
    
    user_id = query.from_user.id
    user_data = user_data_store.get(user_id)
    
    # Если данных нет, предложить начать заново
    if not user_data:
        await query.edit_message_text(
            "❌ Ошибка: данные лида не найдены. Начните добавление заново.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Убедиться, что мы остаёмся в контексте обзора
    context.user_data['current_state'] = ADD_REVIEW
    context.user_data['current_field'] = 'review'
    
    # Построить меню полей для редактирования (по аналогии с меню редактирования сохранённого лида)
    keyboard = []
    
    keyboard.append([InlineKeyboardButton("✏️ Имя клиента", callback_data="add_edit_field_fullname")])
    keyboard.append([InlineKeyboardButton("✏️ Тег Telegram", callback_data="add_edit_field_telegram_name")])
    keyboard.append([InlineKeyboardButton("✏️ Telegram ID", callback_data="add_edit_field_telegram_id")])
    
    # Facebook ссылка - только если FACEBOOK_FLOW включен
    if is_facebook_flow_enabled():
        keyboard.append([InlineKeyboardButton("✏️ Facebook ссылка", callback_data="add_edit_field_fb_link")])
    
    keyboard.append([InlineKeyboardButton("⬅️ К обзору", callback_data="add_edit_back_to_review")])
    keyboard.append([InlineKeyboardButton("❌ Отменить", callback_data="add_cancel")])
    
    message = (
        "✏️ <b>Редактирование лида до сохранения</b>\n\n"
        "Выберите поле, которое хотите изменить. После ввода нового значения вы вернётесь на экран обзора.\n\n"
        "PIN-код не требуется, так как лид ещё не сохранён в базе."
    )
    
    await retry_telegram_api(
        query.edit_message_text,
        text=message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    
    # Сохраняем ID сообщения для последующей очистки
    if query.message:
        await save_add_message(update, context, query.message.message_id)
    
    return ADD_REVIEW

async def add_edit_field_from_review(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str):
    """Generic handler to edit a specific field from pre-save review menu"""
    query = update.callback_query
    await retry_telegram_api(query.answer)
    
    user_id = query.from_user.id
    if user_id not in user_data_store:
        await query.edit_message_text(
            "❌ Ошибка: данные лида не найдены. Начните добавление заново.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    user_data = user_data_store.get(user_id, {})
    
    # После редактирования поля нужно вернуться к обзору
    context.user_data['return_to_review'] = True
    
    # Проверка: facebook_link можно редактировать только если FACEBOOK_FLOW включен
    if field_name == 'facebook_link' and not is_facebook_flow_enabled():
        logger.warning(f"[ADD_EDIT_FROM_REVIEW] Attempt to edit facebook_link when FACEBOOK_FLOW is disabled for user {user_id}")
        await query.edit_message_text(
            "❌ Ошибка: редактирование Facebook ссылки недоступно, так как Facebook flow отключен.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Карту полей в соответствующие состояния add flow
    field_to_state = {
        'fullname': ADD_FULLNAME,
        'telegram_name': ADD_TELEGRAM_NAME,
        'telegram_id': ADD_TELEGRAM_ID,
    }
    # Добавляем facebook_link только если FACEBOOK_FLOW включен
    if is_facebook_flow_enabled():
        field_to_state['facebook_link'] = ADD_FB_LINK
    
    target_state = field_to_state.get(field_name)
    if not target_state:
        logger.error(f"[ADD_EDIT_FROM_REVIEW] Unsupported field_name={field_name} for user {user_id}")
        await query.edit_message_text(
            "❌ Ошибка: данное поле нельзя редактировать на этом этапе.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    context.user_data['current_field'] = field_name
    context.user_data['current_state'] = target_state
    
    current_value = user_data.get(field_name, '')
    field_label = get_field_label(field_name)
    _, _, current_step, total_steps = get_next_add_field('')
    
    progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
    
    # fullname — обязательное поле, без требований к формату в сообщении
    if field_name == 'fullname':
        if current_value:
            message = (
                f"{progress_text}📝 Введите {field_label}:\n\n"
                f"💡 Текущее значение: <code>{escape_html(str(current_value))}</code>"
            )
        else:
            message = f"{progress_text}📝 Введите {field_label}:"
        is_optional = False
    else:
        requirements = get_field_format_requirements(field_name)
        if current_value:
            message = (
                f"{progress_text}📝 Введите {field_label}:\n\n"
                f"💡 Текущее значение: <code>{escape_html(str(current_value))}</code>\n\n"
                f"{requirements}"
            )
        else:
            message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
        is_optional = True
    
    await retry_telegram_api(
        query.edit_message_text,
        text=message,
        reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
        parse_mode='HTML'
    )
    
    if query.message:
        await save_add_message(update, context, query.message.message_id)
    
    return target_state

async def add_edit_field_fullname_from_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await add_edit_field_from_review(update, context, 'fullname')

async def add_edit_field_telegram_name_from_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await add_edit_field_from_review(update, context, 'telegram_name')

async def add_edit_field_telegram_id_from_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await add_edit_field_from_review(update, context, 'telegram_id')

async def add_edit_field_fb_link_from_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await add_edit_field_from_review(update, context, 'facebook_link')

async def add_edit_back_to_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return from pre-save edit menu back to review screen"""
    query = update.callback_query
    await retry_telegram_api(query.answer)
    
    user_id = query.from_user.id
    
    if user_id not in user_data_store:
        await query.edit_message_text(
            "❌ Ошибка: данные лида не найдены. Начните добавление заново.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Явно установить состояние обзора
    context.user_data['current_state'] = ADD_REVIEW
    context.user_data['current_field'] = 'review'
    
    await show_add_review(update, context)
    return ADD_REVIEW

async def add_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to previous field"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    field_name = context.user_data.get('current_field')
    
    # Check if this is a forwarded message
    is_forwarded = context.user_data.get('is_forwarded_message', False)
    
    # Специальная обработка для состояния ADD_REVIEW
    if field_name == 'review':
        # Определить последнее заполненное поле или последнее поле в последовательности
        field_sequence = [
            ('fullname', ADD_FULLNAME),
        ]
        if is_facebook_flow_enabled():
            field_sequence.append(('facebook_link', ADD_FB_LINK))
        field_sequence.extend([
            ('telegram_name', ADD_TELEGRAM_NAME),
            ('telegram_id', ADD_TELEGRAM_ID),
        ])
        
        # Filter out facebook_link if this is a forwarded message
        # Facebook link step - controlled by FACEBOOK_FLOW env var
        if is_forwarded:
            field_sequence = [f for f in field_sequence if f[0] != 'facebook_link']
        
        # Найти последнее заполненное поле
        user_data = user_data_store.get(user_id, {})
        last_filled_field = None
        last_filled_state = ADD_FULLNAME
        
        # Проверяем поля в обратном порядке
        for field, state in reversed(field_sequence):
            if is_field_filled(user_data, field):
                last_filled_field = field
                last_filled_state = state
                break
        
        # Если не найдено заполненное поле, используем последнее поле в последовательности
        if not last_filled_field:
            last_filled_field, last_filled_state = field_sequence[-1]
        
        # Показать форму для последнего поля
        field_label = get_field_label(last_filled_field)
        is_optional = last_filled_field not in ['fullname']
        
        # Calculate step number for the field
        _, _, current_step, total_steps = get_next_add_field(last_filled_field, skip_facebook_link=is_forwarded)
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        
        # Для обязательных полей (fullname) не показываем требования к формату
        if last_filled_field == 'fullname':
            message = f"{progress_text}📝 Введите {field_label}:"
        else:
            requirements = get_field_format_requirements(last_filled_field)
            message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
        
        context.user_data['current_field'] = last_filled_field
        context.user_data['current_state'] = last_filled_state
        
        await query.edit_message_text(
            message,
            reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=(last_filled_field != 'fullname')),
            parse_mode='HTML'
        )
        # Save message ID for cleanup
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        return last_filled_state
    
    # Get previous field - filter out facebook_link for forwarded messages
    field_sequence = [
        ('fullname', ADD_FULLNAME),
    ]
    if is_facebook_flow_enabled():
        field_sequence.append(('facebook_link', ADD_FB_LINK))
    field_sequence.extend([
        ('telegram_name', ADD_TELEGRAM_NAME),
        ('telegram_id', ADD_TELEGRAM_ID),
    ])
    
    # Filter out facebook_link if this is a forwarded message
    # Facebook link step - controlled by FACEBOOK_FLOW env var
    if is_forwarded:
        field_sequence = [f for f in field_sequence if f[0] != 'facebook_link']
    
    prev_field = None
    prev_state = ADD_FULLNAME
    
    for i, (field, state) in enumerate(field_sequence):
        if field == field_name:
            if i > 0:
                prev_field, prev_state = field_sequence[i - 1]
            break
    
    if prev_field:
        field_label = get_field_label(prev_field)
        is_optional = prev_field not in ['fullname']
        
        # Calculate step number for previous field
        _, _, current_step, total_steps = get_next_add_field(prev_field, skip_facebook_link=is_forwarded)
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        
        # Для обязательных полей (fullname) не показываем требования к формату
        if prev_field == 'fullname':
            message = f"{progress_text}📝 Введите {field_label}:"
        else:
            requirements = get_field_format_requirements(prev_field)
            message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
        
        context.user_data['current_field'] = prev_field
        context.user_data['current_state'] = prev_state
        
        await query.edit_message_text(
            message,
            reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=(prev_field != 'fullname')),
            parse_mode='HTML'
        )
        # Save message ID for cleanup
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        return prev_state
    else:
        # Already at first field, go to main menu
        await query.edit_message_text(
            "👋 Главное меню\n\nВыберите действие:",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END

async def add_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate and save the lead"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = user_data_store.get(user_id, {})
    
    # Validation
    if not user_data.get('fullname'):
        field_label = get_field_label('fullname')
        _, _, current_step, total_steps = get_next_add_field('')
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        await query.edit_message_text(
            f"{progress_text}❌ <b>Ошибка:</b> {field_label} обязателен для заполнения!\n\n"
            f"📝 Введите {field_label}:",
            reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
            parse_mode='HTML'
        )
        context.user_data['current_field'] = 'fullname'
        context.user_data['current_state'] = ADD_FULLNAME
        return ADD_FULLNAME
    
    # Check if at least one identifier is present (only if minimal mode is disabled)
    if not is_minimal_add_mode_enabled():
        required_fields = ['telegram_name', 'telegram_id']
        if is_facebook_flow_enabled():
            required_fields.append('facebook_link')
        has_identifier = any(user_data.get(field) for field in required_fields)
        
        if not has_identifier:
            error_msg = "❌ <b>Ошибка:</b> Необходимо указать минимум одно из полей для идентификации клиента:\n\n"
            if is_facebook_flow_enabled():
                error_msg += "• <b>Facebook Ссылка</b> - ссылка на профиль Facebook\n"
            error_msg += "• <b>Тег Telegram</b> - username клиента (минимум 5 символов)\n"
            error_msg += "• <b>Telegram ID</b> - числовой идентификатор (минимум 5 цифр)\n\n"
            error_msg += "ℹ️ Поле <b>Имя клиента</b> является обязательным.\n"
            error_msg += "Одно из полей идентификации также обязательно."
            await query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
    else:
        # Minimal mode: require photo only when no identifiers were provided
        required_fields = ['telegram_name', 'telegram_id']
        if is_facebook_flow_enabled():
            required_fields.append('facebook_link')
        has_identifier = any(user_data.get(field) for field in required_fields)

        had_photo = user_data.get('had_photo') or (user_id in user_data_store and user_data_store[user_id].get('had_photo'))
        photo_file_id_exists = 'photo_file_id' in user_data or (user_id in user_data_store and 'photo_file_id' in user_data_store[user_id])
        
        if not has_identifier and not had_photo and not photo_file_id_exists:
            error_msg = "❌ <b>Ошибка:</b> В режиме минимального добавления необходимо прикрепить фото.\n\n"
            error_msg += "💡 <b>Что нужно сделать:</b>\n"
            error_msg += "• Прикрепите фото к сообщению с именем клиента\n"
            error_msg += "• Или вернитесь к шагу добавления и прикрепите фото\n\n"
            error_msg += "ℹ️ В минимальном режиме требуется только <b>имя</b> и <b>фото</b> клиента."
            await query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
    
    # Get Supabase client for uniqueness check
    client = get_supabase_client()
    if not client:
        error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
        await query.edit_message_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        if user_id in user_data_store:
            del user_data_store[user_id]
        return ConversationHandler.END
    
    # Check uniqueness of fields - optimized batch check
    # Only check optional identifier fields (facebook_link, telegram_name, telegram_id)
    # Do NOT check fullname and manager_name - they can be duplicated
    logger.info(f"[ADD_SAVE] Before uniqueness check - user_data keys: {list(user_data.keys())}")
    if 'telegram_name' in user_data:
        logger.info(f"[ADD_SAVE] Before uniqueness check - telegram_name: '{user_data.get('telegram_name')}'")
    
    fields_to_check = {}
    # Only check optional identifier fields for uniqueness
    for field_name in ['facebook_link', 'telegram_name', 'telegram_id']:
        field_value = user_data.get(field_name)
        if field_value and field_value.strip():  # Only check non-empty fields
            check_value = field_value
            fields_to_check[field_name] = check_value
            logger.info(f"[ADD_SAVE] Adding {field_name} to uniqueness check: '{check_value}'")
        else:
            logger.info(f"[ADD_SAVE] Skipping {field_name} in uniqueness check (empty or None)")
    
    logger.info(f"[ADD_SAVE] Fields to check for uniqueness: {list(fields_to_check.keys())}")
    
    # Batch check uniqueness
    if fields_to_check:
        is_unique, conflicting_field = check_fields_uniqueness_batch(client, fields_to_check)
        logger.info(f"[ADD_SAVE] After uniqueness check - is_unique: {is_unique}, conflicting_field: {conflicting_field}")
        logger.info(f"[ADD_SAVE] After uniqueness check - user_data keys: {list(user_data.keys())}")
        if 'telegram_name' in user_data:
            logger.info(f"[ADD_SAVE] After uniqueness check - telegram_name: '{user_data.get('telegram_name')}'")
        if not is_unique:
            field_label = UNIQUENESS_FIELD_LABELS.get(conflicting_field, conflicting_field)
            
            # Get review screen message ID to exclude from cleanup
            review_message_id = None
            if query.message:
                review_message_id = query.message.message_id
            
            # Clean up all add flow messages BEFORE showing error message
            # Exclude review screen message so we can edit it
            await cleanup_add_messages(update, context, exclude_message_id=review_message_id)
            
            try:
                await query.edit_message_text(
                    f"❌ <b>Ошибка:</b> {field_label} уже существует в базе данных, лид не был сохранён.\n\n"
                    "💡 <b>Что можно сделать:</b>\n"
                    "• Проверить существующий лид через меню \"Проверить\"\n"
                    "• Добавить лид заново с другими данными\n"
                    "• Убедиться, что данные введены корректно",
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            except Exception as e:
                # If edit fails (message was deleted), send new message
                if "not found" in str(e) or "BadRequest" in str(type(e).__name__):
                    await query.message.reply_text(
                        f"❌ <b>Ошибка:</b> {field_label} уже существует в базе, лид не был сохранён.\n\n"
                        "ℹ️ Попробуйте добавить лид заново с другими данными.",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    raise
            # Полностью очищаем все состояния и временные данные пользователя
            clear_all_conversation_state(context, user_id)
            return ConversationHandler.END
    
    # All fields are unique, proceed with saving
    try:
        # Check if user had a photo but it was lost
        had_photo = user_data.get('had_photo') or (user_id in user_data_store and user_data_store[user_id].get('had_photo'))
        photo_file_id_exists = 'photo_file_id' in user_data or (user_id in user_data_store and 'photo_file_id' in user_data_store[user_id])
        
        if had_photo and not photo_file_id_exists:
            # User expected photo to be saved, but it was lost
            logger.warning(f"[ADD_SAVE] Photo was expected (had_photo=True) but photo_file_id is missing for user {user_id}")
            if is_minimal_add_mode_enabled():
                await query.edit_message_text(
                    "⚠️ <b>Предупреждение:</b> Прикреплённое изображение не удалось сохранить.\n\n"
                    "💡 <b>Что можно сделать:</b>\n"
                    "• Отменить и добавить лид заново с фото\n"
                    "• Попробовать прикрепить фото снова",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Отменить", callback_data="add_cancel")]
                    ]),
                    parse_mode='HTML'
                )
            else:
                await query.edit_message_text(
                    "⚠️ <b>Предупреждение:</b> Прикреплённое изображение не удалось сохранить.\n\n"
                    "💡 <b>Что можно сделать:</b>\n"
                    "• Сохранить лид без фото (нажмите «Сохранить» ещё раз)\n"
                    "• Отменить и добавить лид заново с фото\n"
                    "• Попробовать прикрепить фото снова",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💾 Сохранить без фото", callback_data="add_save_force")],
                        [InlineKeyboardButton("❌ Отменить", callback_data="add_cancel")]
                    ]),
                    parse_mode='HTML'
                )
            return ADD_REVIEW
        
        # Prepare data for saving - map telegram_name to telegram_user for database compatibility
        save_data = user_data.copy()
        
        # Remove photo_file_id - it's a temporary value used only for uploading to Supabase Storage
        # The photo_url will be set later after successful upload
        if 'photo_file_id' in save_data:
            save_data.pop('photo_file_id')
        # Remove had_photo - internal flag, not a DB column
        if 'had_photo' in save_data:
            save_data.pop('had_photo')
        
        # Map telegram_name to telegram_user for database (backward compatibility)
        if 'telegram_name' in save_data:
            telegram_name_value = save_data.pop('telegram_name')
            # Only set telegram_user if telegram_name has a value (not empty string or None)
            if telegram_name_value:
                save_data['telegram_user'] = telegram_name_value
        
        # Automatically extract manager_name and manager_tag from user who is saving
        from_user = query.from_user
        first_name = from_user.first_name or ""
        last_name = from_user.last_name or ""
        
        # Build manager_name from first_name + last_name
        if last_name:
            manager_name = f"{first_name} {last_name}".strip()
        else:
            manager_name = first_name.strip()
        
        # Normalize manager_name (trim spaces, collapse multiple spaces)
        manager_name = normalize_text_field(manager_name)
        save_data['manager_name'] = manager_name
        
        # Extract manager_tag from username (without @)
        manager_tag = ""
        if from_user.username:
            # Remove @ if present and normalize
            username = from_user.username.replace('@', '').strip()
            if username:
                manager_tag = username
        
        save_data['manager_tag'] = manager_tag
        
        logger.info(f"[ADD_SAVE] Inserting data to database: {save_data}")
        response = client.table(TABLE_NAME).insert(save_data).execute()
        
        # Log successful save with all fields
        if response.data and len(response.data) > 0:
            saved_lead = response.data[0]
            lead_id = saved_lead.get('id')
            logger.info(f"[NEW_LEAD_SAVED] ✅ New lead successfully saved to database")
            logger.info(f"[NEW_LEAD_SAVED] Lead ID: {lead_id}")
            
            # Try to upload photo if we extracted it earlier
            # Log photo_file_id state in user_data_store before getting user_data
            photo_file_id_in_store = None
            if user_id in user_data_store:
                photo_file_id_in_store = user_data_store[user_id].get('photo_file_id')
                logger.info(f"[ADD_SAVE] Before photo check - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}, photo_file_id={photo_file_id_in_store}")
            else:
                logger.warning(f"[ADD_SAVE] Before photo check - user_data_store[{user_id}] does not exist")
            
            # Log user_data state
            photo_file_id_in_user_data = user_data.get('photo_file_id')
            logger.info(f"[ADD_SAVE] Before photo check - user_data keys: {list(user_data.keys())}, photo_file_id={photo_file_id_in_user_data}")
            
            # Log ConversationHandler state for diagnostics
            has_conversation_keys = any(
                key.startswith('_conversation_') 
                for key in (context.user_data.keys() if context.user_data else [])
            )
            logger.info(f"[ADD_SAVE] ConversationHandler state - has_conversation_keys={has_conversation_keys}, current_state={context.user_data.get('current_state')}")
            
            # Get photo_file_id from user_data, but also check user_data_store directly as fallback
            # This ensures we don't lose photo_file_id if it was saved but not in user_data
            user_photo_file_id = user_data.get('photo_file_id')
            photo_source = "user_data"
            if not user_photo_file_id and user_id in user_data_store:
                # Fallback: check user_data_store directly
                user_photo_file_id = user_data_store[user_id].get('photo_file_id')
                if user_photo_file_id:
                    photo_source = "user_data_store (fallback)"
                    logger.info(f"[ADD_SAVE] Found photo_file_id in user_data_store (was missing in user_data): {user_photo_file_id}")
                    # Update user_data for consistency
                    user_data['photo_file_id'] = user_photo_file_id
            elif user_photo_file_id:
                logger.info(f"[ADD_SAVE] Found photo_file_id in user_data: {user_photo_file_id}")
            
            # Log final source of photo_file_id
            if user_photo_file_id:
                logger.info(f"[ADD_SAVE] photo_file_id source: {photo_source}, value: {user_photo_file_id}")
            else:
                logger.warning(f"[ADD_SAVE] photo_file_id not found in user_data or user_data_store")
            if lead_id:
                if user_photo_file_id:
                    logger.info(f"[PHOTO] Starting photo upload for lead {lead_id} from file_id={user_photo_file_id}")
                    photo_url = await upload_lead_photo_to_supabase(context.bot, user_photo_file_id, lead_id)
                    if photo_url:
                        try:
                            # Update the lead with photo_url
                            client.table(TABLE_NAME).update({"photo_url": photo_url}).eq("id", lead_id).execute()
                            logger.info(f"[PHOTO] Successfully saved photo_url for lead {lead_id}: {photo_url}")
                        except Exception as e:
                            logger.error(f"[PHOTO] Failed to save photo_url to database for lead {lead_id}: {e}", exc_info=True)
                    else:
                        logger.warning(f"[PHOTO] Photo upload failed (returned None) for lead {lead_id}, file_id={user_photo_file_id}")
                else:
                    logger.warning(f"[PHOTO] No photo_file_id in user_data for lead {lead_id}, user_data keys: {list(user_data.keys())}")
            
            # Log all fields with their values
            field_labels = {
                'fullname': 'Клиент (fullname)',
                'manager_name': 'Стейдж менеджера (manager_name)',
                'manager_tag': 'Тег менеджера (manager_tag)',
                'facebook_link': 'Facebook Ссылка (facebook_link)',
                'telegram_user': 'Тег Telegram (telegram_user)',
                'telegram_id': 'Telegram ID (telegram_id)',
                'photo_url': 'Фото (photo_url)',
                'created_at': 'Дата создания (created_at)'
            }
            
            logged_fields = []
            for field_name, field_label in field_labels.items():
                value = saved_lead.get(field_name)
                if value is not None and value != '':
                    logged_fields.append(f"  {field_label}: '{value}'")
                else:
                    logged_fields.append(f"  {field_label}: (не указано)")
            
            logger.info(f"[NEW_LEAD_SAVED] Fields:\n" + "\n".join(logged_fields))
        
        if response.data:
            # Show success message with entered data
            message_parts = ["✅ <b>Клиент успешно добавлен!</b>\n"]
            field_labels = {
                'fullname': 'Имя Фамилия',
                'facebook_link': 'Facebook Ссылка',
                'telegram_name': 'Тег Telegram',
                'telegram_id': 'Telegram ID'
            }
            
            for field_name, field_label in field_labels.items():
                # Check telegram_user for display (it was mapped from telegram_name)
                display_field = 'telegram_user' if field_name == 'telegram_name' else field_name
                value = save_data.get(display_field) or user_data.get(field_name)
                if value:
                    # Format Facebook link to full URL for display
                    if field_name == 'facebook_link':
                        formatted_value = format_facebook_link_for_display(str(value))
                        escaped_value = escape_html(formatted_value)
                    else:
                        escaped_value = escape_html(str(value))
                    message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
            
            # Add date
            from datetime import datetime
            message_parts.append(f"Дата добавления: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            
            message = "\n".join(message_parts)
            
            # Get review screen message ID to exclude from cleanup
            review_message_id = None
            if query.message:
                review_message_id = query.message.message_id
            
            # Clean up all add flow messages BEFORE showing final success message
            # Exclude review screen message so we can edit it
            await cleanup_add_messages(update, context, exclude_message_id=review_message_id)
            
            try:
                await query.edit_message_text(
                    message,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            except Exception as e:
                # If edit fails (message was deleted), send new message
                if "not found" in str(e) or "BadRequest" in str(type(e).__name__):
                    await query.message.reply_text(
                        message,
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    raise
        else:
            # Get review screen message ID to exclude from cleanup
            review_message_id = None
            if query.message:
                review_message_id = query.message.message_id
            
            # Clean up all add flow messages BEFORE showing error message
            # Exclude review screen message so we can edit it
            await cleanup_add_messages(update, context, exclude_message_id=review_message_id)
            
            try:
                await query.edit_message_text(
                    "❌ <b>Ошибка:</b> Данные не были сохранены.\n\n"
                    "ℹ️ Попробуйте снова или обратитесь к администратору.",
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            except Exception as e:
                # If edit fails (message was deleted), send new message
                if "not found" in str(e) or "BadRequest" in str(type(e).__name__):
                    await query.message.reply_text(
                        "❌ <b>Ошибка:</b> Данные не были сохранены.\n\n"
                        "ℹ️ Попробуйте снова или обратитесь к администратору.",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    raise
    
    except Exception as e:
        logger.error(f"Error adding client: {e}", exc_info=True)
        error_msg = get_user_friendly_error(e, "сохранении данных")
        
        # Get review screen message ID to exclude from cleanup
        review_message_id = None
        if query.message:
            review_message_id = query.message.message_id
        
        # Clean up all add flow messages BEFORE showing error message
        # Exclude review screen message so we can edit it
        await cleanup_add_messages(update, context, exclude_message_id=review_message_id)
        
        try:
            await query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
        except Exception as edit_error:
            # If edit fails (message was deleted), send new message
            if "not found" in str(edit_error) or "BadRequest" in str(type(edit_error).__name__):
                await query.message.reply_text(
                    error_msg,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            else:
                raise
    
    # Clean up
    if user_id in user_data_store:
        del user_data_store[user_id]
    if user_id in user_data_store_access_time:
        del user_data_store_access_time[user_id]
    
    # Полная очистка состояния, включая внутренние ключи ConversationHandler
    clear_all_conversation_state(context, user_id)
    
    return ConversationHandler.END

async def add_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel adding new lead"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id in user_data_store:
        del user_data_store[user_id]
    if user_id in user_data_store_access_time:
        del user_data_store_access_time[user_id]
    
    # Полная очистка состояния, включая внутренние ключи ConversationHandler
    clear_all_conversation_state(context, user_id)
    
    await query.edit_message_text(
        "❌ Добавление отменено.",
        reply_markup=get_main_menu_keyboard()
    )
    return ConversationHandler.END

# Edit lead functionality
