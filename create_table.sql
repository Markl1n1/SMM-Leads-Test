
CREATE TABLE IF NOT EXISTS facebook_leads (
    -- Первичный ключ
    id BIGSERIAL PRIMARY KEY,
    
    -- Обязательные поля
    fullname TEXT NOT NULL,
    manager_name TEXT NOT NULL,
    
    -- Опциональные поля для идентификации (минимум одно должно быть заполнено)
    phone TEXT,
    facebook_link TEXT,
    telegram_user TEXT,
    telegram_id TEXT,
    
    -- Тег менеджера (Telegram username того, кто добавил лид)
    manager_tag TEXT,
    
    -- Автоматическая дата создания
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

-- Создание частичных уникальных индексов для проверки уникальности непустых значений
-- Уникальный индекс для телефона (только для непустых значений)
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_phone ON facebook_leads(phone) 
    WHERE phone IS NOT NULL AND phone != '';

-- Уникальный индекс для Facebook Link (только для непустых значений)
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_facebook_link ON facebook_leads(facebook_link) 
    WHERE facebook_link IS NOT NULL AND facebook_link != '';

-- Уникальный индекс для Telegram User (только для непустых значений)
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_telegram_user ON facebook_leads(telegram_user) 
    WHERE telegram_user IS NOT NULL AND telegram_user != '';

-- Уникальный индекс для Telegram ID (только для непустых значений)
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_telegram_id ON facebook_leads(telegram_id) 
    WHERE telegram_id IS NOT NULL AND telegram_id != '';

-- Создание индексов для быстрого поиска
-- Индекс для поиска по телефону (частичный поиск по последним цифрам)
CREATE INDEX IF NOT EXISTS idx_phone ON facebook_leads(phone) WHERE phone IS NOT NULL;

-- Индекс для поиска по Facebook Link
CREATE INDEX IF NOT EXISTS idx_facebook_link ON facebook_leads(facebook_link) WHERE facebook_link IS NOT NULL;

-- Индекс для поиска по Telegram User
CREATE INDEX IF NOT EXISTS idx_telegram_user ON facebook_leads(telegram_user) WHERE telegram_user IS NOT NULL;

-- Индекс для поиска по Telegram ID
CREATE INDEX IF NOT EXISTS idx_telegram_id ON facebook_leads(telegram_id) WHERE telegram_id IS NOT NULL;

-- Индекс для поиска по Full Name (для частичного поиска "contains")
CREATE INDEX IF NOT EXISTS idx_fullname ON facebook_leads(fullname);

-- Индекс для поиска по Manager Name
CREATE INDEX IF NOT EXISTS idx_manager_name ON facebook_leads(manager_name);

-- Индекс для сортировки по дате создания
CREATE INDEX IF NOT EXISTS idx_created_at ON facebook_leads(created_at DESC);

-- Комментарии к таблице и полям
COMMENT ON TABLE facebook_leads IS 'Таблица для хранения лидов из Facebook и других источников';
COMMENT ON COLUMN facebook_leads.id IS 'Уникальный идентификатор лида';
COMMENT ON COLUMN facebook_leads.fullname IS 'Полное имя клиента (обязательное поле)';
COMMENT ON COLUMN facebook_leads.manager_name IS 'Имя менеджера/агента, добавившего лида (обязательное поле)';
COMMENT ON COLUMN facebook_leads.phone IS 'Номер телефона (нормализованный, без пробелов и +)';
COMMENT ON COLUMN facebook_leads.facebook_link IS 'Ссылка на Facebook профиль (username или profile.php?id=...)';
COMMENT ON COLUMN facebook_leads.telegram_user IS 'Telegram username (без @)';
COMMENT ON COLUMN facebook_leads.telegram_id IS 'Telegram ID (только цифры)';
COMMENT ON COLUMN facebook_leads.manager_tag IS 'Telegram username менеджера, добавившего лида (без @)';
COMMENT ON COLUMN facebook_leads.created_at IS 'Дата и время создания записи';

-- Представление для удобного просмотра данных (опционально)
CREATE OR REPLACE VIEW facebook_leads_view AS
SELECT 
    id,
    fullname,
    manager_name,
    phone,
    facebook_link,
    telegram_user,
    telegram_id,
    manager_tag,
    created_at,
    CASE 
        WHEN phone IS NOT NULL AND phone != '' THEN 'Phone'
        WHEN facebook_link IS NOT NULL AND facebook_link != '' THEN 'Facebook Link'
        WHEN telegram_user IS NOT NULL AND telegram_user != '' THEN 'Telegram User'
        WHEN telegram_id IS NOT NULL AND telegram_id != '' THEN 'Telegram ID'
        ELSE 'No identifier'
    END AS primary_identifier
FROM facebook_leads
ORDER BY created_at DESC;

COMMENT ON VIEW facebook_leads_view IS 'Представление для удобного просмотра лидов с определением основного идентификатора';