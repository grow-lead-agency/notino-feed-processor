-- notino-datamining schema addons
-- Neon DB: neondb (ep-soft-forest-agqhbeg0.c-2.eu-central-1.aws.neon.tech)
--
-- Existing tables (from main project, DO NOT recreate):
--   products, product_url_queue, price_history, competitors, brands,
--   affiliate_networks, affiliate_programs, product_categories, etc.
--
-- This file adds only the tables needed by notino-feed-processor.
-- Run: psql "$DATABASE_URL" -f sql/schema.sql

-- Crawl run log
CREATE TABLE IF NOT EXISTS sitemap_crawl_log (
    id          BIGSERIAL PRIMARY KEY,
    shop        TEXT NOT NULL,
    sitemap_url TEXT NOT NULL,
    crawled_at  TIMESTAMPTZ DEFAULT NOW(),
    urls_found  INTEGER DEFAULT 0,
    success     BOOLEAN DEFAULT TRUE,
    error_msg   TEXT
);
CREATE INDEX IF NOT EXISTS idx_crawl_log_shop ON sitemap_crawl_log(shop, crawled_at DESC);

-- Hlídač Shopů API results (standalone, no FK)
CREATE TABLE IF NOT EXISTS hlidac_shopu_data (
    id            BIGSERIAL PRIMARY KEY,
    product_url   TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ DEFAULT NOW(),
    min_price     NUMERIC(12,2),
    max_price     NUMERIC(12,2),
    current_price NUMERIC(12,2),
    currency      VARCHAR(3) DEFAULT 'CZK',
    raw_json      JSONB
);
CREATE INDEX IF NOT EXISTS idx_hlidac_url ON hlidac_shopu_data(product_url);
CREATE INDEX IF NOT EXISTS idx_hlidac_fetched ON hlidac_shopu_data(fetched_at DESC);
