# План модульной структуры и миграции

## Цель

Снизить риск багов и «залипаний» состояния за счёт изоляции сценариев и явных границ между handlers, flows и services. Миграция — маленькими шагами, без одномоментного рефакторинга.

## Предложенная структура модулей
```
bot/
  app.py                # create_telegram_app(), регистрация handlers
  config.py             # env, флаги режимов, константы
  state.py              # user_data_store, helpers clear_all_conversation_state, cleanup
  utils.py              # normalize, validate, format, helpers
  keyboards.py          # inline keyboards
  logging.py            # logger, debug helpers
  services/
    supabase_client.py  # get_supabase_client, storage client
    leads_repo.py       # DB queries: create/update/search
    photos.py           # upload/download to storage
  flows/
    add_flow.py         # add_new_callback, add_field_input, add_save, review
    check_flow.py       # check_menu_callback, smart_check_input, search helpers
    edit_flow.py        # edit_* callbacks, edit save
    tag_flow.py         # /tag, tag_pin_input, tag_manager_callback
    photo_flow.py       # handle_photo_message, handle_photo_during_add/check
    forwarded_flow.py   # handle_forwarded_message, forwarded_add/check
  handlers/
  # (опционально) thin routers for MessageHandler/CallbackQueryHandler
main.py                 # thin entrypoint: imports app, runs initialize_telegram_app
```

## Порядок безопасной миграции (small steps)

### Шаг 1 — Подготовка каркаса
- Создать папку `bot/` и пустые модули (`config.py`, `state.py`, `utils.py`, `keyboards.py`).
- Вынести **только константы/env** в `config.py`.
- `main.py` пока импортирует всё.

### Шаг 2 — Вынести keyboards и простые utils
- Перенести `get_main_menu_keyboard`, `get_check_menu_keyboard`, `get_navigation_keyboard` и т.п. в `keyboards.py`.
- Перенести `normalize_*`, `validate_*`, `format_*` в `utils.py`.
- В `main.py` заменить обращения на импорты.

### Шаг 3 — Вынести state/cleanup
- В `state.py` переместить `user_data_store`, `user_data_store_access_time`, `clear_all_conversation_state`, `cleanup_*`.
- Все модули должны использовать единые state‑helpers.

### Шаг 4 — Вынести services (Supabase и фото)
- Создать `services/supabase_client.py` и `services/photos.py`.
- Перенести `get_supabase_client`, `get_supabase_storage_client`, `upload_lead_photo_to_supabase`, `download_photo_from_supabase`.

### Шаг 5 — Перенос flows по одному сценарию
- 5.1 `check_flow.py`: smart check, check_by_field, check_by_multiple_fields, check_by_fullname.
- 5.2 `add_flow.py`: add_new_callback, add_field_input, add_save_callback, show_add_review.
- 5.3 `photo_flow.py`: handle_photo_message, handle_photo_during_add/check.
- 5.4 `forwarded_flow.py`: handle_forwarded_message, forwarded_add/check.
- 5.5 `edit_flow.py`: edit_* callbacks, edit_save, edit_cancel.
- 5.6 `tag_flow.py`: /tag flow.

Каждый перенос — отдельный коммит/этап: импортировать в `main.py`, запускать, проверять основные сценарии.

### Шаг 6 — Вынести create_telegram_app
- Переместить регистрацию handlers в `bot/app.py`.
- `main.py` становится thin entrypoint.

### Шаг 7 — Мини‑проверка стабильности
- Пройти по основным сценариям: add/check/edit/tag + фото/forward.
- Убедиться, что `current_state` и `user_data_store` не теряются.

## Безопасность миграции
- Никакой логики не меняем, только переносим.
- Публичные функции и сигнатуры не трогаем.
- Каждому шагу — быстрая ручная проверка ключевого сценария.

## Что будет на выходе
- Изолированные сценарии, меньше конфликтов состояний.
- Быстрое понимание, где именно ломается логика.
- Лёгкие точечные правки без риска затронуть другие flows.

