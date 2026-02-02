# Развёртывание SMM Leads Bot (Koyeb + Supabase + Telegram)

## 0. Нужно заранее
- Аккаунты: [Supabase](https://supabase.com), [Koyeb](https://www.koyeb.com), Telegram
- Создайте новый [Github](https://github.com) репозиторий и импортируйте туда [SMM-Bot](https://github.com/Markl1n1/SMM-Leads-Test.git).

## 1. Supabase

### 1.1. Проект и таблица
Создайте проект и выполните SQL в Supabase SQL Editor:

```sql
CREATE TABLE IF NOT EXISTS facebook_leads (
    id BIGSERIAL PRIMARY KEY,
    fullname TEXT,
    manager_name TEXT,
    manager_tag TEXT,
    telegram_user TEXT,
    telegram_id TEXT,
    facebook_link TEXT,
    photo_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telegram_user ON facebook_leads(telegram_user);
CREATE INDEX IF NOT EXISTS idx_telegram_id ON facebook_leads(telegram_id);
CREATE INDEX IF NOT EXISTS idx_facebook_link ON facebook_leads(facebook_link);
CREATE INDEX IF NOT EXISTS idx_fullname ON facebook_leads(fullname);
CREATE INDEX IF NOT EXISTS idx_manager_name ON facebook_leads(manager_name);

ALTER TABLE facebook_leads ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow all operations for service role" ON facebook_leads
    FOR ALL
    USING (true)
    WITH CHECK (true);
```

### 1.2. Storage bucket
- Storage → создать bucket `Leads` (или другое имя)
- Разрешённые типы: `image/jpeg`, `image/jpg`, `image/png`, `image/webp`
- Ограничение размера: по вашему лимиту
- Public: включите, если нужны публичные ссылки на фото

### 1.3. Ключи
Скопируйте в **Settings → API**:
- `SUPABASE_URL`
- `SUPABASE_KEY` (anon public)
- `SUPABASE_SERVICE_ROLE_KEY` (service_role)

## 2. Telegram бот
1. В [@BotFather](https://t.me/BotFather) создайте бота командой `/newbot`
2. Скопируйте `TELEGRAM_BOT_TOKEN`

## 3. Koyeb

### 3.1. Подключение репозитория
1. Загрузите файлы проекта в GitHub
2. Koyeb → **Create App** → **GitHub** → выберите репозиторий (который вы создали на шаге 0)

### 3.2. Переменные окружения
Добавьте в **Environment Variables**:

| Переменная | Назначение | Обязательная |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | токен BotFather | ✅ |
| `WEBHOOK_URL` | `https://<app>.koyeb.app/webhook` | ✅ |
| `SUPABASE_URL` | URL проекта Supabase | ✅ |
| `SUPABASE_KEY` | anon key | ✅ |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role key | ✅ |
| `PIN_CODE` | PIN для редактирования и `/tag` | ✅ |
| `TABLE_NAME` | имя таблицы (по умолчанию `facebook_leads`) | ❌ |
| `SUPABASE_LEADS_BUCKET` | bucket (по умолчанию `Leads`) | ❌ |
| `ENABLE_LEAD_PHOTOS` | `true/false` (по умолчанию `true`) | ❌ |
| `FACEBOOK_FLOW` | `ON/OFF` (по умолчанию `OFF`) | ❌ |
| `MINIMAL_ADD_MODE` | `ON/OFF` (по умолчанию `OFF`) | ❌ |
| `RATE_LIMIT_ENABLED` | `true/false` (по умолчанию `true`) | ❌ |
| `RATE_LIMIT_REQUESTS` | лимит запросов (по умолчанию `30`) | ❌ |
| `RATE_LIMIT_WINDOW` | окно в секундах (по умолчанию `60`) | ❌ |
| `CLEANUP_INTERVAL_MINUTES` | очистка user_data_store (по умолчанию `10`) | ❌ |
| `PORT` | порт приложения (обычно автосет) | ❌ |

Пример:
```
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
WEBHOOK_URL=https://your-app-name.koyeb.app/webhook
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
PIN_CODE=your_secure_pin_here
```

### 3.3. Webhook
1. После деплоя скопируйте URL приложения
2. Обновите `WEBHOOK_URL` на `https://<app>.koyeb.app/webhook`
3. Перезапустите сервис

## 4. Проверка
1. Откройте бота в Telegram
2. `/start` → убедитесь, что меню открывается
3. Протестируйте поиск и добавление лида
