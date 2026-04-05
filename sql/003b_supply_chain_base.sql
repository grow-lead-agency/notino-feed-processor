-- ============================================================================
-- 003b_supply_chain_base.sql
-- Supply Chain Layer: Base tables + seed data
-- ============================================================================
--
-- Creates: brand_groups, distributors, distributor_countries, distributor_brands,
--          brand_group_subsidiaries, wholesale_sources
-- ALTERs:  brands (add brand_group_id, distribution_tier)
--
-- Prereq: Existing tables — brands(id), countries(iso2)
-- Run: psql "$DATABASE_URL" -f sql/003b_supply_chain_base.sql
-- ============================================================================

BEGIN;

-- ============================================================================
-- STEP 1: brand_groups
-- ============================================================================

CREATE TABLE IF NOT EXISTS brand_groups (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    name_short          TEXT,
    hq_country          CHAR(2) REFERENCES countries(iso2),
    type                TEXT NOT NULL DEFAULT 'conglomerate',
    revenue_eur         BIGINT,
    website             TEXT,
    stock_ticker        TEXT,
    distribution_model  TEXT,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE brand_groups IS 'Parent companies owning beauty brands (L''Oreal, ELC, LVMH, Coty...). Links to brands via brands.brand_group_id.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_brand_groups_type') THEN
        ALTER TABLE brand_groups ADD CONSTRAINT chk_brand_groups_type
            CHECK (type IN ('conglomerate', 'independent', 'license_house'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_brand_groups_dist_model') THEN
        ALTER TABLE brand_groups ADD CONSTRAINT chk_brand_groups_dist_model
            CHECK (distribution_model IS NULL OR distribution_model IN ('direct_subsidiary', 'licensed_distributor', 'hybrid'));
    END IF;
END $$;


-- ============================================================================
-- STEP 2: ALTER brands — add brand_group_id, distribution_tier
-- ============================================================================

ALTER TABLE brands ADD COLUMN IF NOT EXISTS brand_group_id INTEGER REFERENCES brand_groups(id);
ALTER TABLE brands ADD COLUMN IF NOT EXISTS distribution_tier TEXT;

COMMENT ON COLUMN brands.brand_group_id IS 'FK to brand_groups — which conglomerate owns this brand.';
COMMENT ON COLUMN brands.distribution_tier IS 'Price/distribution segment: luxury / prestige / masstige / mass / dermo / professional';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_brands_dist_tier') THEN
        ALTER TABLE brands ADD CONSTRAINT chk_brands_dist_tier
            CHECK (distribution_tier IS NULL OR distribution_tier IN ('luxury', 'prestige', 'masstige', 'mass', 'dermo', 'professional'));
    END IF;
END $$;


-- ============================================================================
-- STEP 3: distributors
-- ============================================================================

CREATE TABLE IF NOT EXISTS distributors (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    name_short      TEXT,
    type            TEXT NOT NULL DEFAULT 'national',
    hq_country      CHAR(2) REFERENCES countries(iso2),
    website         TEXT,
    contact_email   TEXT,
    contact_phone   TEXT,
    is_authorized   BOOLEAN DEFAULT true,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE distributors IS 'Supply chain distributors — national, regional, wholesale, B2B platforms.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_distributors_type') THEN
        ALTER TABLE distributors ADD CONSTRAINT chk_distributors_type
            CHECK (type IN ('national', 'regional', 'wholesale', 'b2b_platform', 'brand_subsidiary'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_distributors_type ON distributors(type);
CREATE INDEX IF NOT EXISTS idx_distributors_hq_country ON distributors(hq_country);


-- ============================================================================
-- STEP 4: distributor_countries (M:N — where distributor operates)
-- ============================================================================

CREATE TABLE IF NOT EXISTS distributor_countries (
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    country_iso2    CHAR(2) NOT NULL REFERENCES countries(iso2),
    is_exclusive    BOOLEAN DEFAULT false,
    PRIMARY KEY (distributor_id, country_iso2)
);

COMMENT ON TABLE distributor_countries IS 'Countries where each distributor operates. is_exclusive = holds exclusive license for that country.';


-- ============================================================================
-- STEP 5: distributor_brands (M:N — which brands distributor carries)
-- ============================================================================

CREATE TABLE IF NOT EXISTS distributor_brands (
    distributor_id  INTEGER NOT NULL REFERENCES distributors(id) ON DELETE CASCADE,
    brand_id        INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    relationship    TEXT NOT NULL DEFAULT 'distributor',
    is_exclusive    BOOLEAN DEFAULT false,
    contract_start  DATE,
    contract_end    DATE,
    notes           TEXT,
    PRIMARY KEY (distributor_id, brand_id)
);

COMMENT ON TABLE distributor_brands IS 'M:N link between distributors and brands they carry.';
COMMENT ON COLUMN distributor_brands.relationship IS 'Type: distributor / licensee / wholesaler / reseller / direct';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_dist_brands_relationship') THEN
        ALTER TABLE distributor_brands ADD CONSTRAINT chk_dist_brands_relationship
            CHECK (relationship IN ('distributor', 'licensee', 'wholesaler', 'reseller', 'direct'));
    END IF;
END $$;


-- ============================================================================
-- STEP 6: brand_group_subsidiaries (where brand groups have direct offices)
-- ============================================================================

CREATE TABLE IF NOT EXISTS brand_group_subsidiaries (
    brand_group_id  INTEGER NOT NULL REFERENCES brand_groups(id) ON DELETE CASCADE,
    country_iso2    CHAR(2) NOT NULL REFERENCES countries(iso2),
    subsidiary_name TEXT,
    ico             TEXT,
    distribution_type TEXT DEFAULT 'direct',
    notes           TEXT,
    PRIMARY KEY (brand_group_id, country_iso2)
);

COMMENT ON TABLE brand_group_subsidiaries IS 'Countries where brand groups have direct national subsidiaries (e.g. L''Oreal CZ s.r.o.).';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_subsidiary_dist_type') THEN
        ALTER TABLE brand_group_subsidiaries ADD CONSTRAINT chk_subsidiary_dist_type
            CHECK (distribution_type IS NULL OR distribution_type IN ('direct', 'via_distributor', 'hybrid'));
    END IF;
END $$;


-- ============================================================================
-- STEP 7: wholesale_sources (B2B platforms — Qogita, Gabona...)
-- ============================================================================

CREATE TABLE IF NOT EXISTS wholesale_sources (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    website         TEXT,
    type            TEXT NOT NULL DEFAULT 'b2b_platform',
    hq_country      CHAR(2) REFERENCES countries(iso2),
    min_order_eur   NUMERIC(10,2),
    product_count   INTEGER,
    has_api         BOOLEAN DEFAULT false,
    risk_level      TEXT DEFAULT 'medium',
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE wholesale_sources IS 'B2B wholesale platforms and surplus sources. Relevant for Nakpuci automated purchasing.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_wholesale_risk') THEN
        ALTER TABLE wholesale_sources ADD CONSTRAINT chk_wholesale_risk
            CHECK (risk_level IN ('low', 'medium', 'high'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_wholesale_type') THEN
        ALTER TABLE wholesale_sources ADD CONSTRAINT chk_wholesale_type
            CHECK (type IN ('b2b_platform', 'surplus', 'gray_market'));
    END IF;
END $$;


-- ============================================================================
-- SEED DATA: brand_groups
-- ============================================================================

INSERT INTO brand_groups (name, name_short, hq_country, type, revenue_eur, website, stock_ticker, distribution_model) VALUES
    ('L''Oréal Group',              'loreal',       'FR', 'conglomerate',  41000000000, 'https://www.loreal.com',      'OR.PA',   'direct_subsidiary'),
    ('Estée Lauder Companies',      'estee_lauder', 'US', 'conglomerate',  14000000000, 'https://www.elcompanies.com', 'EL',      'direct_subsidiary'),
    ('LVMH Parfums & Cosmetics',    'lvmh',         'FR', 'conglomerate',   8000000000, 'https://www.lvmh.com',        'MC.PA',   'direct_subsidiary'),
    ('Coty Inc.',                    'coty',         'US', 'conglomerate',   5100000000, 'https://www.coty.com',        'COTY',    'direct_subsidiary'),
    ('Puig',                         'puig',         'ES', 'conglomerate',   5000000000, 'https://www.puig.com',        'PUIG.MC', 'direct_subsidiary'),
    ('Procter & Gamble Beauty',      'pg_beauty',    'US', 'conglomerate',  15000000000, 'https://www.pg.com',          'PG',      'direct_subsidiary'),
    ('Unilever Beauty & Wellbeing',  'unilever',     'GB', 'conglomerate',  13000000000, 'https://www.unilever.com',    'ULVR.L',  'direct_subsidiary'),
    ('Henkel Beauty Care',           'henkel',       'DE', 'conglomerate',   3500000000, 'https://www.henkel.com',      'HEN3.DE', 'direct_subsidiary'),
    ('Beiersdorf',                   'beiersdorf',   'DE', 'conglomerate',  10000000000, 'https://www.beiersdorf.com',  'BEI.DE',  'direct_subsidiary'),
    ('Shiseido Group',               'shiseido',     'JP', 'conglomerate',   7500000000, 'https://www.shiseido.com',    '4911.T',  'direct_subsidiary'),
    ('Inter Parfums',                'interparfums', 'FR', 'license_house',  1400000000, 'https://www.interparfums.com','IPAR',    'hybrid'),
    ('Euroitalia',                   'euroitalia',   'IT', 'license_house',   500000000, NULL,                          NULL,      'licensed_distributor'),
    ('Revlon / Elizabeth Arden',     'revlon',       'US', 'conglomerate',   1800000000, 'https://www.revlon.com',      NULL,      'hybrid'),
    ('Amore Pacific',                'amorepacific', 'KR', 'conglomerate',   3000000000, 'https://www.apgroup.com',     '090430.KS','licensed_distributor'),
    ('Wella Company',                'wella',        'DE', 'independent',    2500000000, 'https://www.wellacompany.com',NULL,      'hybrid')
ON CONFLICT (name) DO NOTHING;


-- ============================================================================
-- SEED DATA: brand_group_subsidiaries (CZ, PL, DE confirmed)
-- ============================================================================

INSERT INTO brand_group_subsidiaries (brand_group_id, country_iso2, subsidiary_name, ico, distribution_type) VALUES
    -- L'Oréal
    ((SELECT id FROM brand_groups WHERE name_short = 'loreal'),     'CZ', 'L''Oréal Česká republika s.r.o.',   '60491850',  'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'loreal'),     'PL', 'L''Oréal Polska Sp. z o.o.',        NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'loreal'),     'DE', 'L''Oréal Deutschland GmbH',         NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'loreal'),     'SK', 'L''Oréal Slovensko s.r.o.',         NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'loreal'),     'FR', 'L''Oréal SA (HQ)',                  NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'loreal'),     'AT', 'L''Oréal Österreich GmbH',          NULL,        'direct'),
    -- Estée Lauder
    ((SELECT id FROM brand_groups WHERE name_short = 'estee_lauder'),'CZ','Estée Lauder CZ s.r.o.',            '41188349',  'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'estee_lauder'),'PL','Estée Lauder Poland Sp. z o.o.',    NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'estee_lauder'),'DE','Estée Lauder GmbH',                 NULL,        'direct'),
    -- Coty
    ((SELECT id FROM brand_groups WHERE name_short = 'coty'),       'CZ', 'Coty Česká republika s.r.o.',       '25794701',  'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'coty'),       'PL', 'Coty Polska Sp. z o.o.',            NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'coty'),       'DE', 'Coty Beauty Germany GmbH',          NULL,        'direct'),
    -- Beiersdorf
    ((SELECT id FROM brand_groups WHERE name_short = 'beiersdorf'), 'CZ', 'Beiersdorf CZ s.r.o.',              NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'beiersdorf'), 'DE', 'Beiersdorf AG (HQ)',                NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'beiersdorf'), 'PL', 'Beiersdorf Manufacturing Poznan',   NULL,        'direct'),
    -- Henkel
    ((SELECT id FROM brand_groups WHERE name_short = 'henkel'),     'CZ', 'Henkel ČR s.r.o.',                  NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'henkel'),     'DE', 'Henkel AG & Co. KGaA (HQ)',         NULL,        'direct'),
    -- LVMH
    ((SELECT id FROM brand_groups WHERE name_short = 'lvmh'),       'CZ', 'Sephora CZ s.r.o.',                 NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'lvmh'),       'FR', 'LVMH Parfums & Cosmétiques (HQ)',   NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'lvmh'),       'DE', 'Parfums Christian Dior GmbH',       NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'lvmh'),       'PL', 'Sephora Polska Sp. z o.o.',         NULL,        'direct'),
    -- Shiseido
    ((SELECT id FROM brand_groups WHERE name_short = 'shiseido'),   'FR', 'Shiseido EMEA (HQ Neuilly-sur-Seine)',NULL,      'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'shiseido'),   'DE', 'Shiseido Germany GmbH',             NULL,        'direct'),
    -- Puig
    ((SELECT id FROM brand_groups WHERE name_short = 'puig'),       'ES', 'Puig SL (HQ Barcelona)',            NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'puig'),       'PL', 'Puig Polska Sp. z o.o.',            NULL,        'direct'),
    ((SELECT id FROM brand_groups WHERE name_short = 'puig'),       'DE', 'Puig Deutschland GmbH',             NULL,        'direct')
ON CONFLICT (brand_group_id, country_iso2) DO NOTHING;


-- ============================================================================
-- SEED DATA: distributors
-- ============================================================================

INSERT INTO distributors (name, name_short, type, hq_country, website, contact_email, contact_phone, is_authorized, notes) VALUES
    -- CZ/SK national distributors
    ('Glamour Distribuce a.s.',      'glamour',          'national',       'CZ', 'https://www.gdist.cz',               'info@gdist.cz',                    '541227975',       true,  'IČO 27753174. 50-99 zaměstnanců. NUXE exclusive CZ/SK. Fúze 2025 s CWJ24. Brno.'),
    ('Luxury Distribution s.r.o.',   'luxury_dist',      'national',       'CZ', 'https://luxurydistribution.cz',      'sylvie@luxurydistribution.cz',     '+420777228995',   true,  'IČO 07098880. Founded 2018. Manželé Malířovi. B2C: AURIO.cz. Sklad: Olicon Jeseník.'),
    -- PL wholesalers
    ('Gabona Sp. z o.o.',            'gabona',           'wholesale',      'PL', 'https://www.gabona.com',             NULL,                                NULL,              true,  'NIP 5213916903. 8K SKU. B2B portal b2b.gabona.co. MOV €2000. Professional hair focus.'),
    ('Beauty Sales House Sp. z o.o.','beauty_sales',     'wholesale',      'PL', 'https://www.beautysaleshouse.com',   'biuro@beautysaleshouse.com',       NULL,              NULL,  'NIP 679-32-07-198. 60K+ SKU. Luxury + niche fragrances. Auth status uncertain.'),
    -- DE wholesalers
    ('Kosmetik4less',                'kosmetik4less',    'wholesale',      'DE', 'https://www.kosmetik4less.de',       NULL,                                NULL,              false, 'Hamburg. ~20K SKU. Surplus/overstock discount model. Gray market signals.'),
    -- B2B platforms
    ('Qogita B.V.',                  'qogita',           'b2b_platform',   'NL', 'https://www.qogita.com',             NULL,                                NULL,              NULL,  'Amsterdam/London. €119M funding (Dawn, Accel, Bessemer). 500K+ products. MOV €300. Has API.'),
    ('Ankorstore',                   'ankorstore',       'b2b_platform',   'FR', 'https://www.ankorstore.com',         NULL,                                NULL,              true,  'Paris. 30K+ indie brands. MOV €100. Shopify integration. Authorized indie supply.'),
    ('Faire',                        'faire',            'b2b_platform',   'US', 'https://www.faire.com',              NULL,                                NULL,              true,  'US/EU. 100K+ brands. Net 60 terms. Lifestyle focus, not beauty-specific.'),
    ('Beautetrade',                  'beautetrade',      'b2b_platform',   NULL, 'https://www.beautetrade.com',        NULL,                                NULL,              NULL,  'Alibaba-style B2B directory. Primarily Asian exporters. Low relevance for branded EU beauty.'),
    -- Brand subsidiaries as distributors (they ARE direct distributors in CZ)
    ('L''Oréal CZ s.r.o.',          'loreal_cz',        'brand_subsidiary','CZ', 'https://www.loreal.cz',             'info@loreal.cz',                   '+420222771111',   true,  'IČO 60491850. ZK 106.8M CZK. All 4 L''Oréal divisions. Plzeňská, Praha 5.'),
    ('Coty Česká republika s.r.o.',  'coty_cz',          'brand_subsidiary','CZ', 'https://www.coty.cz',              NULL,                                '233020111',       true,  'IČO 25794701. Pod dráhou, Praha-Holešovice (od 1/2026). Jednatel: Paweł Chrościcki (Varšava).'),
    ('Estée Lauder CZ s.r.o.',       'elc_cz',           'brand_subsidiary','CZ', NULL,                               NULL,                                NULL,              true,  'IČO 41188349. Václavské nám. 47, Praha. Strictly selective distribution.'),
    ('Beiersdorf CZ s.r.o.',         'beiersdorf_cz',    'brand_subsidiary','CZ', NULL,                               NULL,                                NULL,              true,  'NIVEA, Eucerin, La Prairie. Mass + pharmacy channel.')
ON CONFLICT DO NOTHING;


-- ============================================================================
-- SEED DATA: distributor_countries
-- ============================================================================

-- Glamour Distribuce
INSERT INTO distributor_countries (distributor_id, country_iso2, is_exclusive) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'glamour'), 'CZ', false),
    ((SELECT id FROM distributors WHERE name_short = 'glamour'), 'SK', false)
ON CONFLICT DO NOTHING;

-- Luxury Distribution (CEE5)
INSERT INTO distributor_countries (distributor_id, country_iso2, is_exclusive) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'CZ', false),
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'SK', false),
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'HU', false),
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'RO', false),
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'PL', false)
ON CONFLICT DO NOTHING;

-- Gabona (PL + global export)
INSERT INTO distributor_countries (distributor_id, country_iso2, is_exclusive) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'PL', false),
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'DE', false),
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'CZ', false)
ON CONFLICT DO NOTHING;

-- Beauty Sales House (PL + EU)
INSERT INTO distributor_countries (distributor_id, country_iso2, is_exclusive) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'beauty_sales'), 'PL', false)
ON CONFLICT DO NOTHING;

-- Kosmetik4less (DE)
INSERT INTO distributor_countries (distributor_id, country_iso2, is_exclusive) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'kosmetik4less'), 'DE', false)
ON CONFLICT DO NOTHING;

-- Qogita (pan-EU)
INSERT INTO distributor_countries (distributor_id, country_iso2, is_exclusive) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'NL', false),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'DE', false),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'FR', false),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'CZ', false),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'PL', false),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'AT', false),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'ES', false),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'IT', false)
ON CONFLICT DO NOTHING;

-- Brand subsidiaries (CZ only for these entries)
INSERT INTO distributor_countries (distributor_id, country_iso2, is_exclusive) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'loreal_cz'), 'CZ', false),
    ((SELECT id FROM distributors WHERE name_short = 'loreal_cz'), 'SK', false),
    ((SELECT id FROM distributors WHERE name_short = 'coty_cz'), 'CZ', false),
    ((SELECT id FROM distributors WHERE name_short = 'coty_cz'), 'SK', false),
    ((SELECT id FROM distributors WHERE name_short = 'elc_cz'), 'CZ', false),
    ((SELECT id FROM distributors WHERE name_short = 'beiersdorf_cz'), 'CZ', false),
    ((SELECT id FROM distributors WHERE name_short = 'beiersdorf_cz'), 'SK', false)
ON CONFLICT DO NOTHING;


-- ============================================================================
-- SEED DATA: wholesale_sources
-- ============================================================================

INSERT INTO wholesale_sources (name, website, type, hq_country, min_order_eur, product_count, has_api, risk_level, notes) VALUES
    ('Qogita',             'https://www.qogita.com',            'b2b_platform', 'NL', 300,   500000, true,  'low',    '€119M funded. 500+ suppliers. Vetted surplus. Amazon-invoice compatible. #1 Nakpuci priority.'),
    ('Gabona B2B',         'https://b2b.gabona.co',             'b2b_platform', 'PL', 2000,  8000,   false, 'medium', 'B2B portal (8 jazyků). Professional hair focus. Claims "only authentic products".'),
    ('Beauty Sales House', 'https://www.beautysaleshouse.com',  'b2b_platform', 'PL', NULL,  60000,  false, 'medium', '60K+ SKU. Luxury + niche fragrances (Creed, Amouage). Authorization unverified.'),
    ('Ankorstore',         'https://www.ankorstore.com',        'b2b_platform', 'FR', 100,   NULL,   true,  'low',    '30K+ indie brands. Shopify API. Authorized indie supply.'),
    ('Faire',              'https://www.faire.com',             'b2b_platform', 'US', 0,     NULL,   true,  'low',    '100K+ brands. Net 60 terms. Lifestyle > beauty.'),
    ('Beautetrade',        'https://www.beautetrade.com',       'b2b_platform', NULL, NULL,  NULL,   false, 'high',   'Alibaba-style directory. Asian exporters. For private label only.')
ON CONFLICT DO NOTHING;


COMMIT;

-- ============================================================================
-- VERIFICATION
-- ============================================================================
-- SELECT name, type, hq_country FROM brand_groups ORDER BY revenue_eur DESC NULLS LAST;
-- SELECT name, type, hq_country FROM distributors ORDER BY type, name;
-- SELECT d.name, array_agg(dc.country_iso2) as countries FROM distributors d JOIN distributor_countries dc ON d.id = dc.distributor_id GROUP BY d.name;
-- SELECT name, min_order_eur, has_api, risk_level FROM wholesale_sources ORDER BY risk_level;
