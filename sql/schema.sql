-- notino-datamining schema
-- Run once on Neon DB (neondb)

CREATE TABLE IF NOT EXISTS products (
    id          BIGSERIAL PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    shop        TEXT NOT NULL,
    title       TEXT,
    price       NUMERIC(12,2),
    currency    VARCHAR(3) DEFAULT 'CZK',
    image_url   TEXT,
    scraped_at  TIMESTAMPTZ DEFAULT NOW(),
    raw_json    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop);
CREATE INDEX IF NOT EXISTS idx_products_scraped_at ON products(scraped_at DESC);

CREATE TABLE IF NOT EXISTS product_url_queue (
    id            BIGSERIAL PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    shop          TEXT NOT NULL,
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at  TIMESTAMPTZ,
    status        TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_queue_shop_status ON product_url_queue(shop, status);

CREATE TABLE IF NOT EXISTS price_history (
    id           BIGSERIAL PRIMARY KEY,
    product_url  TEXT NOT NULL,
    fetched_at   TIMESTAMPTZ DEFAULT NOW(),
    min_price    NUMERIC(12,2),
    max_price    NUMERIC(12,2),
    current_price NUMERIC(12,2),
    currency     VARCHAR(3) DEFAULT 'CZK',
    raw_json     JSONB,
    UNIQUE (product_url, (fetched_at::date))
);

CREATE INDEX IF NOT EXISTS idx_price_history_url ON price_history(product_url);
CREATE INDEX IF NOT EXISTS idx_price_history_fetched ON price_history(fetched_at DESC);
