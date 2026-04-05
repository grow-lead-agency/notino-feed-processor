-- ============================================================================
-- 005_supply_chain_seed_data.sql
-- Supply Chain Layer: Operational seed data
-- ============================================================================
--
-- Prereq: 003b_supply_chain_base.sql (base tables + distributor rows)
-- Prereq: 004_supply_chain_operational.sql (operational columns + child tables)
--
-- Seeds: operational columns on distributors, contacts, warehouses, carriers,
--        known customers, brand auth status, notes
--
-- Run: psql "$DATABASE_URL" -f sql/005_supply_chain_seed_data.sql
-- ============================================================================

BEGIN;

-- ============================================================================
-- STEP 1: UPDATE distributors — operational columns
-- ============================================================================

-- Glamour Distribuce a.s.
UPDATE distributors SET
    legal_name              = 'Glamour Distribuce, a.s.',
    ico                     = '27753174',
    vat_id                  = 'CZ27753174',
    founded_year            = 2007,
    account_status          = 'prospect',
    payment_terms_days      = NULL,  -- unknown, to be confirmed
    requires_prepayment     = false,
    bnpl_available          = false,
    min_order_eur           = NULL,  -- unknown, to be confirmed
    returns_policy          = NULL,
    lead_time_days_min      = NULL,
    lead_time_days_max      = NULL,
    shipping_model          = NULL,
    inventory_visibility    = 'unknown',
    product_feed_format     = NULL,
    ean_coverage_pct        = NULL,
    price_update_frequency  = NULL,
    has_media_assets        = false,
    b2b_portal_url          = NULL,
    b2b_credentials_stored  = false,
    avg_discount_vs_rrp_pct = NULL,
    delivery_reliability_score = NULL,
    gray_market_risk        = 'none',
    gray_market_risk_notes  = 'Authorized national distributor. NUXE exclusive CZ/SK. Inter Parfums + Euroitalia licensee.',
    automation_readiness    = 'email_only',
    checkout_flow_notes     = 'Traditional B2B — email/phone ordering. No known self-service portal.',
    has_captcha             = NULL,
    onboarding_requirements = 'Trade license, VAT registration, reference from existing beauty retail operation.',
    registered_at           = NULL
WHERE name_short = 'glamour';

-- Luxury Distribution s.r.o.
UPDATE distributors SET
    legal_name              = 'Luxury distribution s.r.o.',
    ico                     = '07098880',
    vat_id                  = 'CZ07098880',
    founded_year            = 2018,
    account_status          = 'prospect',
    payment_terms_days      = NULL,
    requires_prepayment     = false,
    bnpl_available          = false,
    min_order_eur           = NULL,
    returns_policy          = NULL,
    lead_time_days_min      = NULL,
    lead_time_days_max      = NULL,
    shipping_model          = NULL,
    inventory_visibility    = 'unknown',
    product_feed_format     = NULL,
    ean_coverage_pct        = NULL,
    price_update_frequency  = NULL,
    has_media_assets        = true,
    b2b_portal_url          = NULL,
    b2b_credentials_stored  = false,
    avg_discount_vs_rrp_pct = NULL,
    delivery_reliability_score = NULL,
    gray_market_risk        = 'none',
    gray_market_risk_notes  = 'Authorized boutique distributor. 3900+ doors. Direct contracts with niche brands.',
    automation_readiness    = 'email_only',
    checkout_flow_notes     = 'Email/WhatsApp based ordering (sylvie@luxurydistribution.cz, +420 777 228 995).',
    has_captcha             = NULL,
    onboarding_requirements = 'Trade license, CZ retail presence or e-shop, brand references.',
    registered_at           = NULL
WHERE name_short = 'luxury_dist';

-- Gabona Sp. z o.o.
UPDATE distributors SET
    legal_name              = 'Gabona sp. z o.o.',
    ico                     = '5213916903',  -- NIP
    vat_id                  = 'PL5213916903',
    founded_year            = 2015,
    account_status          = 'prospect',
    payment_terms_days      = 0,  -- prepayment required
    requires_prepayment     = true,
    bnpl_available          = false,
    min_order_eur           = 2000,
    returns_policy          = NULL,
    lead_time_days_min      = 2,
    lead_time_days_max      = 5,
    shipping_model          = 'DDP',
    inventory_visibility    = 'b2b_portal',
    product_feed_format     = NULL,
    ean_coverage_pct        = NULL,
    price_update_frequency  = 'daily',
    has_media_assets        = true,
    b2b_portal_url          = 'https://b2b.gabona.co',
    b2b_credentials_stored  = false,
    avg_discount_vs_rrp_pct = NULL,
    delivery_reliability_score = NULL,
    gray_market_risk        = 'medium',
    gray_market_risk_notes  = 'Claims "only authentic products". Large portfolio (62 brands, 8K SKU) suggests mix of authorized + surplus. Unverified.',
    automation_readiness    = 'b2b_portal',
    checkout_flow_notes     = 'Self-service B2B portal (b2b.gabona.co). Registration → approval (24h) → browse catalog → cart → bank transfer. Live stock levels. 8 languages.',
    has_captcha             = false,
    onboarding_requirements = 'Business registration (KRS/NIP or equivalent). First order flexible on MOV. Approval within 24h.',
    registered_at           = NULL
WHERE name_short = 'gabona';

-- Beauty Sales House
UPDATE distributors SET
    legal_name              = 'Beauty Sales House sp. z o.o.',
    ico                     = '679-32-07-198',  -- NIP
    vat_id                  = NULL,
    founded_year            = 2012,
    account_status          = 'prospect',
    payment_terms_days      = NULL,
    requires_prepayment     = NULL,
    bnpl_available          = false,
    min_order_eur           = NULL,
    returns_policy          = NULL,
    lead_time_days_min      = NULL,
    lead_time_days_max      = NULL,
    shipping_model          = NULL,
    inventory_visibility    = 'email_only',
    product_feed_format     = NULL,
    ean_coverage_pct        = NULL,
    price_update_frequency  = NULL,
    has_media_assets        = false,
    b2b_portal_url          = NULL,
    b2b_credentials_stored  = false,
    avg_discount_vs_rrp_pct = NULL,
    delivery_reliability_score = NULL,
    gray_market_risk        = 'high',
    gray_market_risk_notes  = '60K+ SKU includes ultra-luxury niche (Creed, Amouage, Kilian, Xerjoff, Byredo) with very strict selective distribution. High probability of parallel import/surplus.',
    automation_readiness    = 'email_only',
    checkout_flow_notes     = 'Email only (biuro@beautysaleshouse.com). No B2B portal. No self-service.',
    has_captcha             = NULL,
    onboarding_requirements = 'Unknown — email inquiry required.',
    registered_at           = NULL
WHERE name_short = 'beauty_sales';

-- Kosmetik4less
UPDATE distributors SET
    legal_name              = 'Kosmetik4less GmbH',
    ico                     = NULL,
    vat_id                  = NULL,
    founded_year            = NULL,
    account_status          = 'prospect',
    payment_terms_days      = 0,
    requires_prepayment     = true,
    bnpl_available          = false,
    min_order_eur           = NULL,
    returns_policy          = NULL,
    lead_time_days_min      = 1,
    lead_time_days_max      = 3,
    shipping_model          = NULL,
    inventory_visibility    = 'unknown',
    product_feed_format     = NULL,
    ean_coverage_pct        = NULL,
    price_update_frequency  = NULL,
    has_media_assets        = false,
    b2b_portal_url          = 'https://www.kosmetik4less.de',
    b2b_credentials_stored  = false,
    avg_discount_vs_rrp_pct = 30.0,
    delivery_reliability_score = NULL,
    gray_market_risk        = 'high',
    gray_market_risk_notes  = 'Discount model — surplus/overstock from large distributors. No brand authorization visible. Primarily B2C.',
    automation_readiness    = 'unknown',
    checkout_flow_notes     = 'Primarily B2C e-shop. Limited B2B capability.',
    has_captcha             = NULL,
    onboarding_requirements = NULL,
    registered_at           = NULL
WHERE name_short = 'kosmetik4less';

-- Qogita B.V.
UPDATE distributors SET
    legal_name              = 'Qogita B.V.',
    ico                     = NULL,
    vat_id                  = NULL,  -- NL VAT TBD
    founded_year            = 2021,
    account_status          = 'prospect',
    payment_terms_days      = 0,  -- card/transfer at checkout, BNPL optional
    requires_prepayment     = true,
    bnpl_available          = true,
    min_order_eur           = 300,
    returns_policy          = 'Claim resolution within 24 hours.',
    lead_time_days_min      = 1,
    lead_time_days_max      = 3,
    shipping_model          = 'DDP',
    inventory_visibility    = 'api',
    product_feed_format     = 'api',
    ean_coverage_pct        = 90,  -- estimated, most products have EAN
    price_update_frequency  = 'realtime',
    has_media_assets        = true,
    api_docs_url            = NULL,  -- requires registration to access
    b2b_portal_url          = 'https://www.qogita.com',
    b2b_credentials_stored  = false,
    avg_discount_vs_rrp_pct = 25.0,  -- estimated average wholesale discount
    delivery_reliability_score = NULL,
    gray_market_risk        = 'low',
    gray_market_risk_notes  = '€119M VC-funded (Dawn, Accel, Bessemer). Vetted 500+ suppliers. "Guaranteed authenticity". Amazon-invoice compatible. Accel partner called EU wholesale "notoriously grey market" — Qogita digitizes it with quality controls.',
    automation_readiness    = 'api',
    checkout_flow_notes     = 'Web cart, spreadsheet upload, or API. Cart → payment (card/transfer/BNPL) → Qogita splits across optimal suppliers → ships from EU/UK warehouses. No CAPTCHA.',
    has_captcha             = false,
    onboarding_requirements = 'Free registration. Business email. No trade license required for initial access. Full onboarding for API access.',
    registered_at           = NULL
WHERE name_short = 'qogita';

-- Ankorstore
UPDATE distributors SET
    legal_name              = 'Ankorstore SAS',
    ico                     = NULL,
    vat_id                  = NULL,
    founded_year            = 2019,
    account_status          = 'prospect',
    payment_terms_days      = 60,  -- Net 60 for brands
    requires_prepayment     = false,
    bnpl_available          = true,
    min_order_eur           = 100,
    returns_policy          = NULL,
    lead_time_days_min      = 2,
    lead_time_days_max      = 7,
    shipping_model          = NULL,
    inventory_visibility    = 'b2b_portal',
    product_feed_format     = 'api',
    ean_coverage_pct        = NULL,
    price_update_frequency  = 'daily',
    has_media_assets        = true,
    b2b_portal_url          = 'https://www.ankorstore.com',
    b2b_credentials_stored  = false,
    avg_discount_vs_rrp_pct = NULL,
    delivery_reliability_score = NULL,
    gray_market_risk        = 'none',
    gray_market_risk_notes  = 'Brands sell directly on platform. Authorized indie supply. Shopify integration.',
    automation_readiness    = 'api',
    checkout_flow_notes     = 'Self-service marketplace. Shopify API integration available.',
    has_captcha             = false,
    onboarding_requirements = 'Free registration. Business presence.',
    registered_at           = NULL
WHERE name_short = 'ankorstore';

-- Brand subsidiaries — minimal operational data (they're suppliers, not traditional distributors)
UPDATE distributors SET
    legal_name = 'L''Oréal Česká republika s.r.o.',
    ico = '60491850', vat_id = 'CZ60491850', founded_year = 1994,
    account_status = 'prospect',
    gray_market_risk = 'none', gray_market_risk_notes = 'Direct brand subsidiary.',
    automation_readiness = 'email_only',
    onboarding_requirements = 'Selective distribution contract. Physical retail or authorized e-commerce presence. Trained staff (for prestige lines).'
WHERE name_short = 'loreal_cz';

UPDATE distributors SET
    legal_name = 'Coty Česká republika s.r.o.',
    ico = '25794701', vat_id = 'CZ25794701', founded_year = 1999,
    account_status = 'prospect',
    gray_market_risk = 'none', gray_market_risk_notes = 'Direct brand subsidiary. Managed from Warsaw (Paweł Chrościcki).',
    automation_readiness = 'email_only',
    onboarding_requirements = 'Selective distribution agreement. Coty qualification criteria (store standards, beauty advisors, no marketplace sales).'
WHERE name_short = 'coty_cz';

UPDATE distributors SET
    legal_name = 'Estée Lauder CZ s.r.o.',
    ico = '41188349', vat_id = 'CZ41188349', founded_year = NULL,
    account_status = 'prospect',
    gray_market_risk = 'none', gray_market_risk_notes = 'Direct brand subsidiary. Strictly selective.',
    automation_readiness = 'email_only',
    onboarding_requirements = 'Strictly selective distribution. Department store or premium beauty retailer presence required.'
WHERE name_short = 'elc_cz';

UPDATE distributors SET
    legal_name = 'Beiersdorf CZ s.r.o.',
    founded_year = NULL, account_status = 'prospect',
    gray_market_risk = 'none', gray_market_risk_notes = 'Direct brand subsidiary.',
    automation_readiness = 'email_only'
WHERE name_short = 'beiersdorf_cz';


-- ============================================================================
-- STEP 2: distributor_contacts
-- ============================================================================

INSERT INTO distributor_contacts (distributor_id, full_name, role, email, phone, linkedin_url, is_primary, notes) VALUES
    -- Glamour Distribuce
    ((SELECT id FROM distributors WHERE name_short = 'glamour'), 'Jiří Malinka, DiS',     'executive',       'info@gdist.cz',                    '605209211',       NULL, true,  'Předseda představenstva. Od 2018.'),
    ((SELECT id FROM distributors WHERE name_short = 'glamour'), 'Ing. Antonín Keller',   'executive',       NULL,                                NULL,              NULL, false, 'Člen představenstva. Od 2018.'),
    -- Luxury Distribution
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'Sylvie Malířová',   'executive',       'sylvie@luxurydistribution.cz',     '+420777228995',   NULL, true,  'Jednatel, co-founder, 50% podíl. Primary face of company.'),
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'Tomáš Malíř',       'executive',       NULL,                                NULL,              NULL, false, 'Jednatel, co-founder, 50% podíl.'),
    -- Gabona — no named contacts, general only
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'B2B Sales Team',         'sales_rep',       NULL,                                NULL,              NULL, true,  'General B2B contact via b2b.gabona.co registration.'),
    -- Qogita
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'Manolis Manassakis',     'executive',       NULL,                                NULL,              NULL, false, 'CEO (od 2023). Dříve EMEA Operations Director u Uber.'),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'Danny Toledano',         'executive',       NULL,                                NULL,              NULL, false, 'Board Member / Founder. Dříve president Isramco.'),
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'Yaniv Toledano',         'executive',       NULL,                                NULL,              NULL, false, 'Board Member / Founder. Dříve Goldman Sachs.'),
    -- Coty CZ
    ((SELECT id FROM distributors WHERE name_short = 'coty_cz'), 'Paweł Chrościcki',     'executive',       NULL,                                NULL,              NULL, true,  'Jednatel. Sídlo Varšava — řídí CZ operace z PL regionálně. Od 27.6.2018.')
ON CONFLICT DO NOTHING;


-- ============================================================================
-- STEP 3: distributor_warehouses
-- ============================================================================

INSERT INTO distributor_warehouses (distributor_id, name, address, city, country_iso2, postal_code, is_primary, notes) VALUES
    -- Glamour Distribuce
    ((SELECT id FROM distributors WHERE name_short = 'glamour'), 'HQ + sklad Brno', 'Jandáskova 1957/24', 'Brno - Řečkovice', 'CZ', '62100', true, 'Business park. Konsolidován po fúzi 2025 s CWJ24.'),
    -- Luxury Distribution
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'Sklad Jeseník (Olicon)', 'Janáčkova 760', 'Jeseník', 'CZ', '79001', true, 'Outsourced na Olicon logistics s.r.o. Severní Morava.'),
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'Kancelář Praha', 'V Parku 2308/8', 'Praha 11 - Chodov', 'CZ', '14800', false, 'The Park business complex. Office only, ne sklad.'),
    -- Gabona
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'HQ + sklad Varšava', 'ul. Świeradowska 47', 'Warszawa', 'PL', '02662', true, NULL),
    -- Qogita — multi-warehouse EU/UK (no specific addresses published)
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'EU warehouse network', NULL, NULL, 'NL', NULL, true, 'Ships from EU/UK warehouses. Coordinates across 500+ suppliers. No single warehouse address.'),
    -- L'Oréal CZ
    ((SELECT id FROM distributors WHERE name_short = 'loreal_cz'), 'HQ Praha 5', 'Plzeňská 213/11', 'Praha 5 - Smíchov', 'CZ', '15000', true, NULL),
    -- Coty CZ
    ((SELECT id FROM distributors WHERE name_short = 'coty_cz'), 'Office Praha 7', 'Pod dráhou 1637/2', 'Praha - Holešovice', 'CZ', '17000', true, 'Přestěhováno z Veleslavína 1/2026.')
ON CONFLICT DO NOTHING;


-- ============================================================================
-- STEP 4: distributor_carriers (Gabona confirmed)
-- ============================================================================

INSERT INTO distributor_carriers (distributor_id, carrier_name, service_type, max_weight_kg, notes) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'UPS',         'parcel',    30,   'Default parcel carrier.'),
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'SUUS',        'pallet',    NULL, 'Pallet/freight.'),
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'DACHSER',     'pallet',    NULL, 'Pallet/freight.'),
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'RABEN',       'pallet',    NULL, 'Pallet/freight.'),
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'MAINFREIGHT', 'container', NULL, 'Container/sea freight.'),
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'ASL',         'container', NULL, 'Container/sea freight.')
ON CONFLICT DO NOTHING;


-- ============================================================================
-- STEP 5: distributor_known_customers (who buys from whom)
-- ============================================================================
-- Links distributors to competitors (retailers) they supply.
-- Only inserting where we have high-confidence data.

-- Glamour Distribuce supplies these CZ retail chains:
INSERT INTO distributor_known_customers (distributor_id, competitor_id, confidence, source) VALUES
    ((SELECT id FROM distributors WHERE name_short = 'glamour'),
     (SELECT id FROM competitors WHERE url ILIKE '%dm.cz%' OR name ILIKE '%dm drogerie%' LIMIT 1),
     'confirmed', 'Academic research ČZU 2015 — dm = #1 customer of Glamour/Prestige Products')
ON CONFLICT DO NOTHING;

-- Luxury Distribution supplies:
-- (retail partners from their website: Alza, Dr. Max, Pilulka, Lékárna.cz, Douglas, FAnn, Marionnaud, Sephora)
-- Only insert if competitor exists in our DB
DO $$
DECLARE
    ld_id INTEGER := (SELECT id FROM distributors WHERE name_short = 'luxury_dist');
    comp_id INTEGER;
BEGIN
    -- Try each known customer
    FOR comp_id IN
        SELECT id FROM competitors
        WHERE url ILIKE ANY(ARRAY['%alza.cz%', '%drmax.cz%', '%pilulka.cz%', '%lekarna.cz%', '%douglas.cz%', '%fann.cz%', '%marionnaud.cz%', '%sephora.cz%'])
    LOOP
        INSERT INTO distributor_known_customers (distributor_id, competitor_id, confidence, source)
        VALUES (ld_id, comp_id, 'confirmed', 'luxurydistribution.cz — retail partner logos on website')
        ON CONFLICT DO NOTHING;
    END LOOP;
END $$;


-- ============================================================================
-- STEP 6: distributor_notes (initial intel notes)
-- ============================================================================

INSERT INTO distributor_notes (distributor_id, note_type, subject, body, author) VALUES
    -- Glamour
    ((SELECT id FROM distributors WHERE name_short = 'glamour'), 'intel',
     'Fúze 2025 + Glamour Group',
     'Srpen 2025: Glamour Distribuce absorbovala CWJ24 a.s. fúzí sloučením. Součást širší "Glamour Group" (zmiňována v akademickém výzkumu ČZU 2015, zahrnovala i Prestige Products CZ). Konsolidace holdingové struktury. Historicky: Prestige Products měla 54 dámských + 31 pánských brandů, #1 zákazník = dm.',
     'AI research agent'),
    -- Luxury Distribution
    ((SELECT id FROM distributors WHERE name_short = 'luxury_dist'), 'intel',
     'Founder-led boutique distributor',
     'Založeno 2018 manželi Malířovými (Sylvie + Tomáš) s 1000 CZK ZK. Za 7 let narostli z nuly na 3900+ doors a 20 brandů. Lean founder-operated model. Sklad outsourcován na Olicon Jeseník. Vlastní B2C e-shop AURIO.cz. Primary kontakt: Sylvie Malířová.',
     'AI research agent'),
    -- Qogita
    ((SELECT id FROM distributors WHERE name_short = 'qogita'), 'intel',
     'Qogita — #1 Nakpuci integration priority',
     '€119M VC-funded B2B marketplace (Dawn Capital, Accel, Bessemer, LocalGlobe). 500K+ products, 500+ suppliers, 100K+ retailers. MOV €300 (nejnižší na trhu). HAS API for automated purchasing. BNPL available. Amazon-invoice compatible. CEO: Manolis Manassakis (ex-Uber). Founders: Danny + Yaniv Toledano (ex-Goldman Sachs). "Guaranteed authenticity" — vetted suppliers, digitized gray market.',
     'AI research agent'),
    -- Gabona
    ((SELECT id FROM distributors WHERE name_short = 'gabona'), 'intel',
     'Gabona — B2B portal available',
     'B2B self-service portal: b2b.gabona.co (8 jazyků). Registrace → schválení do 24h → browse + order. Live stock, cenové tiery. 62 potvrzených brandů, silný professional hair segment (Kérastase, Wella, L''Oréal Pro, Olaplex, K18). MOV €2000. Dopravci: UPS (parcels), SUUS/DACHSER/RABEN (palety), MAINFREIGHT/ASL (kontejnery). CCTV dokumentace balení.',
     'AI research agent'),
    -- Beauty Sales House
    ((SELECT id FROM distributors WHERE name_short = 'beauty_sales'), 'intel',
     'Beauty Sales House — niche fragrance focus, high gray market risk',
     '60K+ SKU. Portfolio zahrnuje ultra-luxury niche: Creed, Amouage, Kilian, Xerjoff, Byredo, MFK, Parfums de Marly, Nasomatto. Tyto brandy mají velmi přísnou selective distribution → vysoká pravděpodobnost parallel import/surplus. Bez B2B portálu — email only. Kraków.',
     'AI research agent'),
    -- Coty CZ
    ((SELECT id FROM distributors WHERE name_short = 'coty_cz'), 'intel',
     'Coty CZ — regional management from Warsaw',
     'Jednatel Paweł Chrościcki sídlí ve Varšavě — Coty řídí CZ operace regionálně z PL. Adresa přestěhována 1/2026 z Veleslavín na Holešovice (Pod dráhou). ZK 5M CZK. Owns: Coty B.V. 99% + Lancaster B.V. 1% (Amsterdam → Coty Inc. NYSE: COTY).',
     'AI research agent')
ON CONFLICT DO NOTHING;


COMMIT;

-- ============================================================================
-- VERIFICATION
-- ============================================================================
-- SELECT d.name, d.account_status, d.min_order_eur, d.automation_readiness, d.gray_market_risk
-- FROM distributors d ORDER BY d.name;
--
-- SELECT d.name, dc.full_name, dc.role, dc.is_primary
-- FROM distributors d JOIN distributor_contacts dc ON d.id = dc.distributor_id
-- ORDER BY d.name, dc.is_primary DESC;
--
-- SELECT d.name, dw.city, dw.country_iso2, dw.is_primary
-- FROM distributors d JOIN distributor_warehouses dw ON d.id = dw.distributor_id
-- ORDER BY d.name;
--
-- SELECT d.name, dn.subject, dn.note_type, dn.created_at
-- FROM distributors d JOIN distributor_notes dn ON d.id = dn.distributor_id
-- ORDER BY d.name, dn.created_at;
