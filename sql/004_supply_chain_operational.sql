-- ============================================================================
-- 004_supply_chain_operational.sql
-- Supply Chain Layer: Operational distributor extension
-- ============================================================================
--
-- Prereq: Existing tables — brands(id), competitors(id), countries(id, iso2)
-- Prereq: Supply chain tables from supply-chain-map.md MUST be migrated first:
--   brand_groups, distributors, distributor_countries, distributor_brands,
--   brand_group_subsidiaries, wholesale_sources
--
-- This migration EXTENDS the distributor layer with operational-level detail:
--   1. Commercial terms (payment, pricing, returns)
--   2. Operations/logistics (warehouses, carriers, lead times)
--   3. Contacts & relationships (people, notes, onboarding)
--   4. Catalog & data integration (feeds, APIs, B2B portals)
--   5. Intelligence (margins, reliability, gray market risk)
--   6. Nakpuci automation readiness (checkout, CAPTCHA, purchase history)
--
-- Run AFTER the base supply-chain tables exist.
-- Run: psql "$DATABASE_URL" -f sql/004_supply_chain_operational.sql
-- ============================================================================

BEGIN;

-- ============================================================================
-- STEP 1: ALTER distributors — add operational columns
-- ============================================================================
-- These columns belong ON the distributor entity itself because they are
-- 1:1 with the distributor and queried in list/filter views.

-- Legal identity
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS legal_name TEXT;
COMMENT ON COLUMN distributors.legal_name IS 'Full legal entity name (e.g. "Glamour Distribuce, a.s.")';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS ico TEXT;
COMMENT ON COLUMN distributors.ico IS 'CZ IČO / PL NIP / DE Handelsregister — national business ID';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS vat_id TEXT;
COMMENT ON COLUMN distributors.vat_id IS 'EU VAT number (e.g. CZ27753174)';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS founded_year INTEGER;

-- Our relationship status with this distributor
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS account_status TEXT NOT NULL DEFAULT 'prospect';
COMMENT ON COLUMN distributors.account_status IS 'Our status: prospect → contacted → registered → active → suspended → churned';
-- CHECK added below after all ALTERs

-- Commercial terms (1:1 with distributor, queried in filters)
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS payment_terms_days INTEGER;
COMMENT ON COLUMN distributors.payment_terms_days IS 'Standard payment terms in days (e.g. 30 = net 30). NULL = unknown.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS requires_prepayment BOOLEAN DEFAULT false;
COMMENT ON COLUMN distributors.requires_prepayment IS 'True if first/all orders require prepayment before shipping.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS bnpl_available BOOLEAN DEFAULT false;
COMMENT ON COLUMN distributors.bnpl_available IS 'Offers Buy Now Pay Later terms for SME buyers.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS min_order_eur NUMERIC(10,2);
COMMENT ON COLUMN distributors.min_order_eur IS 'Minimum order value in EUR (overall, not per-brand). NULL = unknown.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS returns_policy TEXT;
COMMENT ON COLUMN distributors.returns_policy IS 'Free-text summary of returns/claims policy and conditions.';

-- Logistics summary (1:1 scalars)
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS lead_time_days_min INTEGER;
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS lead_time_days_max INTEGER;
COMMENT ON COLUMN distributors.lead_time_days_min IS 'Typical minimum delivery lead time in business days.';
COMMENT ON COLUMN distributors.lead_time_days_max IS 'Typical maximum delivery lead time in business days.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS shipping_model TEXT;
COMMENT ON COLUMN distributors.shipping_model IS 'Primary incoterm/shipping model: FOB, DDP, EXW, DAP, etc.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS inventory_visibility TEXT NOT NULL DEFAULT 'unknown';
COMMENT ON COLUMN distributors.inventory_visibility IS 'How we can see their stock: api / b2b_portal / spreadsheet / email_only / unknown';

-- Catalog & data integration (1:1 scalars)
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS product_feed_format TEXT;
COMMENT ON COLUMN distributors.product_feed_format IS 'Feed format if available: xml / csv / json / api / none';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS product_feed_url TEXT;
COMMENT ON COLUMN distributors.product_feed_url IS 'URL of the product feed (if publicly accessible or we have access).';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS ean_coverage_pct INTEGER;
COMMENT ON COLUMN distributors.ean_coverage_pct IS 'Estimated % of their catalog that has valid EAN/GTIN. 0-100.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS price_update_frequency TEXT;
COMMENT ON COLUMN distributors.price_update_frequency IS 'How often prices are updated: realtime / daily / weekly / monthly / manual';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS has_media_assets BOOLEAN DEFAULT false;
COMMENT ON COLUMN distributors.has_media_assets IS 'Provides product photos/media assets to buyers.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS api_docs_url TEXT;
COMMENT ON COLUMN distributors.api_docs_url IS 'URL to API documentation, if any.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS b2b_portal_url TEXT;
COMMENT ON COLUMN distributors.b2b_portal_url IS 'B2B portal URL for manual ordering / catalog browsing.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS b2b_credentials_stored BOOLEAN DEFAULT false;
COMMENT ON COLUMN distributors.b2b_credentials_stored IS 'Whether we have stored B2B portal credentials (in secrets vault, NOT in DB).';

-- Intelligence scores
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS avg_discount_vs_rrp_pct NUMERIC(5,2);
COMMENT ON COLUMN distributors.avg_discount_vs_rrp_pct IS 'Average discount vs RRP as percentage (e.g. 25.5 = 25.5% below RRP). Estimated.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS delivery_reliability_score INTEGER;
COMMENT ON COLUMN distributors.delivery_reliability_score IS 'Delivery reliability score 0-100 based on our experience. NULL = no experience yet.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS gray_market_risk TEXT NOT NULL DEFAULT 'unknown';
COMMENT ON COLUMN distributors.gray_market_risk IS 'Gray market risk assessment: none / low / medium / high / unknown';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS gray_market_risk_notes TEXT;
COMMENT ON COLUMN distributors.gray_market_risk_notes IS 'Justification for the gray market risk score.';

-- Nakpuci automation
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS automation_readiness TEXT NOT NULL DEFAULT 'unknown';
COMMENT ON COLUMN distributors.automation_readiness IS 'Automation level: api / b2b_portal / email_only / unknown. Determines Nakpuci approach.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS checkout_flow_notes TEXT;
COMMENT ON COLUMN distributors.checkout_flow_notes IS 'Description of the B2B checkout flow for automation planning.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS has_captcha BOOLEAN;
COMMENT ON COLUMN distributors.has_captcha IS 'B2B portal/checkout has CAPTCHA. NULL = unknown.';

ALTER TABLE distributors ADD COLUMN IF NOT EXISTS onboarding_requirements TEXT;
COMMENT ON COLUMN distributors.onboarding_requirements IS 'What they need to approve us as buyer (trade license, VAT, references, etc.).';

-- Onboarding timestamp
ALTER TABLE distributors ADD COLUMN IF NOT EXISTS registered_at TIMESTAMPTZ;
COMMENT ON COLUMN distributors.registered_at IS 'When we registered/were approved as buyer. NULL = not registered.';

-- CHECK constraints on new enum-like columns
-- Using DO block to make idempotent (constraint may already exist on re-run)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_account_status') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_account_status
            CHECK (account_status IN ('prospect', 'contacted', 'registered', 'active', 'suspended', 'churned'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_inventory_visibility') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_inventory_visibility
            CHECK (inventory_visibility IN ('api', 'b2b_portal', 'spreadsheet', 'email_only', 'unknown'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_gray_market_risk') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_gray_market_risk
            CHECK (gray_market_risk IN ('none', 'low', 'medium', 'high', 'unknown'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_automation_readiness') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_automation_readiness
            CHECK (automation_readiness IN ('api', 'b2b_portal', 'email_only', 'unknown'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_feed_format') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_feed_format
            CHECK (product_feed_format IS NULL OR product_feed_format IN ('xml', 'csv', 'json', 'api', 'none'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_price_frequency') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_price_frequency
            CHECK (price_update_frequency IS NULL OR price_update_frequency IN ('realtime', 'daily', 'weekly', 'monthly', 'manual'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_shipping_model') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_shipping_model
            CHECK (shipping_model IS NULL OR shipping_model IN ('FOB', 'DDP', 'EXW', 'DAP', 'CIF', 'FCA', 'other'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_ean_coverage') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_ean_coverage
            CHECK (ean_coverage_pct IS NULL OR (ean_coverage_pct >= 0 AND ean_coverage_pct <= 100));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_reliability') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_reliability
            CHECK (delivery_reliability_score IS NULL OR (delivery_reliability_score >= 0 AND delivery_reliability_score <= 100));
    END IF;
END
$$;


-- ============================================================================
-- STEP 2: distributor_contacts — multiple contact persons per distributor
-- ============================================================================
COMMENT ON TABLE distributors IS 'Supply chain distributors — national, regional, wholesale, B2B platforms. Extended with operational detail for Nakpuci and competitive intelligence.';

CREATE TABLE IF NOT EXISTS distributor_contacts (
    id              SERIAL PRIMARY KEY,
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    full_name       TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'general',
    -- role: account_manager / sales_rep / logistics / finance / general / executive
    email           TEXT,
    phone           TEXT,
    linkedin_url    TEXT,
    is_primary      BOOLEAN DEFAULT false,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE distributor_contacts IS 'Contact persons at each distributor. One distributor can have multiple contacts with different roles.';
COMMENT ON COLUMN distributor_contacts.role IS 'Contact role: account_manager / sales_rep / logistics / finance / general / executive';
COMMENT ON COLUMN distributor_contacts.is_primary IS 'Our primary point of contact at this distributor.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_contact_role') THEN
        ALTER TABLE distributor_contacts ADD CONSTRAINT chk_contact_role
            CHECK (role IN ('account_manager', 'sales_rep', 'logistics', 'finance', 'general', 'executive'));
    END IF;
END
$$;


-- ============================================================================
-- STEP 3: distributor_warehouses — physical warehouse locations
-- ============================================================================

CREATE TABLE IF NOT EXISTS distributor_warehouses (
    id              SERIAL PRIMARY KEY,
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    name            TEXT,                              -- "Main warehouse", "Brno facility"
    address         TEXT,
    city            TEXT,
    country_iso2    CHAR(2) NOT NULL REFERENCES countries(iso2),
    postal_code     TEXT,
    is_primary      BOOLEAN DEFAULT false,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE distributor_warehouses IS 'Physical warehouse/fulfillment locations per distributor. Affects shipping times and costs by country.';
COMMENT ON COLUMN distributor_warehouses.is_primary IS 'Primary warehouse — orders ship from here by default.';


-- ============================================================================
-- STEP 4: distributor_carriers — shipping carriers used by each distributor
-- ============================================================================

CREATE TABLE IF NOT EXISTS distributor_carriers (
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    carrier_name    TEXT NOT NULL,  -- "UPS", "DHL", "SUUS", "DACHSER", "RABEN", "PPL"
    service_type    TEXT,           -- "parcel" / "pallet" / "container" / "express"
    max_weight_kg   NUMERIC(8,2),  -- weight limit for this carrier option
    notes           TEXT,
    PRIMARY KEY (distributor_id, carrier_name)
);

COMMENT ON TABLE distributor_carriers IS 'Shipping carriers available at each distributor. Junction table with composite PK.';
COMMENT ON COLUMN distributor_carriers.service_type IS 'Type of shipping service: parcel / pallet / container / express';


-- ============================================================================
-- STEP 5: distributor_volume_tiers — volume discount / pricing tiers
-- ============================================================================

CREATE TABLE IF NOT EXISTS distributor_volume_tiers (
    id              SERIAL PRIMARY KEY,
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    brand_id        INTEGER REFERENCES brands(id) ON DELETE SET NULL,
    -- brand_id NULL = applies to all brands at this distributor
    tier_name       TEXT,                              -- "Standard", "Silver", "Gold", "Platinum"
    min_order_eur   NUMERIC(10,2) NOT NULL,            -- threshold to qualify for this tier
    discount_pct    NUMERIC(5,2),                      -- discount % off list price at this tier
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE distributor_volume_tiers IS 'Volume discount tiers per distributor, optionally per brand. Defines pricing structure.';
COMMENT ON COLUMN distributor_volume_tiers.brand_id IS 'NULL = tier applies to all brands. Non-NULL = brand-specific tier (e.g. higher MOV for luxury brands).';
COMMENT ON COLUMN distributor_volume_tiers.min_order_eur IS 'Minimum order value in EUR to qualify for this tier.';


-- ============================================================================
-- STEP 6: distributor_territorial_restrictions
-- ============================================================================
-- Separate from distributor_countries (which is "where they operate").
-- This captures contractual selling restrictions they impose on US as buyer.

CREATE TABLE IF NOT EXISTS distributor_territorial_restrictions (
    id              SERIAL PRIMARY KEY,
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    brand_id        INTEGER REFERENCES brands(id) ON DELETE SET NULL,
    -- brand_id NULL = restriction applies across all brands
    restriction_type TEXT NOT NULL DEFAULT 'territory',
    -- territory = cannot resell in certain countries
    -- channel = cannot sell on certain channels (marketplace, etc.)
    -- exclusivity = exclusive arrangement, must buy X from them only
    restricted_countries TEXT[],    -- ISO2 codes where resale is prohibited
    restricted_channels  TEXT[],   -- "amazon", "ebay", "allegro", "marketplace"
    description     TEXT NOT NULL,
    contract_ref    TEXT,           -- reference to contract clause if known
    valid_from      DATE,
    valid_until     DATE,          -- NULL = indefinite
    created_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE distributor_territorial_restrictions IS 'Contractual restrictions distributors impose on buyers — territory limits, channel bans, exclusivity clauses.';
COMMENT ON COLUMN distributor_territorial_restrictions.restriction_type IS 'Type: territory (country-level resale ban) / channel (marketplace ban) / exclusivity (must buy from them)';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_restriction_type') THEN
        ALTER TABLE distributor_territorial_restrictions ADD CONSTRAINT chk_restriction_type
            CHECK (restriction_type IN ('territory', 'channel', 'exclusivity'));
    END IF;
END
$$;


-- ============================================================================
-- STEP 7: distributor_notes — timestamped communication history
-- ============================================================================

CREATE TABLE IF NOT EXISTS distributor_notes (
    id              SERIAL PRIMARY KEY,
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    note_type       TEXT NOT NULL DEFAULT 'general',
    -- general / call / email / meeting / onboarding / issue / intel
    subject         TEXT,
    body            TEXT NOT NULL,
    author          TEXT,          -- who wrote this note (our team member name)
    created_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE distributor_notes IS 'Timestamped notes and communication history per distributor. Append-only log.';
COMMENT ON COLUMN distributor_notes.note_type IS 'Note category: general / call / email / meeting / onboarding / issue / intel';
COMMENT ON COLUMN distributor_notes.author IS 'Name of team member who created the note. Free text (no auth system).';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_note_type') THEN
        ALTER TABLE distributor_notes ADD CONSTRAINT chk_note_type
            CHECK (note_type IN ('general', 'call', 'email', 'meeting', 'onboarding', 'issue', 'intel'));
    END IF;
END
$$;


-- ============================================================================
-- STEP 8: distributor_known_customers — link distributors to competitors
-- ============================================================================
-- "Known customers" = other retailers we track (in competitors table) who
-- also buy from this distributor. Competitive intelligence.

CREATE TABLE IF NOT EXISTS distributor_known_customers (
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    competitor_id   INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    confidence      TEXT NOT NULL DEFAULT 'suspected',
    -- confirmed = verified via distributor website, public reference, or direct knowledge
    -- suspected = inferred from product overlap, pricing patterns, or industry rumor
    source          TEXT,          -- how we know: "distributor website", "industry source", "price analysis"
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (distributor_id, competitor_id)
);

COMMENT ON TABLE distributor_known_customers IS 'Links distributors to competitors (retailers) who buy from them. Competitive intelligence — shows supply chain relationships.';
COMMENT ON COLUMN distributor_known_customers.confidence IS 'Confidence level: confirmed (verified) / suspected (inferred)';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_customer_confidence') THEN
        ALTER TABLE distributor_known_customers ADD CONSTRAINT chk_customer_confidence
            CHECK (confidence IN ('confirmed', 'suspected'));
    END IF;
END
$$;


-- ============================================================================
-- STEP 9: distributor_brand_auth — per-brand authorization verification
-- ============================================================================
-- Different from distributor_brands (which says "they carry this brand").
-- This table tracks whether their authorization is VERIFIED by us.

CREATE TABLE IF NOT EXISTS distributor_brand_auth (
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    brand_id        INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    auth_status     TEXT NOT NULL DEFAULT 'unverified',
    -- authorized = confirmed by brand/brand group
    -- unauthorized = confirmed NOT authorized (gray market)
    -- unverified = we haven't checked yet
    -- expired = was authorized, no longer
    verified_at     TIMESTAMPTZ,
    verified_by     TEXT,           -- who verified (our team member or "brand website", "brand confirmation email")
    verification_source TEXT,       -- "brand website listing", "email from L'Oreal CZ", "industry contact"
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (distributor_id, brand_id)
);

COMMENT ON TABLE distributor_brand_auth IS 'Per-brand authorization verification status. Tracks whether a distributor is genuinely authorized to sell each brand.';
COMMENT ON COLUMN distributor_brand_auth.auth_status IS 'Authorization: authorized / unauthorized (gray market) / unverified / expired';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_auth_status') THEN
        ALTER TABLE distributor_brand_auth ADD CONSTRAINT chk_auth_status
            CHECK (auth_status IN ('authorized', 'unauthorized', 'unverified', 'expired'));
    END IF;
END
$$;


-- ============================================================================
-- STEP 10: nakpuci_purchases — our purchase history from distributors
-- ============================================================================
-- Tracks actual orders we place through distributors (manual or automated).
-- This is OUR data, not scraped — it comes from Nakpuci or manual ordering.

CREATE TABLE IF NOT EXISTS nakpuci_purchases (
    id              SERIAL PRIMARY KEY,
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE RESTRICT,
    -- RESTRICT: do not delete distributor if we have purchase history
    order_ref       TEXT,                              -- their order/invoice number
    order_date      DATE NOT NULL,
    total_eur       NUMERIC(12,2) NOT NULL,
    total_items     INTEGER NOT NULL DEFAULT 0,
    currency        CHAR(3) NOT NULL DEFAULT 'EUR',
    total_original  NUMERIC(12,2),                     -- total in original currency if not EUR
    status          TEXT NOT NULL DEFAULT 'placed',
    -- placed → confirmed → shipped → delivered → completed
    -- or: placed → cancelled
    delivery_days_actual INTEGER,                      -- actual days from order to delivery
    automation_method TEXT,                             -- manual / api / b2b_portal_bot / email_bot
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE nakpuci_purchases IS 'Our purchase orders from distributors. Tracks spend, delivery performance, and automation method.';
COMMENT ON COLUMN nakpuci_purchases.order_ref IS 'Distributor order/invoice reference number.';
COMMENT ON COLUMN nakpuci_purchases.automation_method IS 'How the order was placed: manual / api / b2b_portal_bot / email_bot';
COMMENT ON COLUMN nakpuci_purchases.delivery_days_actual IS 'Actual delivery time in days — used to calculate delivery_reliability_score on distributors.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_purchase_status') THEN
        ALTER TABLE nakpuci_purchases ADD CONSTRAINT chk_purchase_status
            CHECK (status IN ('placed', 'confirmed', 'shipped', 'delivered', 'completed', 'cancelled'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_purchase_automation') THEN
        ALTER TABLE nakpuci_purchases ADD CONSTRAINT chk_purchase_automation
            CHECK (automation_method IS NULL OR automation_method IN ('manual', 'api', 'b2b_portal_bot', 'email_bot'));
    END IF;
END
$$;


-- ============================================================================
-- STEP 11: nakpuci_purchase_items — line items per purchase
-- ============================================================================

CREATE TABLE IF NOT EXISTS nakpuci_purchase_items (
    id              SERIAL PRIMARY KEY,
    purchase_id     INTEGER NOT NULL REFERENCES nakpuci_purchases(id) ON DELETE CASCADE,
    product_id      INTEGER REFERENCES products(id) ON DELETE SET NULL,
    -- product_id can be NULL if the product is not yet in our catalog
    ean             TEXT,
    product_name    TEXT NOT NULL,                      -- as listed on invoice
    quantity        INTEGER NOT NULL DEFAULT 1,
    unit_price_eur  NUMERIC(10,2) NOT NULL,
    line_total_eur  NUMERIC(12,2) GENERATED ALWAYS AS (quantity * unit_price_eur) STORED,
    rrp_eur         NUMERIC(10,2),                     -- RRP for margin calculation
    margin_pct      NUMERIC(5,2) GENERATED ALWAYS AS (
        CASE
            WHEN rrp_eur IS NOT NULL AND rrp_eur > 0
            THEN ROUND(((rrp_eur - unit_price_eur) / rrp_eur) * 100, 2)
            ELSE NULL
        END
    ) STORED,
    created_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE nakpuci_purchase_items IS 'Line items for each purchase order. Links to products table when possible. Computed margin vs RRP.';
COMMENT ON COLUMN nakpuci_purchase_items.product_id IS 'FK to products. NULL if the SKU is not yet mapped in our catalog.';
COMMENT ON COLUMN nakpuci_purchase_items.ean IS 'EAN/GTIN from the invoice — used for matching to products.';
COMMENT ON COLUMN nakpuci_purchase_items.rrp_eur IS 'Recommended retail price in EUR — for margin calculation. NULL if unknown.';
COMMENT ON COLUMN nakpuci_purchase_items.margin_pct IS 'Computed: (RRP - unit_price) / RRP * 100. Measures buying efficiency.';


-- ============================================================================
-- STEP 12: INDEXES
-- ============================================================================

-- distributors: filter by account status (dashboard: "show me all active distributors")
CREATE INDEX IF NOT EXISTS idx_distributors_account_status ON distributors(account_status);

-- distributors: filter by automation readiness (Nakpuci: "API-ready distributors")
CREATE INDEX IF NOT EXISTS idx_distributors_automation ON distributors(automation_readiness);

-- distributors: filter by gray market risk
CREATE INDEX IF NOT EXISTS idx_distributors_gray_risk ON distributors(gray_market_risk);

-- distributors: filter by type (national/wholesale/b2b_platform)
-- NOTE: This index may already exist from base migration. IF NOT EXISTS makes it safe.
CREATE INDEX IF NOT EXISTS idx_distributors_type ON distributors(type);

-- distributor_contacts: lookup contacts for a distributor
CREATE INDEX IF NOT EXISTS idx_dist_contacts_distributor ON distributor_contacts(distributor_id);

-- distributor_warehouses: lookup warehouses by distributor
CREATE INDEX IF NOT EXISTS idx_dist_warehouses_distributor ON distributor_warehouses(distributor_id);

-- distributor_warehouses: find warehouses in a specific country
CREATE INDEX IF NOT EXISTS idx_dist_warehouses_country ON distributor_warehouses(country_iso2);

-- distributor_volume_tiers: lookup tiers for a distributor
CREATE INDEX IF NOT EXISTS idx_dist_tiers_distributor ON distributor_volume_tiers(distributor_id);

-- distributor_volume_tiers: lookup tiers for a specific brand
CREATE INDEX IF NOT EXISTS idx_dist_tiers_brand ON distributor_volume_tiers(brand_id) WHERE brand_id IS NOT NULL;

-- distributor_territorial_restrictions: lookup by distributor
CREATE INDEX IF NOT EXISTS idx_dist_restrictions_distributor ON distributor_territorial_restrictions(distributor_id);

-- distributor_notes: latest notes per distributor (dashboard timeline)
CREATE INDEX IF NOT EXISTS idx_dist_notes_distributor_time ON distributor_notes(distributor_id, created_at DESC);

-- distributor_known_customers: "who else buys from this distributor?"
-- Composite PK already covers (distributor_id, competitor_id) lookups.
-- Add reverse lookup: "which distributors supply this competitor?"
CREATE INDEX IF NOT EXISTS idx_dist_customers_competitor ON distributor_known_customers(competitor_id);

-- distributor_brand_auth: lookup auth status by brand
-- Composite PK covers (distributor_id, brand_id). Add reverse for brand-centric queries.
CREATE INDEX IF NOT EXISTS idx_dist_auth_brand ON distributor_brand_auth(brand_id);

-- distributor_brand_auth: filter unverified (prioritize verification work)
CREATE INDEX IF NOT EXISTS idx_dist_auth_status ON distributor_brand_auth(auth_status) WHERE auth_status = 'unverified';

-- nakpuci_purchases: lookup by distributor + date range (spend reports)
CREATE INDEX IF NOT EXISTS idx_purchases_distributor_date ON nakpuci_purchases(distributor_id, order_date DESC);

-- nakpuci_purchases: filter by status (operational dashboard)
CREATE INDEX IF NOT EXISTS idx_purchases_status ON nakpuci_purchases(status);

-- nakpuci_purchase_items: lookup items for a purchase
CREATE INDEX IF NOT EXISTS idx_purchase_items_purchase ON nakpuci_purchase_items(purchase_id);

-- nakpuci_purchase_items: find purchases of a specific product
CREATE INDEX IF NOT EXISTS idx_purchase_items_product ON nakpuci_purchase_items(product_id) WHERE product_id IS NOT NULL;

-- nakpuci_purchase_items: EAN lookup for matching
CREATE INDEX IF NOT EXISTS idx_purchase_items_ean ON nakpuci_purchase_items(ean) WHERE ean IS NOT NULL;


-- ============================================================================
-- STEP 13: updated_at auto-trigger
-- ============================================================================
-- Reusable function — may already exist in the DB from other migrations.

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply to all mutable tables with updated_at
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'distributors',
        'distributor_contacts',
        'distributor_brand_auth',
        'nakpuci_purchases'
    ]
    LOOP
        -- Drop if exists to make idempotent, then create
        EXECUTE format('DROP TRIGGER IF EXISTS trg_updated_at ON %I', tbl);
        EXECUTE format(
            'CREATE TRIGGER trg_updated_at BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
            tbl
        );
    END LOOP;
END
$$;


COMMIT;

-- ============================================================================
-- POST-MIGRATION VERIFICATION
-- ============================================================================
-- Run these manually to verify:
--
-- SELECT column_name, data_type, is_nullable
-- FROM information_schema.columns
-- WHERE table_name = 'distributors'
-- ORDER BY ordinal_position;
--
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public'
-- AND table_name LIKE 'distributor_%' OR table_name LIKE 'nakpuci_%'
-- ORDER BY table_name;
--
-- SELECT conname, contype, pg_get_constraintdef(oid)
-- FROM pg_constraint
-- WHERE conrelid = 'distributors'::regclass;
