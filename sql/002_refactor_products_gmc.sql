-- ============================================================================
-- Migration 002: Refactor products to Google Merchant Center compatible model
-- ============================================================================
-- Strategy: 3-layer model (catalog / offer / price_history)
--   Current `products` (5144 rows, catalog+offer mixed) → splits into:
--     products (master catalog, 1 row per GTIN)
--     product_offers (shop × country × product, current snapshot)
--     offer_price_history (time-series, partitioned monthly)
--
-- Safety:
--   - Forward-only. BACKUP DB BEFORE RUNNING.
--   - Old products table preserved as `products_legacy` until verification.
--   - Run in transaction; rollback if any step fails.
--
-- Tested on: PostgreSQL 17 (Neon)
-- TimescaleDB-ready: offer_price_history uses native PG partitioning by month.
--   On self-hosted migration, convert to hypertable + compression.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- STEP 0: Extensions
-- ----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- trigram search on title
CREATE EXTENSION IF NOT EXISTS "unaccent";     -- for title_normalized
CREATE EXTENSION IF NOT EXISTS "pgcrypto";     -- gen_random_uuid()

-- ----------------------------------------------------------------------------
-- STEP 1: Create ENUM types
-- ----------------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE availability_status AS ENUM (
        'in_stock', 'out_of_stock', 'preorder', 'backorder', 'discontinued', 'unknown'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE product_condition AS ENUM ('new', 'refurbished', 'used', 'open_box');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE gtin_type AS ENUM (
        'gtin8', 'gtin12', 'gtin13', 'gtin14', 'mpn', 'asin', 'isbn'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE size_system AS ENUM (
        'EU', 'US', 'UK', 'DE', 'FR', 'IT', 'JP', 'CN', 'BR', 'AU', 'MEX'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE feed_source AS ENUM (
        'affiliate_awin', 'affiliate_cj', 'affiliate_tradedoubler', 'affiliate_daisycon',
        'jsonld_scrape', 'microdata_scrape', 'sitemap_scrape',
        'hlidac_shopu', 'manual', 'api_merchant'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ----------------------------------------------------------------------------
-- STEP 2: Rename old products table, preserve as legacy backup
-- ----------------------------------------------------------------------------
ALTER TABLE products RENAME TO products_legacy;
ALTER TABLE products_legacy RENAME CONSTRAINT products_pkey TO products_legacy_pkey;

-- Preserve old FKs by dropping them (will be re-added to new structure)
-- Find and log FKs pointing to products_legacy for later re-wiring
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT conname, conrelid::regclass AS table_name
        FROM pg_constraint
        WHERE confrelid = 'products_legacy'::regclass
    LOOP
        RAISE NOTICE 'Legacy FK to rewire: %.%', r.table_name, r.conname;
    END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- STEP 3: Create NEW products (master catalog)
-- ----------------------------------------------------------------------------
CREATE TABLE products (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identity
    gtin13                  varchar(13),
    gtin12                  varchar(12),
    gtin14                  varchar(14),
    gtin8                   varchar(8),
    mpn                     varchar(70),
    identifier_exists       boolean NOT NULL DEFAULT true,

    -- Catalog data
    brand_id                uuid NOT NULL REFERENCES brands(id) ON DELETE RESTRICT,
    category_id             uuid REFERENCES product_categories(id) ON DELETE SET NULL,
    google_product_category varchar(500),

    title                   varchar(150) NOT NULL,
    title_normalized        varchar(200) GENERATED ALWAYS AS (lower(unaccent(title))) STORED,
    description             text,
    condition               product_condition NOT NULL DEFAULT 'new',

    -- Variant support
    item_group_id           uuid,
    is_variant              boolean NOT NULL DEFAULT false,
    color                   varchar(100),
    size                    varchar(50),
    size_system             size_system,
    material                varchar(100),
    pattern                 varchar(100),
    gender                  varchar(20),
    age_group               varchar(20),

    -- Beauty-specific
    volume_ml               numeric(10,2),
    volume_unit_raw         varchar(20),
    volume_value_raw        numeric(10,2),
    scent_notes             text[],
    ingredients             text,
    fragrance_family        varchar(50),
    concentration           varchar(20),

    -- Packaging
    is_bundle               boolean NOT NULL DEFAULT false,
    multipack_count         smallint,

    -- Highlights
    highlights              text[],

    -- Canonical images
    canonical_image_url     text,
    canonical_image_urls    text[],

    -- Lifecycle
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_at            timestamptz NOT NULL DEFAULT now(),
    enriched_at             timestamptz,
    is_active               boolean NOT NULL DEFAULT true,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    -- Raw data
    raw_feed_data           jsonb,

    -- Constraints
    CONSTRAINT products_has_identifier CHECK (
        gtin13 IS NOT NULL OR gtin12 IS NOT NULL OR gtin14 IS NOT NULL
        OR gtin8 IS NOT NULL OR mpn IS NOT NULL OR identifier_exists = false
    ),
    CONSTRAINT products_title_not_empty CHECK (length(btrim(title)) > 0),
    CONSTRAINT products_volume_positive CHECK (volume_ml IS NULL OR volume_ml > 0),
    CONSTRAINT products_multipack_positive CHECK (multipack_count IS NULL OR multipack_count >= 1),
    CONSTRAINT products_highlights_limit CHECK (highlights IS NULL OR array_length(highlights, 1) <= 10),
    CONSTRAINT products_canonical_images_limit CHECK (
        canonical_image_urls IS NULL OR array_length(canonical_image_urls, 1) <= 10
    )
);

-- Identity indexes (unique partial)
CREATE UNIQUE INDEX products_gtin13_uk ON products(gtin13) WHERE gtin13 IS NOT NULL;
CREATE UNIQUE INDEX products_gtin12_uk ON products(gtin12) WHERE gtin12 IS NOT NULL;
CREATE UNIQUE INDEX products_gtin8_uk  ON products(gtin8)  WHERE gtin8  IS NOT NULL;
CREATE UNIQUE INDEX products_gtin14_uk ON products(gtin14) WHERE gtin14 IS NOT NULL;
CREATE UNIQUE INDEX products_brand_mpn_uk ON products(brand_id, mpn) WHERE mpn IS NOT NULL;

-- Access pattern indexes
CREATE INDEX products_brand_id_idx         ON products(brand_id);
CREATE INDEX products_category_id_idx      ON products(category_id) WHERE category_id IS NOT NULL;
CREATE INDEX products_item_group_id_idx    ON products(item_group_id) WHERE item_group_id IS NOT NULL;
CREATE INDEX products_brand_category_idx   ON products(brand_id, category_id) WHERE is_active = true;
CREATE INDEX products_title_trgm_idx       ON products USING gin (title_normalized gin_trgm_ops);
CREATE INDEX products_highlights_gin_idx   ON products USING gin (highlights);
CREATE INDEX products_scent_notes_gin_idx  ON products USING gin (scent_notes);
CREATE INDEX products_raw_feed_gin_idx     ON products USING gin (raw_feed_data jsonb_path_ops);
CREATE INDEX products_last_seen_idx        ON products(last_seen_at DESC) WHERE is_active = true;

-- ----------------------------------------------------------------------------
-- STEP 4: Create product_offers (fact table)
-- ----------------------------------------------------------------------------
CREATE TABLE product_offers (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Relationships
    product_id              uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    shop_id                 uuid NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    country_id              uuid NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,

    -- Shop identifiers
    external_sku            varchar(100) NOT NULL,
    external_id             varchar(100),

    -- URLs
    product_url             text NOT NULL,
    mobile_url              text,
    affiliate_url           text,
    affiliate_network_id    uuid REFERENCES affiliate_networks(id) ON DELETE SET NULL,

    -- Pricing
    price                   numeric(12,2) NOT NULL,
    price_currency          char(3) NOT NULL,
    sale_price              numeric(12,2),
    sale_price_start        timestamptz,
    sale_price_end          timestamptz,
    cost_of_goods_sold      numeric(12,2),

    -- Denormalized volume snapshot for generated column
    volume_ml_snapshot      numeric(10,2),

    -- Generated columns
    effective_price         numeric(12,2) GENERATED ALWAYS AS (
        COALESCE(sale_price, price)
    ) STORED,
    discount_pct            numeric(5,2) GENERATED ALWAYS AS (
        CASE WHEN price > 0 AND sale_price IS NOT NULL AND sale_price < price
        THEN ROUND(((price - sale_price) / price * 100)::numeric, 2)
        ELSE 0 END
    ) STORED,
    unit_price_per_ml       numeric(12,4) GENERATED ALWAYS AS (
        CASE WHEN volume_ml_snapshot > 0
        THEN COALESCE(sale_price, price) / volume_ml_snapshot
        ELSE NULL END
    ) STORED,

    -- Availability
    availability            availability_status NOT NULL DEFAULT 'unknown',
    quantity_available      integer,
    last_in_stock_at        timestamptz,
    last_out_of_stock_at    timestamptz,

    -- Shipping
    delivery_days_min       smallint,
    delivery_days_max       smallint,
    shipping_price          numeric(10,2),
    free_shipping_threshold numeric(10,2),
    shipping_weight_g       integer,

    -- Per-offer images
    image_url               text,
    additional_image_urls   text[],

    -- Per-offer title/desc
    offer_title             varchar(200),
    offer_description       text,

    -- Feed metadata
    source                  feed_source NOT NULL,
    last_feed_run_id        uuid,
    raw_offer_data          jsonb,

    -- Custom labels
    custom_label_0          varchar(100),
    custom_label_1          varchar(100),
    custom_label_2          varchar(100),
    custom_label_3          varchar(100),
    custom_label_4          varchar(100),

    -- Lifecycle
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_at            timestamptz NOT NULL DEFAULT now(),
    is_active               boolean NOT NULL DEFAULT true,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    -- Constraints
    CONSTRAINT offers_price_positive CHECK (price >= 0),
    CONSTRAINT offers_sale_price_valid CHECK (sale_price IS NULL OR sale_price >= 0),
    CONSTRAINT offers_sale_price_window CHECK (
        sale_price_end IS NULL OR sale_price_start IS NULL
        OR sale_price_end > sale_price_start
    ),
    CONSTRAINT offers_currency_format CHECK (price_currency ~ '^[A-Z]{3}$'),
    CONSTRAINT offers_delivery_range CHECK (
        delivery_days_max IS NULL OR delivery_days_min IS NULL
        OR delivery_days_max >= delivery_days_min
    ),
    CONSTRAINT offers_additional_images_limit CHECK (
        additional_image_urls IS NULL OR array_length(additional_image_urls, 1) <= 10
    )
);

-- Unique: each shop has one offer per SKU
CREATE UNIQUE INDEX offers_shop_sku_uk ON product_offers(shop_id, external_sku);

-- FK indexes
CREATE INDEX offers_product_id_idx         ON product_offers(product_id);
CREATE INDEX offers_shop_id_idx            ON product_offers(shop_id);
CREATE INDEX offers_country_id_idx         ON product_offers(country_id);
CREATE INDEX offers_affiliate_network_idx  ON product_offers(affiliate_network_id)
    WHERE affiliate_network_id IS NOT NULL;

-- Access patterns
CREATE INDEX offers_product_price_idx      ON product_offers(product_id, effective_price ASC)
    WHERE availability = 'in_stock' AND is_active = true;
CREATE INDEX offers_product_country_idx    ON product_offers(product_id, country_id, effective_price ASC)
    WHERE is_active = true;
CREATE INDEX offers_country_product_idx    ON product_offers(country_id, product_id)
    WHERE availability = 'in_stock' AND is_active = true;
CREATE INDEX offers_shop_updated_idx       ON product_offers(shop_id, updated_at DESC);
CREATE INDEX offers_oos_idx                ON product_offers(last_in_stock_at DESC)
    WHERE availability = 'out_of_stock';
CREATE INDEX offers_discount_idx           ON product_offers(discount_pct DESC, updated_at DESC)
    WHERE discount_pct > 0 AND is_active = true;
CREATE INDEX offers_raw_offer_gin_idx      ON product_offers USING gin (raw_offer_data jsonb_path_ops);

-- ----------------------------------------------------------------------------
-- STEP 5: Create product_identifiers (additional GTIN variants, ASIN, etc.)
-- ----------------------------------------------------------------------------
CREATE TABLE product_identifiers (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id       uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    identifier_type  gtin_type NOT NULL,
    identifier_value varchar(100) NOT NULL,
    source           feed_source,
    verified         boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT product_identifiers_uk UNIQUE (identifier_type, identifier_value)
);

CREATE INDEX product_identifiers_product_idx ON product_identifiers(product_id);

-- ----------------------------------------------------------------------------
-- STEP 6: Create product_variant_groups (for itemGroupId grouping)
-- ----------------------------------------------------------------------------
CREATE TABLE product_variant_groups (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id     uuid NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    category_id  uuid REFERENCES product_categories(id) ON DELETE SET NULL,
    group_title  varchar(200) NOT NULL,
    group_slug   varchar(250) NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX product_variant_groups_slug_uk ON product_variant_groups(group_slug);
CREATE INDEX product_variant_groups_brand_idx      ON product_variant_groups(brand_id);

-- Wire products.item_group_id → product_variant_groups.id
ALTER TABLE products
    ADD CONSTRAINT products_item_group_fk
    FOREIGN KEY (item_group_id) REFERENCES product_variant_groups(id) ON DELETE SET NULL;

-- ----------------------------------------------------------------------------
-- STEP 7: Create offer_price_history (partitioned time-series)
-- ----------------------------------------------------------------------------
CREATE TABLE offer_price_history (
    id                 bigserial,
    offer_id           uuid NOT NULL,
    product_id         uuid NOT NULL,
    shop_id            uuid NOT NULL,
    country_id         uuid NOT NULL,

    recorded_at        timestamptz NOT NULL DEFAULT now(),

    price              numeric(12,2) NOT NULL,
    sale_price         numeric(12,2),
    price_currency     char(3) NOT NULL,
    availability       availability_status NOT NULL,
    quantity_available integer,
    source             feed_source NOT NULL,

    PRIMARY KEY (recorded_at, id)
) PARTITION BY RANGE (recorded_at);

-- Create initial partitions (24 months forward from current month)
DO $$
DECLARE
    start_date date := date_trunc('month', CURRENT_DATE)::date;
    end_date date;
    partition_name text;
    i int;
BEGIN
    FOR i IN 0..23 LOOP
        end_date := (start_date + INTERVAL '1 month')::date;
        partition_name := 'offer_price_history_' || to_char(start_date, 'YYYY_MM');
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF offer_price_history FOR VALUES FROM (%L) TO (%L)',
            partition_name, start_date, end_date
        );
        start_date := end_date;
    END LOOP;
END $$;

CREATE INDEX offer_history_offer_time_idx   ON offer_price_history(offer_id, recorded_at DESC);
CREATE INDEX offer_history_product_time_idx ON offer_price_history(product_id, recorded_at DESC);
CREATE INDEX offer_history_shop_time_idx    ON offer_price_history(shop_id, recorded_at DESC);

-- ----------------------------------------------------------------------------
-- STEP 8: Migrate data from products_legacy → products + product_offers
-- ----------------------------------------------------------------------------
-- Legacy rows: (ean, sku, competitor_id, brand_id, category_id, name, url, price, ...)
-- Target: ONE master product per EAN (or per brand+sku fallback), MANY offers.

-- 8a. Insert master products (dedupe by EAN, then brand+name for no-EAN rows)
INSERT INTO products (
    id, gtin13, mpn, brand_id, category_id, title,
    description, canonical_image_url, volume_ml, volume_unit_raw, volume_value_raw,
    first_seen_at, last_seen_at, raw_feed_data, created_at, updated_at
)
SELECT DISTINCT ON (COALESCE(l.ean, l.brand_id::text || '::' || l.name))
    gen_random_uuid() AS id,
    CASE WHEN length(l.ean) = 13 THEN l.ean ELSE NULL END AS gtin13,
    l.sku AS mpn,
    l.brand_id,
    l.category_id,
    LEFT(l.name, 150) AS title,
    l.description,
    l.image_url AS canonical_image_url,
    l.volume_ml,
    l.volume AS volume_unit_raw,
    NULL AS volume_value_raw,
    COALESCE(l.scraped_at, now()) AS first_seen_at,
    COALESCE(l.scraped_at, now()) AS last_seen_at,
    l.raw_data,
    COALESCE(l.scraped_at, now()) AS created_at,
    COALESCE(l.scraped_at, now()) AS updated_at
FROM products_legacy l
WHERE l.brand_id IS NOT NULL
  AND l.name IS NOT NULL
  AND length(btrim(l.name)) > 0
ORDER BY COALESCE(l.ean, l.brand_id::text || '::' || l.name), l.scraped_at DESC NULLS LAST;

-- 8b. Create offers for each legacy row (join by matching product)
INSERT INTO product_offers (
    product_id, shop_id, country_id,
    external_sku, product_url, affiliate_url,
    price, price_currency, sale_price,
    volume_ml_snapshot, availability,
    delivery_days_min, delivery_days_max,
    image_url, source,
    first_seen_at, last_seen_at, created_at, updated_at
)
SELECT
    p.id AS product_id,
    l.competitor_id AS shop_id,
    l.country_id,
    COALESCE(l.sku, l.id::text) AS external_sku,
    l.url AS product_url,
    l.affiliate_url,
    COALESCE(l.price_original, l.price) AS price,
    COALESCE(l.currency, 'EUR') AS price_currency,
    CASE WHEN l.price_original IS NOT NULL AND l.price < l.price_original
         THEN l.price ELSE NULL END AS sale_price,
    p.volume_ml AS volume_ml_snapshot,
    CASE COALESCE(l.stock_status, CASE WHEN l.in_stock THEN 'in_stock' ELSE 'out_of_stock' END)
        WHEN 'in_stock' THEN 'in_stock'::availability_status
        WHEN 'out_of_stock' THEN 'out_of_stock'::availability_status
        WHEN 'preorder' THEN 'preorder'::availability_status
        WHEN 'backorder' THEN 'backorder'::availability_status
        ELSE 'unknown'::availability_status
    END AS availability,
    l.delivery_days AS delivery_days_min,
    l.delivery_days AS delivery_days_max,
    l.image_url,
    CASE l.source
        WHEN 'affiliate' THEN 'affiliate_awin'::feed_source
        WHEN 'jsonld' THEN 'jsonld_scrape'::feed_source
        WHEN 'sitemap' THEN 'sitemap_scrape'::feed_source
        WHEN 'hlidac_shopu' THEN 'hlidac_shopu'::feed_source
        ELSE 'manual'::feed_source
    END AS source,
    COALESCE(l.scraped_at, now()) AS first_seen_at,
    COALESCE(l.scraped_at, now()) AS last_seen_at,
    COALESCE(l.scraped_at, now()) AS created_at,
    COALESCE(l.scraped_at, now()) AS updated_at
FROM products_legacy l
JOIN products p ON
    (length(l.ean) = 13 AND p.gtin13 = l.ean)
    OR (l.ean IS NULL AND p.brand_id = l.brand_id AND p.mpn = l.sku)
WHERE l.competitor_id IS NOT NULL
  AND l.country_id IS NOT NULL
  AND l.url IS NOT NULL
  AND l.price IS NOT NULL
ON CONFLICT (shop_id, external_sku) DO NOTHING;

-- ----------------------------------------------------------------------------
-- STEP 9: Rewire price_history FK (currently points to products_legacy)
-- ----------------------------------------------------------------------------
-- Old price_history.product_id → products_legacy.id
-- New: we keep price_history as-is but archive it; new data goes to offer_price_history.
-- If price_history has data, migrate; if empty (per seed), skip.

DO $$
DECLARE
    row_count bigint;
BEGIN
    SELECT count(*) INTO row_count FROM price_history;
    IF row_count > 0 THEN
        RAISE NOTICE 'price_history has % rows — manual migration required', row_count;
        -- Add offer_id column to old price_history for backward compat
        ALTER TABLE price_history ADD COLUMN IF NOT EXISTS offer_id uuid;
    ELSE
        RAISE NOTICE 'price_history empty — safe to drop/recreate';
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- STEP 10: Triggers for updated_at
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER products_updated_at
    BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER product_offers_updated_at
    BEFORE UPDATE ON product_offers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER product_variant_groups_updated_at
    BEFORE UPDATE ON product_variant_groups
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- STEP 11: Verification queries (run AFTER migration)
-- ----------------------------------------------------------------------------
-- Uncomment and run:
-- SELECT 'products' AS tbl, count(*) FROM products
-- UNION ALL SELECT 'product_offers', count(*) FROM product_offers
-- UNION ALL SELECT 'products_legacy', count(*) FROM products_legacy;
--
-- SELECT 'products with EAN' AS metric, count(*) FROM products WHERE gtin13 IS NOT NULL
-- UNION ALL SELECT 'products without EAN', count(*) FROM products WHERE gtin13 IS NULL
-- UNION ALL SELECT 'offers in stock', count(*) FROM product_offers WHERE availability = 'in_stock'
-- UNION ALL SELECT 'offers out of stock', count(*) FROM product_offers WHERE availability = 'out_of_stock';
--
-- -- Cross-shop coverage (how many shops per product, should show beauty top products)
-- SELECT p.title, p.gtin13, count(DISTINCT o.shop_id) AS shop_count
-- FROM products p JOIN product_offers o ON o.product_id = p.id
-- GROUP BY p.id, p.title, p.gtin13
-- ORDER BY shop_count DESC LIMIT 20;

COMMIT;

-- ----------------------------------------------------------------------------
-- POST-MIGRATION: after verification (separate transaction)
-- ----------------------------------------------------------------------------
-- Only run after app is updated + verified for 1-2 weeks:
--   DROP TABLE products_legacy CASCADE;
-- ----------------------------------------------------------------------------
