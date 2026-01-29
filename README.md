# Telegram Bot для управления лидами (SMM Leads Bot)

Telegram-бот для управления базой данных лидов с поддержкой проверки, добавления и редактирования записей. Бот интегрирован с Supabase для хранения данных и загрузки фотографий.

## Возможности

- ✅ **Проверка лидов** - поиск по имени, Telegram username/ID, Facebook ссылке
- ✅ **Добавление лидов** - пошаговое заполнение данных с возможностью прикрепления фото
- ✅ **Редактирование лидов** - изменение данных существующих записей (требуется PIN-код)
- ✅ **Изменение тегов менеджеров** - массовое обновление тегов для всех лидов менеджера (требуется PIN-код)
- ✅ **Работа с пересланными сообщениями** - автоматическое извлечение данных из пересланных сообщений
- ✅ **Загрузка фотографий** - сохранение фото лидов в Supabase Storage

## Требования

- Python 3.8+
- Аккаунт в [Supabase](https://supabase.com)
- Аккаунт в [Koyeb](https://www.koyeb.com) (или другой платформе для хостинга)
- Telegram бот (созданный через [@BotFather](https://t.me/BotFather))

## Быстрый старт

### 1. Настройка Supabase

#### 1.1. Создание проекта

1. Зарегистрируйтесь на [Supabase](https://supabase.com) и создайте новый проект
2. Дождитесь завершения инициализации проекта

#### 1.2. Создание таблицы

Выполните следующий SQL в SQL Editor вашего проекта Supabase:

```sql
-- Создание таблицы для лидов
CREATE TABLE IF NOT EXISTS facebook_leads (
    id BIGSERIAL PRIMARY KEY,
    fullname TEXT,
    telegram_user TEXT,
    telegram_id TEXT,
    facebook_link TEXT,
    manager_name TEXT,
    manager_tag TEXT,
    photo_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Создание индексов для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_telegram_user ON facebook_leads(telegram_user);
CREATE INDEX IF NOT EXISTS idx_telegram_id ON facebook_leads(telegram_id);
CREATE INDEX IF NOT EXISTS idx_facebook_link ON facebook_leads(facebook_link);
CREATE INDEX IF NOT EXISTS idx_fullname ON facebook_leads(fullname);
CREATE INDEX IF NOT EXISTS idx_manager_name ON facebook_leads(manager_name);

-- Включение Row Level Security (RLS)
ALTER TABLE facebook_leads ENABLE ROW LEVEL SECURITY;

-- Политика RLS: разрешить все операции через service_role key
-- Для анонимного доступа можно создать более строгие политики
CREATE POLICY "Allow all operations for service role" ON facebook_leads
    FOR ALL
    USING (true)
    WITH CHECK (true);
```

#### 1.3. Создание Storage Bucket

1. Перейдите в раздел **Storage** в вашем проекте Supabase
2. Создайте новый bucket с именем `Leads` (или другое имя, которое вы укажете в переменных окружения)
3. Настройте политики доступа:
   - **Public bucket**: включите, если хотите, чтобы фото были доступны по прямой ссылке
   - **File size limit**: установите максимальный размер файла (например, 5MB)
   - **Allowed MIME types**: `image/jpeg`, `image/png`, `image/webp`

#### 1.4. Получение ключей API

1. Перейдите в **Settings** → **API**
2. Скопируйте следующие значения:
   - **Project URL** (SUPABASE_URL)
   - **anon public** key (SUPABASE_KEY)
   - **service_role** key (SUPABASE_SERVICE_ROLE_KEY) - **ВАЖНО**: этот ключ имеет полный доступ к базе данных, храните его в секрете!

### 2. Настройка Telegram бота

1. Откройте [@BotFather](https://t.me/BotFather) в Telegram
2. Отправьте команду `/newbot` и следуйте инструкциям
3. Скопируйте полученный **Bot Token** (TELEGRAM_BOT_TOKEN)
4. (Опционально) Настройте описание и команды бота через `/setdescription` и `/setcommands`

### 3. Развёртывание на Koyeb

#### 3.1. Подготовка репозитория

1. Создайте новый репозиторий на GitHub
2. Загрузите файлы проекта:
   - `main.py`
   - `requirements.txt`
   - `Procfile`
   - `.gitignore` (если используете)

#### 3.2. Создание сервиса в Koyeb

1. Зарегистрируйтесь на [Koyeb](https://www.koyeb.com)
2. Нажмите **Create App** → **GitHub**
3. Выберите ваш репозиторий
4. Настройте сервис:
   - **Name**: выберите имя для вашего сервиса
   - **Region**: выберите ближайший регион
   - **Build Command**: оставьте пустым (Koyeb автоматически определит Python проект)
   - **Run Command**: оставьте пустым (используется Procfile)

#### 3.3. Настройка переменных окружения

В разделе **Environment Variables** добавьте следующие переменные:

| Переменная | Описание | Обязательная |
|-----------|----------|--------------|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram бота от @BotFather | ✅ Да |
| `WEBHOOK_URL` | URL для webhook (будет настроен автоматически) | ✅ Да |
| `SUPABASE_URL` | URL вашего Supabase проекта | ✅ Да |
| `SUPABASE_KEY` | anon public key из Supabase | ✅ Да |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role key из Supabase | ✅ Да |
| `PIN_CODE` | PIN-код для доступа к редактированию и команде /tag | ✅ Да |
| `TABLE_NAME` | Имя таблицы в Supabase (по умолчанию: `facebook_leads`) | ❌ Нет |
| `SUPABASE_LEADS_BUCKET` | Имя Storage bucket (по умолчанию: `Leads`) | ❌ Нет |
| `ENABLE_LEAD_PHOTOS` | Включить загрузку фото (`true`/`false`, по умолчанию: `true`) | ❌ Нет |
| `PORT` | Порт приложения (обычно устанавливается автоматически Koyeb) | ❌ Нет |

**Пример значений:**
```
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
WEBHOOK_URL=https://your-app-name.koyeb.app/webhook
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
PIN_CODE=your_secure_pin_here
```

#### 3.4. Настройка Webhook URL

После развёртывания сервиса:

1. Скопируйте URL вашего приложения (например, `https://your-app-name.koyeb.app`)
2. Обновите переменную окружения `WEBHOOK_URL` на `https://your-app-name.koyeb.app/webhook`
3. Перезапустите сервис в Koyeb

**Примечание**: Webhook будет автоматически настроен при первом запуске приложения.

### 4. Проверка работы

1. Откройте вашего бота в Telegram
2. Отправьте команду `/start`
3. Проверьте, что бот отвечает и показывает главное меню
4. Протестируйте основные функции:
   - Проверка лида
   - Добавление нового лида
   - Редактирование лида (требуется PIN-код)

## Структура проекта

```
.
├── main.py              # Основной файл с логикой бота
├── requirements.txt     # Зависимости Python
├── Procfile            # Конфигурация для Koyeb
├── .gitignore          # Игнорируемые файлы
└── README.md           # Этот файл
```

## Переменные окружения

### Обязательные переменные

- **TELEGRAM_BOT_TOKEN** - Токен бота от @BotFather
- **WEBHOOK_URL** - URL для webhook (формат: `https://your-domain.com/webhook`)
- **SUPABASE_URL** - URL проекта Supabase
- **SUPABASE_KEY** - anon public key из Supabase
- **SUPABASE_SERVICE_ROLE_KEY** - service_role key из Supabase (для операций Storage)
- **PIN_CODE** - PIN-код для защищённых операций (редактирование, команда /tag)

### Опциональные переменные

- **TABLE_NAME** - Имя таблицы в базе данных (по умолчанию: `facebook_leads`)
- **SUPABASE_LEADS_BUCKET** - Имя Storage bucket для фото (по умолчанию: `Leads`)
- **ENABLE_LEAD_PHOTOS** - Включить/выключить загрузку фото (`true`/`false`, по умолчанию: `true`)
- **PORT** - Порт приложения (обычно устанавливается автоматически Koyeb)

## Команды бота

- `/start` - Начать работу с ботом, показать главное меню
- `/q` - Выйти из текущего сценария, вернуться в главное меню
- `/tag` - Изменить тег менеджера (требуется PIN-код)
- `/skip` - Пропустить текущий шаг (в процессе добавления лида)

## Безопасность

⚠️ **ВАЖНО**: 

- Никогда не коммитьте файлы `.env` или реальные значения переменных окружения в репозиторий
- `SUPABASE_SERVICE_ROLE_KEY` имеет полный доступ к базе данных - храните его в секрете
- `PIN_CODE` должен быть достаточно сложным и не должен быть в коде
- Используйте переменные окружения для всех секретных данных

## Возможные проблемы и решения

### Проблема: Бот не отвечает

**Решение:**
1. Проверьте, что все обязательные переменные окружения установлены
2. Проверьте логи в Koyeb на наличие ошибок
3. Убедитесь, что `WEBHOOK_URL` указан правильно
4. Проверьте, что бот не был заблокирован пользователем

### Проблема: Ошибка подключения к Supabase

**Решение:**
1. Проверьте правильность `SUPABASE_URL` и `SUPABASE_KEY`
2. Убедитесь, что проект Supabase активен
3. Проверьте настройки RLS (Row Level Security) в Supabase

### Проблема: Фото не загружаются

**Решение:**
1. Проверьте, что Storage bucket создан и имеет правильное имя
2. Убедитесь, что `SUPABASE_SERVICE_ROLE_KEY` установлен правильно
3. Проверьте политики доступа к Storage bucket
4. Убедитесь, что `ENABLE_LEAD_PHOTOS=true`

### Проблема: Ошибка при сохранении лида

**Решение:**
1. Проверьте структуру таблицы в Supabase (должна соответствовать схеме выше)
2. Убедитесь, что RLS политики настроены правильно
3. Проверьте логи на наличие конкретных ошибок


## Поддержка

Если у вас возникли проблемы или вопросы:
1. Проверьте раздел "Возможные проблемы и решения"
2. Изучите логи приложения в Koyeb
3. Создайте issue в репозитории проекта
