from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Проверить", callback_data="check_menu")],
        [InlineKeyboardButton("➕ Добавить", callback_data="add_new")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_check_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📱 Тег Telegram", callback_data="check_telegram")],
        [InlineKeyboardButton("🆔 Telegram ID", callback_data="check_telegram_id")],
        [InlineKeyboardButton("👤 Клиент", callback_data="check_fullname")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_check_back_keyboard():
    keyboard = [
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_add_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Добавить новый лид", callback_data="add_new")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_navigation_keyboard(is_optional: bool = False, show_back: bool = True) -> InlineKeyboardMarkup:
    keyboard = []
    if is_optional:
        keyboard.append([InlineKeyboardButton("⏭️ Пропустить", callback_data="add_skip")])
    if show_back:
        keyboard.append([
            InlineKeyboardButton("◀️ Назад", callback_data="add_back"),
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
        ])
    else:
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def get_edit_field_keyboard(user_id: int, original_data: dict = None, user_data_store: dict | None = None):
    store = user_data_store or {}
    user_data = store.get(user_id, {})
    keyboard = []

    def has_value(field_name):
        value = user_data.get(field_name)
        return value is not None and value != '' and (not isinstance(value, str) or value.strip() != '')

    def is_changed(field_name):
        if not original_data:
            return False
        current_value = user_data.get(field_name)
        original_value = original_data.get(field_name)
        if field_name == 'telegram_name':
            original_value = original_data.get('telegram_name') or original_data.get('telegram_user')
        if current_value is None and (original_value is None or original_value == ''):
            return False
        if current_value == '' and (original_value is None or original_value == ''):
            return False
        return str(current_value).strip() != str(original_value).strip() if original_value else current_value is not None

    def get_status(field_name):
        if is_changed(field_name):
            return "🟡"
        if has_value(field_name):
            return "🟢"
        return "⚪"

    fullname_status = get_status('fullname')
    manager_status = get_status('manager_name')

    keyboard.append([InlineKeyboardButton(f"{fullname_status} Имя Фамилия *", callback_data="edit_field_fullname")])
    keyboard.append([InlineKeyboardButton(f"{manager_status} Агент *", callback_data="edit_field_manager")])

    fb_link_status = get_status('facebook_link')
    telegram_name_status = get_status('telegram_name')
    telegram_id_status = get_status('telegram_id')

    keyboard.append([InlineKeyboardButton(f"{fb_link_status} Facebook Ссылка", callback_data="edit_field_fb_link")])
    keyboard.append([InlineKeyboardButton(f"{telegram_name_status} Тег Telegram", callback_data="edit_field_telegram_name")])
    keyboard.append([InlineKeyboardButton(f"{telegram_id_status} Telegram ID", callback_data="edit_field_telegram_id")])

    keyboard.append([InlineKeyboardButton("💾 Сохранить изменения", callback_data="edit_save")])
    keyboard.append([InlineKeyboardButton("❌ Отменить", callback_data="edit_cancel")])

    return InlineKeyboardMarkup(keyboard)

