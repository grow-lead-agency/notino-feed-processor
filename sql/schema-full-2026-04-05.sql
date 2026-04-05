--
-- PostgreSQL database dump
--

\restrict s4Z6BaobwhHAQ6NOM6c9KxqIiVarcsdsTb3DBSdxkDTPgGrd6SGfzYDTbER2XTS

-- Dumped from database version 17.8 (a48d9ca)
-- Dumped by pg_dump version 18.3

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: unaccent; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;


--
-- Name: EXTENSION unaccent; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION unaccent IS 'text search dictionary that removes accents';


--
-- Name: availability_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.availability_status AS ENUM (
    'in_stock',
    'out_of_stock',
    'preorder',
    'backorder',
    'limited'
);


--
-- Name: feed_source; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.feed_source AS ENUM (
    'sitemap_scrape',
    'jsonld_scrape',
    'microdata_scrape',
    'affiliate_feed',
    'api',
    'manual',
    'firecrawl_jsonld'
);


--
-- Name: product_condition; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.product_condition AS ENUM (
    'new',
    'refurbished',
    'used'
);


--
-- Name: normalize_brand(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.normalize_brand(raw text) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT lower(regexp_replace(
        regexp_replace(
            unaccent(raw),
            '[^a-zA-Z0-9\s]', '', 'g'
        ),
        '\s+', ' ', 'g'
    ))
$$;


--
-- Name: normalize_value(text, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.normalize_value(p_entity_type text, p_raw_value text, p_language text DEFAULT NULL::text) RETURNS text
    LANGUAGE plpgsql STABLE
    AS $$
DECLARE
    result TEXT;
BEGIN
    -- 1. Try exact match with language
    SELECT canonical INTO result
    FROM normalization_rules
    WHERE entity_type = p_entity_type
      AND pattern_normalized = normalize_brand(p_raw_value)
      AND (language = p_language OR language IS NULL)
    ORDER BY priority DESC, language IS NULL ASC  -- prefer language-specific
    LIMIT 1;
    
    IF result IS NOT NULL THEN
        RETURN result;
    END IF;
    
    -- 2. Try fuzzy match (similarity > 0.8)
    SELECT canonical INTO result
    FROM normalization_rules
    WHERE entity_type = p_entity_type
      AND similarity(pattern_normalized, normalize_brand(p_raw_value)) > 0.8
      AND (language = p_language OR language IS NULL)
    ORDER BY similarity(pattern_normalized, normalize_brand(p_raw_value)) DESC
    LIMIT 1;
    
    RETURN result;  -- NULL if no match
END;
$$;


--
-- Name: price_in_eur(numeric, character, date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.price_in_eur(price numeric, currency character, rate_date date DEFAULT CURRENT_DATE) RETURNS numeric
    LANGUAGE sql STABLE
    AS $$
    SELECT CASE 
        WHEN currency = 'EUR' THEN price
        ELSE price / COALESCE(
            (SELECT rate FROM exchange_rates 
             WHERE target_currency = currency 
             AND exchange_rates.rate_date <= price_in_eur.rate_date 
             ORDER BY exchange_rates.rate_date DESC LIMIT 1),
            1.0
        )
    END
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: affiliate_networks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.affiliate_networks (
    id integer NOT NULL,
    name text NOT NULL,
    website text,
    api_available boolean DEFAULT false,
    feed_access text,
    markets text[],
    notes text
);


--
-- Name: affiliate_networks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.affiliate_networks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: affiliate_networks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.affiliate_networks_id_seq OWNED BY public.affiliate_networks.id;


--
-- Name: affiliate_programs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.affiliate_programs (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    has_program boolean DEFAULT false,
    network text,
    program_url text,
    feed_url text,
    feed_format text,
    feed_product_count integer,
    commission_rate text,
    commission_type text,
    cookie_duration_days integer,
    payment_terms text,
    min_payout numeric,
    min_payout_currency text,
    deeplink_supported boolean,
    notes text,
    discovered_at timestamp with time zone DEFAULT now(),
    verified_at timestamp with time zone
);


--
-- Name: affiliate_programs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.affiliate_programs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: affiliate_programs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.affiliate_programs_id_seq OWNED BY public.affiliate_programs.id;


--
-- Name: ai_sweep_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_sweep_config (
    id integer NOT NULL,
    sweep_type text NOT NULL,
    enabled boolean DEFAULT true,
    schedule text,
    model text DEFAULT 'gemini-flash'::text,
    max_items_per_run integer DEFAULT 100,
    confidence_threshold numeric(3,2) DEFAULT 0.85,
    last_run_at timestamp with time zone,
    last_run_items integer,
    last_run_resolved integer,
    notes text
);


--
-- Name: ai_sweep_config_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_sweep_config_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_sweep_config_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_sweep_config_id_seq OWNED BY public.ai_sweep_config.id;


--
-- Name: ai_sweep_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_sweep_log (
    id bigint NOT NULL,
    sweep_type text NOT NULL,
    model text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    items_processed integer,
    items_auto_resolved integer,
    items_queued_for_review integer,
    items_skipped integer,
    tokens_used integer,
    cost_estimate numeric(8,4),
    errors integer DEFAULT 0,
    error_details jsonb
);


--
-- Name: ai_sweep_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_sweep_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_sweep_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_sweep_log_id_seq OWNED BY public.ai_sweep_log.id;


--
-- Name: attribute_values; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attribute_values (
    id integer NOT NULL,
    attribute_id integer NOT NULL,
    code text NOT NULL,
    value_en text NOT NULL,
    value_cs text,
    sort_order integer DEFAULT 0
);


--
-- Name: attribute_values_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.attribute_values_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: attribute_values_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.attribute_values_id_seq OWNED BY public.attribute_values.id;


--
-- Name: attributes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attributes (
    id integer NOT NULL,
    code text NOT NULL,
    name_en text NOT NULL,
    name_cs text,
    attribute_type text NOT NULL,
    unit text,
    category_ids integer[],
    is_filterable boolean DEFAULT true,
    is_searchable boolean DEFAULT false,
    sort_order integer DEFAULT 0
);


--
-- Name: attributes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.attributes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: attributes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.attributes_id_seq OWNED BY public.attributes.id;


--
-- Name: banner_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.banner_snapshots (
    id bigint NOT NULL,
    competitor_id integer,
    country_id integer,
    campaign_id bigint,
    page_url text,
    page_type text,
    banner_image_url text,
    banner_position text,
    banner_copy text,
    banner_cta text,
    banner_link text,
    captured_at timestamp with time zone DEFAULT now()
);


--
-- Name: banner_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.banner_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: banner_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.banner_snapshots_id_seq OWNED BY public.banner_snapshots.id;


--
-- Name: brand_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_aliases (
    id integer NOT NULL,
    brand_id integer NOT NULL,
    alias text NOT NULL,
    alias_normalized text NOT NULL,
    source text,
    confidence numeric(3,2),
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: brand_aliases_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brand_aliases_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brand_aliases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brand_aliases_id_seq OWNED BY public.brand_aliases.id;


--
-- Name: brand_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_images (
    id integer NOT NULL,
    brand_id integer NOT NULL,
    image_id bigint NOT NULL,
    role text
);


--
-- Name: brand_images_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brand_images_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brand_images_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brand_images_id_seq OWNED BY public.brand_images.id;


--
-- Name: brand_protection_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_protection_alerts (
    id bigint NOT NULL,
    brand_id integer NOT NULL,
    product_id integer,
    competitor_id integer,
    country_id integer,
    alert_type text NOT NULL,
    severity text NOT NULL,
    detected_price numeric(12,2),
    expected_min_price numeric(12,2),
    violation_pct numeric(5,2),
    evidence jsonb,
    status text DEFAULT 'open'::text,
    brand_notified_at timestamp with time zone,
    resolved_at timestamp with time zone,
    resolution_notes text,
    detected_at timestamp with time zone DEFAULT now()
);


--
-- Name: brand_protection_alerts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brand_protection_alerts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brand_protection_alerts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brand_protection_alerts_id_seq OWNED BY public.brand_protection_alerts.id;


--
-- Name: brand_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_subscriptions (
    id integer NOT NULL,
    brand_id integer NOT NULL,
    subscriber_company text NOT NULL,
    subscriber_contact_email text,
    tier text NOT NULL,
    monthly_price numeric(10,2),
    currency text DEFAULT 'EUR'::text,
    products_tracked integer,
    countries_tracked integer,
    active boolean DEFAULT true,
    started_at date,
    renews_at date,
    notes text
);


--
-- Name: brand_subscriptions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brand_subscriptions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brand_subscriptions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brand_subscriptions_id_seq OWNED BY public.brand_subscriptions.id;


--
-- Name: brands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brands (
    id integer NOT NULL,
    name text NOT NULL,
    name_normalized text NOT NULL,
    parent_company text,
    category text,
    segment text,
    origin_country_id integer,
    website text,
    logo_url text,
    created_at timestamp with time zone DEFAULT now(),
    slug text,
    tagline text,
    description text,
    story text,
    brand_values text[],
    certifications text[],
    founded_year integer,
    hero_image_url text,
    protection_active boolean DEFAULT false,
    map_price_policy numeric(5,2),
    seo_title text,
    seo_description text,
    seo_keywords text[]
);


--
-- Name: brands_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brands_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brands_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brands_id_seq OWNED BY public.brands.id;


--
-- Name: category_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.category_mappings (
    id integer NOT NULL,
    competitor_id integer,
    shop_group_id integer,
    source_category text NOT NULL,
    source_category_normalized text,
    source_breadcrumb text[],
    target_category_id integer,
    mapping_type text,
    confidence numeric(3,2),
    verified boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: category_mappings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.category_mappings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: category_mappings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.category_mappings_id_seq OWNED BY public.category_mappings.id;


--
-- Name: category_tree_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.category_tree_snapshots (
    id integer NOT NULL,
    competitor_id integer,
    country_id integer,
    snapshot_at timestamp with time zone DEFAULT now(),
    tree_structure jsonb,
    total_categories integer,
    total_leaf_categories integer,
    max_depth integer
);


--
-- Name: category_tree_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.category_tree_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: category_tree_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.category_tree_snapshots_id_seq OWNED BY public.category_tree_snapshots.id;


--
-- Name: competitor_coverage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.competitor_coverage (
    id integer NOT NULL,
    domain text NOT NULL,
    country text,
    paired_items integer,
    competitor_direct_id text,
    competitor_direct_name text,
    country_id integer
);


--
-- Name: competitor_coverage_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.competitor_coverage_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: competitor_coverage_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.competitor_coverage_id_seq OWNED BY public.competitor_coverage.id;


--
-- Name: competitors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.competitors (
    id integer NOT NULL,
    name text NOT NULL,
    url text,
    category text,
    description text,
    notes text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    country_id integer,
    country_code character(2),
    vertical text,
    size text,
    max_paired_items integer,
    shop_group_id integer
);


--
-- Name: competitors_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.competitors_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: competitors_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.competitors_id_seq OWNED BY public.competitors.id;


--
-- Name: countries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.countries (
    id integer NOT NULL,
    iso2 character(2) NOT NULL,
    iso3 character(3) NOT NULL,
    numeric_code character(3),
    name_en text NOT NULL,
    name_cs text,
    name_local text,
    region text,
    subregion text,
    currency_code character(3),
    tld text,
    notino_domain text
);


--
-- Name: countries_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.countries_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: countries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.countries_id_seq OWNED BY public.countries.id;


--
-- Name: country_languages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.country_languages (
    id integer NOT NULL,
    country_id integer NOT NULL,
    language_id integer NOT NULL,
    is_primary boolean DEFAULT false,
    hreflang_code text NOT NULL
);


--
-- Name: country_languages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.country_languages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: country_languages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.country_languages_id_seq OWNED BY public.country_languages.id;


--
-- Name: data_quality_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.data_quality_snapshots (
    id bigint NOT NULL,
    snapshot_date date DEFAULT CURRENT_DATE NOT NULL,
    total_products integer,
    total_offers integer,
    total_shops_with_data integer,
    total_countries_with_data integer,
    total_price_history_rows bigint,
    products_with_ean integer,
    products_with_ean_pct numeric(5,2),
    products_with_brand integer,
    products_with_brand_pct numeric(5,2),
    products_with_image integer,
    products_with_image_pct numeric(5,2),
    products_with_description integer,
    products_with_volume integer,
    offers_with_price integer,
    offers_with_price_pct numeric(5,2),
    offers_in_stock integer,
    offers_in_stock_pct numeric(5,2),
    offers_with_affiliate integer,
    avg_price_eur numeric(10,2),
    median_price_eur numeric(10,2),
    offers_updated_today integer,
    offers_updated_24h_pct numeric(5,2),
    offers_stale_7d integer,
    offers_stale_7d_pct numeric(5,2),
    products_with_multi_offers integer,
    avg_offers_per_product numeric(5,2),
    max_offers_per_product integer,
    source_breakdown jsonb,
    country_breakdown jsonb,
    db_size_mb numeric(10,2)
);


--
-- Name: data_quality_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.data_quality_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: data_quality_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.data_quality_snapshots_id_seq OWNED BY public.data_quality_snapshots.id;


--
-- Name: exchange_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exchange_rates (
    id integer NOT NULL,
    base_currency character(3) DEFAULT 'EUR'::bpchar NOT NULL,
    target_currency character(3) NOT NULL,
    rate numeric(12,6) NOT NULL,
    rate_date date NOT NULL,
    source text DEFAULT 'ecb'::text
);


--
-- Name: exchange_rates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.exchange_rates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: exchange_rates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.exchange_rates_id_seq OWNED BY public.exchange_rates.id;


--
-- Name: extracted_shops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extracted_shops (
    id integer NOT NULL,
    name text NOT NULL,
    domain text,
    url text,
    source_portal text NOT NULL,
    country text NOT NULL,
    is_new_competitor boolean,
    matched_competitor_id integer,
    extracted_at timestamp with time zone DEFAULT now()
);


--
-- Name: extracted_shops_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.extracted_shops_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: extracted_shops_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.extracted_shops_id_seq OWNED BY public.extracted_shops.id;


--
-- Name: findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.findings (
    id integer NOT NULL,
    competitor_id integer,
    source text,
    data_type text,
    key text,
    value text,
    raw_data jsonb,
    scraped_at timestamp with time zone DEFAULT now()
);


--
-- Name: findings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.findings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: findings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.findings_id_seq OWNED BY public.findings.id;


--
-- Name: google_taxonomy; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.google_taxonomy (
    google_id integer NOT NULL,
    path_en text NOT NULL,
    name_en text NOT NULL,
    level integer DEFAULT 0 NOT NULL,
    parent_google_id integer,
    is_beauty boolean DEFAULT false,
    name_cs text,
    name_sk text,
    name_pl text,
    name_hu text,
    name_ro text,
    name_de text,
    name_fr text,
    name_it text,
    name_nl text,
    name_el text,
    name_bg text,
    name_pt text,
    name_sv text,
    name_da text,
    name_nb text,
    name_fi text,
    name_hr text,
    name_es text,
    mapped_category_id integer,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: hlidac_shopu_data; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hlidac_shopu_data (
    id bigint NOT NULL,
    product_url text NOT NULL,
    fetched_at timestamp with time zone DEFAULT now(),
    min_price numeric(12,2),
    max_price numeric(12,2),
    current_price numeric(12,2),
    currency character varying(3) DEFAULT 'CZK'::character varying,
    raw_json jsonb
);


--
-- Name: hlidac_shopu_data_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.hlidac_shopu_data_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: hlidac_shopu_data_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.hlidac_shopu_data_id_seq OWNED BY public.hlidac_shopu_data.id;


--
-- Name: images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.images (
    id bigint NOT NULL,
    source_url text NOT NULL,
    source_shop_id integer,
    storage_path text,
    cdn_url text,
    cdn_provider text,
    width integer,
    height integer,
    file_size_bytes integer,
    mime_type text,
    hash_sha256 text,
    variants jsonb,
    quality_score numeric(3,2),
    is_primary boolean DEFAULT false,
    has_watermark boolean DEFAULT false,
    alt_text text,
    status text DEFAULT 'pending'::text,
    downloaded_at timestamp with time zone,
    processed_at timestamp with time zone,
    last_verified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: images_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.images_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: images_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.images_id_seq OWNED BY public.images.id;


--
-- Name: ingredients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ingredients (
    id integer NOT NULL,
    inci_name text NOT NULL,
    name_common text,
    name_cs text,
    function text,
    safety_rating text,
    ew_score integer,
    comedogenic_rating integer,
    notes text
);


--
-- Name: ingredients_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ingredients_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ingredients_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ingredients_id_seq OWNED BY public.ingredients.id;


--
-- Name: languages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.languages (
    id integer NOT NULL,
    code character(2) NOT NULL,
    code_bcp47 text NOT NULL,
    name_en text NOT NULL,
    name_native text NOT NULL,
    primary_country_id integer,
    writing_direction text DEFAULT 'ltr'::text,
    is_active boolean DEFAULT true
);


--
-- Name: languages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.languages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: languages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.languages_id_seq OWNED BY public.languages.id;


--
-- Name: marketing_calendar_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.marketing_calendar_events (
    id bigint NOT NULL,
    competitor_id integer,
    country_id integer,
    event_date date NOT NULL,
    event_type text,
    campaign_id bigint,
    typical_discount_pct numeric(5,2),
    year integer
);


--
-- Name: marketing_calendar_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.marketing_calendar_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: marketing_calendar_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.marketing_calendar_events_id_seq OWNED BY public.marketing_calendar_events.id;


--
-- Name: marketing_campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.marketing_campaigns (
    id bigint NOT NULL,
    competitor_id integer NOT NULL,
    country_id integer,
    name text NOT NULL,
    slug text,
    campaign_type text,
    detected_at timestamp with time zone DEFAULT now(),
    start_date timestamp with time zone,
    end_date timestamp with time zone,
    duration_days integer,
    is_active boolean DEFAULT true,
    discount_pct numeric(5,2),
    discount_amount numeric(10,2),
    discount_code text,
    min_order_value numeric(10,2),
    headline text,
    subtitle text,
    description text,
    cta_text text,
    cta_url text,
    banner_url text,
    banner_location text,
    target_category_id integer,
    target_brand_id integer,
    target_products integer[],
    raw_html text,
    source_url text,
    screenshot_url text,
    scraped_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: marketing_campaigns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.marketing_campaigns_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: marketing_campaigns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.marketing_campaigns_id_seq OWNED BY public.marketing_campaigns.id;


--
-- Name: normalization_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.normalization_rules (
    id integer NOT NULL,
    entity_type text NOT NULL,
    pattern text NOT NULL,
    pattern_normalized text,
    is_regex boolean DEFAULT false,
    canonical text NOT NULL,
    canonical_id integer,
    language text,
    shop_group_id integer,
    source text DEFAULT 'manual'::text,
    confidence numeric(3,2) DEFAULT 1.0,
    verified boolean DEFAULT true,
    priority integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: normalization_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.normalization_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: normalization_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.normalization_rules_id_seq OWNED BY public.normalization_rules.id;


--
-- Name: offer_price_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_price_history (
    id bigint NOT NULL,
    offer_id bigint NOT NULL,
    product_id integer NOT NULL,
    shop_id integer NOT NULL,
    country_id integer NOT NULL,
    price numeric(12,2) NOT NULL,
    sale_price numeric(12,2),
    price_currency character(3) NOT NULL,
    availability public.availability_status,
    source public.feed_source,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
)
PARTITION BY RANGE (recorded_at);


--
-- Name: offer_price_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.offer_price_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: offer_price_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.offer_price_history_id_seq OWNED BY public.offer_price_history.id;


--
-- Name: offer_price_history_2026_04; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_price_history_2026_04 (
    id bigint DEFAULT nextval('public.offer_price_history_id_seq'::regclass) NOT NULL,
    offer_id bigint NOT NULL,
    product_id integer NOT NULL,
    shop_id integer NOT NULL,
    country_id integer NOT NULL,
    price numeric(12,2) NOT NULL,
    sale_price numeric(12,2),
    price_currency character(3) NOT NULL,
    availability public.availability_status,
    source public.feed_source,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: offer_price_history_2026_05; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_price_history_2026_05 (
    id bigint DEFAULT nextval('public.offer_price_history_id_seq'::regclass) NOT NULL,
    offer_id bigint NOT NULL,
    product_id integer NOT NULL,
    shop_id integer NOT NULL,
    country_id integer NOT NULL,
    price numeric(12,2) NOT NULL,
    sale_price numeric(12,2),
    price_currency character(3) NOT NULL,
    availability public.availability_status,
    source public.feed_source,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: offer_price_history_2026_06; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_price_history_2026_06 (
    id bigint DEFAULT nextval('public.offer_price_history_id_seq'::regclass) NOT NULL,
    offer_id bigint NOT NULL,
    product_id integer NOT NULL,
    shop_id integer NOT NULL,
    country_id integer NOT NULL,
    price numeric(12,2) NOT NULL,
    sale_price numeric(12,2),
    price_currency character(3) NOT NULL,
    availability public.availability_status,
    source public.feed_source,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: offer_price_history_2026_07; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offer_price_history_2026_07 (
    id bigint DEFAULT nextval('public.offer_price_history_id_seq'::regclass) NOT NULL,
    offer_id bigint NOT NULL,
    product_id integer NOT NULL,
    shop_id integer NOT NULL,
    country_id integer NOT NULL,
    price numeric(12,2) NOT NULL,
    sale_price numeric(12,2),
    price_currency character(3) NOT NULL,
    availability public.availability_status,
    source public.feed_source,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: portal_brands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_brands (
    id integer NOT NULL,
    portal_id integer NOT NULL,
    brand_name text NOT NULL,
    brand_url text,
    product_count integer,
    mapped_brand_id integer,
    extracted_at timestamp with time zone DEFAULT now()
);


--
-- Name: portal_brands_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.portal_brands_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: portal_brands_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.portal_brands_id_seq OWNED BY public.portal_brands.id;


--
-- Name: portal_categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_categories (
    id integer NOT NULL,
    portal_id integer NOT NULL,
    name text NOT NULL,
    name_en text,
    slug text,
    url text NOT NULL,
    parent_id integer,
    level integer DEFAULT 0 NOT NULL,
    path text,
    breadcrumb text[],
    product_count integer,
    shop_count integer,
    mapped_category_id integer,
    mapping_confidence numeric(3,2),
    is_beauty_relevant boolean DEFAULT true,
    scrape_priority integer DEFAULT 2,
    last_scraped_at timestamp with time zone,
    raw_data jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    estimated_products integer
);


--
-- Name: portal_categories_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.portal_categories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: portal_categories_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.portal_categories_id_seq OWNED BY public.portal_categories.id;


--
-- Name: portal_shops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.portal_shops (
    id integer NOT NULL,
    portal_id integer NOT NULL,
    shop_name text NOT NULL,
    shop_domain text,
    shop_url text,
    external_url text,
    rating numeric(3,1),
    review_count integer,
    product_count integer,
    matched_competitor_id integer,
    is_new boolean DEFAULT true,
    extracted_at timestamp with time zone DEFAULT now()
);


--
-- Name: portal_shops_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.portal_shops_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: portal_shops_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.portal_shops_id_seq OWNED BY public.portal_shops.id;


--
-- Name: price_portals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.price_portals (
    id integer NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    portal_type text,
    countries text[],
    website text NOT NULL,
    has_beauty_vertical boolean DEFAULT true,
    shops_count integer,
    products_count integer,
    has_price_history boolean DEFAULT false,
    has_reviews boolean DEFAULT false,
    has_api boolean DEFAULT false,
    api_url text,
    has_browser_extension boolean DEFAULT false,
    monetization text,
    sitemap_url text,
    product_url_pattern text,
    review_url_pattern text,
    founded_year integer,
    parent_company text,
    notes text,
    added_at timestamp with time zone DEFAULT now(),
    scrape_strategy text,
    category_tree_status text DEFAULT 'pending'::text,
    beauty_root_url text,
    beauty_category_count integer,
    total_beauty_products integer,
    data_source_priority integer DEFAULT 3,
    scrape_config jsonb DEFAULT '{}'::jsonb
);


--
-- Name: price_portals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.price_portals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: price_portals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.price_portals_id_seq OWNED BY public.price_portals.id;


--
-- Name: product_attributes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_attributes (
    id bigint NOT NULL,
    product_id integer NOT NULL,
    attribute_id integer NOT NULL,
    attribute_value_id integer,
    value_text text,
    value_numeric numeric
);


--
-- Name: product_attributes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_attributes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_attributes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_attributes_id_seq OWNED BY public.product_attributes.id;


--
-- Name: product_categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_categories (
    id integer NOT NULL,
    name text NOT NULL,
    name_cs text,
    slug text NOT NULL,
    parent_id integer,
    level integer DEFAULT 0 NOT NULL,
    path text,
    icon text,
    seo_title text,
    seo_description text,
    seo_h1 text,
    seo_keywords text[],
    seo_content text,
    seo_faq jsonb,
    description text,
    hero_image_url text,
    og_image_url text
);


--
-- Name: product_categories_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_categories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_categories_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_categories_id_seq OWNED BY public.product_categories.id;


--
-- Name: product_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_images (
    id bigint NOT NULL,
    product_id integer NOT NULL,
    image_id bigint NOT NULL,
    role text,
    sort_order integer DEFAULT 0
);


--
-- Name: product_images_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_images_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_images_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_images_id_seq OWNED BY public.product_images.id;


--
-- Name: product_ingredients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_ingredients (
    id integer NOT NULL,
    product_id integer,
    ean text,
    ingredient_id integer,
    "position" integer
);


--
-- Name: product_ingredients_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_ingredients_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_ingredients_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_ingredients_id_seq OWNED BY public.product_ingredients.id;


--
-- Name: product_matches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_matches (
    id integer NOT NULL,
    canonical_product_id integer,
    product_id integer NOT NULL,
    match_type text NOT NULL,
    match_confidence numeric,
    matched_at timestamp with time zone DEFAULT now()
);


--
-- Name: product_matches_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_matches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_matches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_matches_id_seq OWNED BY public.product_matches.id;


--
-- Name: product_merge_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_merge_queue (
    id bigint NOT NULL,
    product_a_id integer NOT NULL,
    product_b_id integer NOT NULL,
    match_type text NOT NULL,
    match_score numeric(3,2),
    match_evidence jsonb,
    status text DEFAULT 'pending'::text,
    merged_into_id integer,
    reviewed_by text,
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: product_merge_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_merge_queue_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_merge_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_merge_queue_id_seq OWNED BY public.product_merge_queue.id;


--
-- Name: product_offers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_offers (
    id bigint NOT NULL,
    product_id integer NOT NULL,
    shop_id integer NOT NULL,
    country_id integer NOT NULL,
    external_sku text,
    external_product_id text,
    offer_title text,
    offer_description text,
    product_url text NOT NULL,
    affiliate_url text,
    image_url text,
    price numeric(12,2),
    sale_price numeric(12,2),
    sale_price_start timestamp with time zone,
    sale_price_end timestamp with time zone,
    price_currency character(3) DEFAULT 'EUR'::bpchar NOT NULL,
    effective_price numeric(12,2) GENERATED ALWAYS AS (COALESCE(sale_price, price)) STORED,
    discount_pct numeric(5,2) GENERATED ALWAYS AS (
CASE
    WHEN ((price > (0)::numeric) AND (sale_price IS NOT NULL)) THEN round((((price - sale_price) / price) * (100)::numeric), 2)
    ELSE NULL::numeric
END) STORED,
    unit_price_per_ml numeric(10,4),
    volume_ml_snapshot numeric(10,2),
    availability public.availability_status,
    stock_level text,
    quantity_available integer,
    delivery_days_min integer,
    delivery_days_max integer,
    shipping_cost numeric(10,2),
    free_shipping_threshold numeric(10,2),
    condition public.product_condition DEFAULT 'new'::public.product_condition,
    affiliate_network text,
    affiliate_commission_pct numeric(5,2),
    custom_label_0 text,
    custom_label_1 text,
    custom_label_2 text,
    custom_label_3 text,
    custom_label_4 text,
    source public.feed_source,
    is_active boolean DEFAULT true,
    last_seen_at timestamp with time zone DEFAULT now(),
    first_seen_at timestamp with time zone DEFAULT now(),
    raw_offer_data jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: product_offers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_offers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_offers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_offers_id_seq OWNED BY public.product_offers.id;


--
-- Name: product_review_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_review_sources (
    id bigint NOT NULL,
    product_id integer,
    review_site_id integer,
    external_product_id text,
    review_count integer,
    avg_rating numeric(3,2),
    source_url text,
    last_synced_at timestamp with time zone
);


--
-- Name: product_review_sources_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_review_sources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_review_sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_review_sources_id_seq OWNED BY public.product_review_sources.id;


--
-- Name: product_shop_categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_shop_categories (
    id integer NOT NULL,
    product_id integer,
    shop_category_id integer,
    competitor_id integer,
    breadcrumb text[]
);


--
-- Name: product_shop_categories_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_shop_categories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_shop_categories_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_shop_categories_id_seq OWNED BY public.product_shop_categories.id;


--
-- Name: product_url_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_url_queue (
    id bigint NOT NULL,
    url text NOT NULL,
    shop text NOT NULL,
    discovered_at timestamp with time zone DEFAULT now(),
    processed_at timestamp with time zone,
    status text DEFAULT 'pending'::text,
    CONSTRAINT product_url_queue_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'done'::text, 'error'::text])))
);


--
-- Name: product_url_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_url_queue_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_url_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_url_queue_id_seq OWNED BY public.product_url_queue.id;


--
-- Name: products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.products (
    id integer NOT NULL,
    gtin13 text,
    gtin12 text,
    gtin8 text,
    gtin14 text,
    mpn text,
    title text NOT NULL,
    title_normalized text,
    description text,
    brand_id integer,
    category_id integer,
    volume_ml numeric(10,2),
    volume_unit_raw text,
    weight_g numeric(10,2),
    color text,
    size text,
    variant text,
    item_group_id text,
    canonical_image_url text,
    additional_image_urls text[],
    highlights text[],
    google_product_category_id text,
    product_type_path text[],
    first_seen_shop_id integer,
    first_seen_at timestamp with time zone DEFAULT now(),
    last_updated_at timestamp with time zone DEFAULT now(),
    raw_feed_data jsonb,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: products_legacy; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.products_legacy (
    id integer NOT NULL,
    ean text,
    sku text,
    competitor_id integer NOT NULL,
    brand_id integer,
    category_id integer,
    name text NOT NULL,
    name_normalized text,
    url text,
    image_url text,
    description text,
    volume text,
    volume_ml numeric,
    variant text,
    price numeric,
    price_original numeric,
    discount_pct numeric,
    currency text DEFAULT 'EUR'::text,
    price_per_ml numeric,
    in_stock boolean,
    stock_status text,
    delivery_days integer,
    affiliate_url text,
    affiliate_network text,
    country_id integer,
    raw_data jsonb,
    source text,
    scraped_at timestamp with time zone DEFAULT now()
);


--
-- Name: products_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.products_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: products_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.products_id_seq OWNED BY public.products_legacy.id;


--
-- Name: products_id_seq1; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.products_id_seq1
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: products_id_seq1; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.products_id_seq1 OWNED BY public.products.id;


--
-- Name: review_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.review_queue (
    id bigint NOT NULL,
    issue_type text NOT NULL,
    priority text DEFAULT 'medium'::text,
    entity_type text,
    entity_id integer,
    entity_id_b integer,
    title text NOT NULL,
    detail jsonb,
    ai_suggestion text,
    ai_confidence numeric(3,2),
    ai_model text,
    ai_resolved_at timestamp with time zone,
    status text DEFAULT 'open'::text,
    resolved_by text,
    resolution text,
    resolution_data jsonb,
    resolved_at timestamp with time zone,
    auto_detected boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone
);


--
-- Name: review_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.review_queue_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: review_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.review_queue_id_seq OWNED BY public.review_queue.id;


--
-- Name: review_sites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.review_sites (
    id integer NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    site_type text,
    countries text[],
    languages text[],
    website text NOT NULL,
    reviews_count integer,
    products_covered integer,
    has_ingredient_db boolean DEFAULT false,
    has_user_photos boolean DEFAULT false,
    has_editorial boolean DEFAULT false,
    sitemap_url text,
    product_url_pattern text,
    review_url_pattern text,
    api_url text,
    rating_scale text,
    rating_aspects text[],
    notes text,
    added_at timestamp with time zone DEFAULT now()
);


--
-- Name: review_sites_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.review_sites_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: review_sites_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.review_sites_id_seq OWNED BY public.review_sites.id;


--
-- Name: reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reviews (
    id integer NOT NULL,
    product_id integer,
    ean text,
    competitor_id integer,
    rating numeric,
    rating_count integer,
    review_text text,
    reviewer_name text,
    source text,
    language text,
    scraped_at timestamp with time zone DEFAULT now()
);


--
-- Name: reviews_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.reviews_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: reviews_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.reviews_id_seq OWNED BY public.reviews.id;


--
-- Name: scrape_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scrape_jobs (
    id integer NOT NULL,
    competitor_id integer,
    job_type text NOT NULL,
    status text DEFAULT 'pending'::text,
    pages_total integer,
    pages_scraped integer DEFAULT 0,
    pages_failed integer DEFAULT 0,
    products_found integer DEFAULT 0,
    products_updated integer DEFAULT 0,
    prices_changed integer DEFAULT 0,
    error_message text,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    duration_seconds integer,
    node_id text
);


--
-- Name: scrape_jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scrape_jobs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scrape_jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scrape_jobs_id_seq OWNED BY public.scrape_jobs.id;


--
-- Name: scrape_run_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scrape_run_metrics (
    id bigint NOT NULL,
    run_id text NOT NULL,
    scraper text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    duration_seconds numeric(10,2),
    shops_targeted integer,
    shops_success integer,
    shops_failed integer,
    urls_fetched integer,
    products_found integer,
    products_saved integer,
    products_updated integer,
    products_new integer,
    offers_created integer,
    offers_updated integer,
    price_changes_detected integer,
    hit_rate numeric(5,2),
    save_rate numeric(5,2),
    ean_coverage_pct numeric(5,2),
    price_coverage_pct numeric(5,2),
    errors_total integer DEFAULT 0,
    errors_network integer DEFAULT 0,
    errors_parse integer DEFAULT 0,
    errors_db integer DEFAULT 0,
    error_details jsonb,
    api_credits_used integer DEFAULT 0,
    api_cost_estimate numeric(8,4),
    shop_details jsonb,
    node_id text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: scrape_run_metrics_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scrape_run_metrics_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scrape_run_metrics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scrape_run_metrics_id_seq OWNED BY public.scrape_run_metrics.id;


--
-- Name: seo_keywords; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seo_keywords (
    id bigint NOT NULL,
    keyword text NOT NULL,
    keyword_normalized text,
    language_id integer NOT NULL,
    country_id integer NOT NULL,
    hreflang_code text NOT NULL,
    search_volume integer,
    difficulty numeric(4,1),
    cpc numeric(8,2),
    cpc_currency text,
    competition numeric(3,2),
    intent text,
    related_brand_id integer,
    related_category_id integer,
    related_product_id integer,
    target_page_id bigint,
    current_position integer,
    best_position integer,
    position_history jsonb,
    measured_at timestamp with time zone,
    serp_features text[],
    top_competitors text[],
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: seo_keywords_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seo_keywords_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seo_keywords_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.seo_keywords_id_seq OWNED BY public.seo_keywords.id;


--
-- Name: seo_landing_pages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seo_landing_pages (
    id bigint NOT NULL,
    page_type text NOT NULL,
    language_id integer NOT NULL,
    country_id integer NOT NULL,
    hreflang_code text NOT NULL,
    slug text NOT NULL,
    canonical_url text NOT NULL,
    brand_id integer,
    category_id integer,
    shop_group_id integer,
    competitor_id integer,
    attribute_filter jsonb,
    title text NOT NULL,
    meta_description text,
    h1 text,
    intro_content text,
    main_content text,
    faq jsonb,
    keywords text[],
    alternate_pages jsonb,
    product_count integer,
    avg_price numeric(10,2),
    min_price numeric(10,2),
    max_price numeric(10,2),
    currency text,
    published boolean DEFAULT false,
    noindex boolean DEFAULT false,
    priority numeric(2,1) DEFAULT 0.5,
    last_generated_at timestamp with time zone,
    content_generated_by text,
    content_quality_score numeric(3,2)
);


--
-- Name: seo_landing_pages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seo_landing_pages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seo_landing_pages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.seo_landing_pages_id_seq OWNED BY public.seo_landing_pages.id;


--
-- Name: seo_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seo_metrics (
    id integer NOT NULL,
    competitor_id integer,
    domain text,
    organic_traffic integer,
    keywords_count integer,
    domain_rating numeric,
    backlinks integer,
    top_pages jsonb,
    source text,
    measured_at timestamp with time zone DEFAULT now()
);


--
-- Name: seo_metrics_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seo_metrics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seo_metrics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.seo_metrics_id_seq OWNED BY public.seo_metrics.id;


--
-- Name: seo_url_patterns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seo_url_patterns (
    id integer NOT NULL,
    page_type text NOT NULL,
    hreflang_code text NOT NULL,
    language_id integer,
    country_id integer,
    url_template text NOT NULL,
    example_url text,
    section_name text
);


--
-- Name: seo_url_patterns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seo_url_patterns_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seo_url_patterns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.seo_url_patterns_id_seq OWNED BY public.seo_url_patterns.id;


--
-- Name: shipping_carriers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shipping_carriers (
    id integer NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    type text,
    countries_supported text[],
    website text,
    tracking_url_template text
);


--
-- Name: shipping_carriers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shipping_carriers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shipping_carriers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shipping_carriers_id_seq OWNED BY public.shipping_carriers.id;


--
-- Name: shop_categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_categories (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    country_id integer,
    shop_category_id text,
    url text NOT NULL,
    slug text,
    parent_id integer,
    level integer DEFAULT 0 NOT NULL,
    path text,
    breadcrumb text[],
    name text NOT NULL,
    name_normalized text,
    description text,
    meta_title text,
    meta_description text,
    h1 text,
    canonical_url text,
    product_count integer,
    mapped_category_id integer,
    mapping_confidence numeric(3,2),
    category_image_url text,
    raw_data jsonb,
    scraped_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: shop_categories_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_categories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_categories_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_categories_id_seq OWNED BY public.shop_categories.id;


--
-- Name: shop_filters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_filters (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    shop_category_id integer,
    filter_name text NOT NULL,
    filter_name_normalized text,
    filter_type text,
    "values" jsonb,
    values_count integer,
    parent_filter_id integer,
    scraped_at timestamp with time zone DEFAULT now()
);


--
-- Name: shop_filters_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_filters_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_filters_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_filters_id_seq OWNED BY public.shop_filters.id;


--
-- Name: shop_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_groups (
    id integer NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    parent_company text,
    headquarters_country_id integer,
    website_main text,
    category text,
    vertical text,
    size text,
    founded_year integer,
    total_countries integer,
    notes text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: shop_groups_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_groups_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_groups_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_groups_id_seq OWNED BY public.shop_groups.id;


--
-- Name: shop_loyalty_programs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_loyalty_programs (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    country_id integer,
    program_name text NOT NULL,
    program_type text,
    tiers jsonb,
    earn_rate numeric(5,2),
    redemption_rate numeric(5,2),
    benefits text[],
    join_requirements text,
    scraped_at timestamp with time zone DEFAULT now()
);


--
-- Name: shop_loyalty_programs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_loyalty_programs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_loyalty_programs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_loyalty_programs_id_seq OWNED BY public.shop_loyalty_programs.id;


--
-- Name: shop_payment_methods; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_payment_methods (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    country_id integer,
    method text NOT NULL,
    fee_amount numeric(10,2),
    fee_pct numeric(5,2),
    is_free boolean DEFAULT true,
    description text
);


--
-- Name: shop_payment_methods_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_payment_methods_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_payment_methods_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_payment_methods_id_seq OWNED BY public.shop_payment_methods.id;


--
-- Name: shop_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_profiles (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    country_id integer,
    headline text,
    intro text,
    about text,
    pros text[],
    cons text[],
    best_for text[],
    avg_price_index numeric(5,2),
    avg_shipping_cost numeric(10,2),
    avg_delivery_days numeric(3,1),
    omnibus_compliance_score numeric(5,2),
    customer_rating numeric(3,2),
    seo_title text,
    seo_description text,
    seo_keywords text[],
    canonical_url text,
    verified boolean DEFAULT false,
    trusted boolean DEFAULT false,
    warning_flag text,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: shop_profiles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_profiles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_profiles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_profiles_id_seq OWNED BY public.shop_profiles.id;


--
-- Name: shop_returns_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_returns_policies (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    country_id integer,
    return_days integer,
    free_returns boolean,
    return_fee numeric(10,2),
    conditions text,
    exceptions text[],
    scraped_at timestamp with time zone DEFAULT now()
);


--
-- Name: shop_returns_policies_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_returns_policies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_returns_policies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_returns_policies_id_seq OWNED BY public.shop_returns_policies.id;


--
-- Name: shop_shipping_options; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_shipping_options (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    country_id integer,
    carrier_id integer,
    service_name text,
    delivery_days_min integer,
    delivery_days_max integer,
    base_price numeric(10,2),
    currency text DEFAULT 'EUR'::text,
    free_shipping_threshold numeric(10,2),
    free_shipping_always boolean DEFAULT false,
    price_per_kg numeric(8,2),
    max_weight_kg numeric(6,2),
    cod_available boolean,
    cod_fee numeric(10,2),
    is_premium boolean DEFAULT false,
    description text,
    conditions text,
    scraped_at timestamp with time zone DEFAULT now()
);


--
-- Name: shop_shipping_options_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_shipping_options_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_shipping_options_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_shipping_options_id_seq OWNED BY public.shop_shipping_options.id;


--
-- Name: shop_usps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shop_usps (
    id integer NOT NULL,
    competitor_id integer NOT NULL,
    country_id integer,
    usp_type text NOT NULL,
    title text NOT NULL,
    description text,
    value text,
    icon_url text,
    days_number integer,
    is_free boolean,
    scraped_at timestamp with time zone DEFAULT now(),
    source_url text
);


--
-- Name: shop_usps_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shop_usps_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: shop_usps_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shop_usps_id_seq OWNED BY public.shop_usps.id;


--
-- Name: sitemap_crawl_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sitemap_crawl_log (
    id bigint NOT NULL,
    shop text NOT NULL,
    sitemap_url text NOT NULL,
    crawled_at timestamp with time zone DEFAULT now(),
    urls_found integer DEFAULT 0,
    success boolean DEFAULT true,
    error_msg text
);


--
-- Name: sitemap_crawl_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sitemap_crawl_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sitemap_crawl_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sitemap_crawl_log_id_seq OWNED BY public.sitemap_crawl_log.id;


--
-- Name: translations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.translations (
    id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_id integer NOT NULL,
    language_id integer NOT NULL,
    field_name text NOT NULL,
    value text NOT NULL,
    source text,
    confidence numeric(3,2),
    reviewed boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: translations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.translations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: translations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.translations_id_seq OWNED BY public.translations.id;


--
-- Name: website_checks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.website_checks (
    id integer NOT NULL,
    competitor_id integer,
    domain text NOT NULL,
    url text,
    http_status integer,
    redirect_url text,
    server_header text,
    protection text,
    captcha text,
    cdn text,
    waf_details text,
    firecrawl_status text,
    firecrawl_content_length integer,
    response_time_ms integer,
    headers_raw jsonb,
    checked_at timestamp with time zone DEFAULT now(),
    robots_txt text,
    robots_disallow_all boolean DEFAULT false,
    robots_blocks_ai boolean DEFAULT false,
    robots_sitemap text,
    bot_control text,
    difficulty text,
    recommended_tool text,
    sitemap_url text,
    sitemap_status integer,
    sitemap_type text,
    sitemap_urls_count integer,
    sitemap_product_feed text,
    sitemap_feeds jsonb,
    scrape_strategy text,
    has_jsonld_product boolean,
    price_source text,
    price_extraction_cost text,
    rss_feed_url text,
    atom_feed_url text,
    api_url text,
    google_merchant_feed text,
    schema_org_types text[],
    tech_stack jsonb,
    social_links jsonb,
    contact_email text,
    meta_title text,
    meta_description text,
    language text,
    hreflang_tags jsonb,
    canonical_domain text,
    portal_id integer,
    entity_type text,
    CONSTRAINT chk_entity_exclusive CHECK (((((competitor_id IS NOT NULL))::integer + ((portal_id IS NOT NULL))::integer) = 1))
);


--
-- Name: v_competitor_data_sources; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_competitor_data_sources AS
 SELECT c.id,
    c.name AS domain,
    c.vertical,
    c.category,
    c.size,
    co.name_cs AS country,
    co.iso2 AS country_code,
    wc.protection,
    wc.cdn,
    wc.bot_control,
    wc.captcha,
    wc.difficulty,
    wc.http_status,
    wc.response_time_ms,
    wc.scrape_strategy,
    wc.firecrawl_status,
    wc.price_source,
    wc.price_extraction_cost,
    wc.sitemap_url,
    wc.sitemap_type,
    wc.sitemap_product_feed,
    wc.sitemap_urls_count,
    jsonb_array_length(COALESCE(wc.sitemap_feeds, '[]'::jsonb)) AS sitemap_feed_count,
    ap.has_program AS has_affiliate,
    ap.network AS affiliate_network,
    ap.program_url AS affiliate_url,
    ap.feed_url AS affiliate_feed_url,
    wc.robots_blocks_ai,
    wc.robots_disallow_all,
    wc.redirect_url AS actual_url,
    (((((
        CASE
            WHEN (wc.sitemap_product_feed IS NOT NULL) THEN 30
            ELSE 0
        END +
        CASE
            WHEN (ap.has_program = true) THEN 20
            ELSE 0
        END) +
        CASE
            WHEN (ap.feed_url IS NOT NULL) THEN 20
            ELSE 0
        END) +
        CASE
            WHEN (wc.price_source = ANY (ARRAY['jsonld'::text, 'microdata'::text])) THEN 15
            ELSE 0
        END) +
        CASE
            WHEN (wc.firecrawl_status = ANY (ARRAY['ok'::text, 'predicted_ok'::text])) THEN 10
            ELSE 0
        END) +
        CASE
            WHEN (wc.difficulty = ANY (ARRAY['easy'::text, 'medium'::text])) THEN 5
            ELSE 0
        END) AS data_quality_score
   FROM (((public.competitors c
     LEFT JOIN public.countries co ON ((co.id = c.country_id)))
     LEFT JOIN public.website_checks wc ON ((wc.competitor_id = c.id)))
     LEFT JOIN public.affiliate_programs ap ON ((ap.competitor_id = c.id)))
  ORDER BY c.category, c.name;


--
-- Name: v_offers_eur; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_offers_eur AS
 SELECT po.id,
    po.product_id,
    po.shop_id,
    po.country_id,
    po.external_sku,
    po.external_product_id,
    po.offer_title,
    po.offer_description,
    po.product_url,
    po.affiliate_url,
    po.image_url,
    po.price,
    po.sale_price,
    po.sale_price_start,
    po.sale_price_end,
    po.price_currency,
    po.effective_price,
    po.discount_pct,
    po.unit_price_per_ml,
    po.volume_ml_snapshot,
    po.availability,
    po.stock_level,
    po.quantity_available,
    po.delivery_days_min,
    po.delivery_days_max,
    po.shipping_cost,
    po.free_shipping_threshold,
    po.condition,
    po.affiliate_network,
    po.affiliate_commission_pct,
    po.custom_label_0,
    po.custom_label_1,
    po.custom_label_2,
    po.custom_label_3,
    po.custom_label_4,
    po.source,
    po.is_active,
    po.last_seen_at,
    po.first_seen_at,
    po.raw_offer_data,
    po.created_at,
    po.updated_at,
    p.title AS product_title,
    p.gtin13,
    b.name AS brand_name,
    c.name AS shop_name,
    co.iso2 AS country_code,
    co.name_cs AS country_name,
    public.price_in_eur(po.effective_price, po.price_currency) AS price_eur,
    public.price_in_eur(po.price, po.price_currency) AS original_price_eur
   FROM ((((public.product_offers po
     JOIN public.products p ON ((p.id = po.product_id)))
     LEFT JOIN public.brands b ON ((b.id = p.brand_id)))
     JOIN public.competitors c ON ((c.id = po.shop_id)))
     JOIN public.countries co ON ((co.id = po.country_id)))
  WHERE (po.is_active = true);


--
-- Name: v_portal_scrape_overview; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_portal_scrape_overview AS
 SELECT pp.slug,
    pp.name,
    pp.countries,
    pp.portal_type,
    pp.scrape_strategy,
    pp.data_source_priority,
    pp.beauty_root_url,
    pp.category_tree_status,
    pp.beauty_category_count,
    pp.total_beauty_products,
    wc.difficulty,
    wc.protection,
    wc.cdn,
    wc.sitemap_urls_count,
    ( SELECT count(*) AS count
           FROM public.portal_categories pc
          WHERE (pc.portal_id = pp.id)) AS categories_mapped
   FROM (public.price_portals pp
     LEFT JOIN public.website_checks wc ON (((wc.portal_id = pp.id) AND (wc.entity_type = 'portal'::text))))
  ORDER BY pp.data_source_priority, pp.slug;


--
-- Name: v_seo_potential; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_seo_potential AS
 SELECT (533 * 30000) AS potential_product_pages,
    500 AS estimated_brands,
    (( SELECT count(*) AS count
           FROM public.product_categories) * 27) AS category_pages,
    (500 * ( SELECT count(*) AS count
           FROM public.product_categories)) AS brand_category_pages,
    (( SELECT count(*) AS count
           FROM public.product_categories) * 27) AS best_in_country_pages,
    1000 AS comparison_pages_estimate,
    (533 * 100) AS review_pages_estimate;


--
-- Name: variant_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.variant_mappings (
    id integer NOT NULL,
    pattern text NOT NULL,
    canonical text NOT NULL,
    language text
);


--
-- Name: variant_mappings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.variant_mappings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: variant_mappings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.variant_mappings_id_seq OWNED BY public.variant_mappings.id;


--
-- Name: volume_conversions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.volume_conversions (
    id integer NOT NULL,
    pattern text NOT NULL,
    multiplier_to_ml numeric(10,4),
    canonical_unit text NOT NULL
);


--
-- Name: volume_conversions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.volume_conversions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: volume_conversions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.volume_conversions_id_seq OWNED BY public.volume_conversions.id;


--
-- Name: website_checks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.website_checks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: website_checks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.website_checks_id_seq OWNED BY public.website_checks.id;


--
-- Name: offer_price_history_2026_04; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history ATTACH PARTITION public.offer_price_history_2026_04 FOR VALUES FROM ('2026-04-01 00:00:00+00') TO ('2026-05-01 00:00:00+00');


--
-- Name: offer_price_history_2026_05; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history ATTACH PARTITION public.offer_price_history_2026_05 FOR VALUES FROM ('2026-05-01 00:00:00+00') TO ('2026-06-01 00:00:00+00');


--
-- Name: offer_price_history_2026_06; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history ATTACH PARTITION public.offer_price_history_2026_06 FOR VALUES FROM ('2026-06-01 00:00:00+00') TO ('2026-07-01 00:00:00+00');


--
-- Name: offer_price_history_2026_07; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history ATTACH PARTITION public.offer_price_history_2026_07 FOR VALUES FROM ('2026-07-01 00:00:00+00') TO ('2026-08-01 00:00:00+00');


--
-- Name: affiliate_networks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.affiliate_networks ALTER COLUMN id SET DEFAULT nextval('public.affiliate_networks_id_seq'::regclass);


--
-- Name: affiliate_programs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.affiliate_programs ALTER COLUMN id SET DEFAULT nextval('public.affiliate_programs_id_seq'::regclass);


--
-- Name: ai_sweep_config id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_sweep_config ALTER COLUMN id SET DEFAULT nextval('public.ai_sweep_config_id_seq'::regclass);


--
-- Name: ai_sweep_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_sweep_log ALTER COLUMN id SET DEFAULT nextval('public.ai_sweep_log_id_seq'::regclass);


--
-- Name: attribute_values id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attribute_values ALTER COLUMN id SET DEFAULT nextval('public.attribute_values_id_seq'::regclass);


--
-- Name: attributes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attributes ALTER COLUMN id SET DEFAULT nextval('public.attributes_id_seq'::regclass);


--
-- Name: banner_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banner_snapshots ALTER COLUMN id SET DEFAULT nextval('public.banner_snapshots_id_seq'::regclass);


--
-- Name: brand_aliases id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_aliases ALTER COLUMN id SET DEFAULT nextval('public.brand_aliases_id_seq'::regclass);


--
-- Name: brand_images id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_images ALTER COLUMN id SET DEFAULT nextval('public.brand_images_id_seq'::regclass);


--
-- Name: brand_protection_alerts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_protection_alerts ALTER COLUMN id SET DEFAULT nextval('public.brand_protection_alerts_id_seq'::regclass);


--
-- Name: brand_subscriptions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_subscriptions ALTER COLUMN id SET DEFAULT nextval('public.brand_subscriptions_id_seq'::regclass);


--
-- Name: brands id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brands ALTER COLUMN id SET DEFAULT nextval('public.brands_id_seq'::regclass);


--
-- Name: category_mappings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_mappings ALTER COLUMN id SET DEFAULT nextval('public.category_mappings_id_seq'::regclass);


--
-- Name: category_tree_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_tree_snapshots ALTER COLUMN id SET DEFAULT nextval('public.category_tree_snapshots_id_seq'::regclass);


--
-- Name: competitor_coverage id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_coverage ALTER COLUMN id SET DEFAULT nextval('public.competitor_coverage_id_seq'::regclass);


--
-- Name: competitors id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitors ALTER COLUMN id SET DEFAULT nextval('public.competitors_id_seq'::regclass);


--
-- Name: countries id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.countries ALTER COLUMN id SET DEFAULT nextval('public.countries_id_seq'::regclass);


--
-- Name: country_languages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.country_languages ALTER COLUMN id SET DEFAULT nextval('public.country_languages_id_seq'::regclass);


--
-- Name: data_quality_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_quality_snapshots ALTER COLUMN id SET DEFAULT nextval('public.data_quality_snapshots_id_seq'::regclass);


--
-- Name: exchange_rates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates ALTER COLUMN id SET DEFAULT nextval('public.exchange_rates_id_seq'::regclass);


--
-- Name: extracted_shops id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extracted_shops ALTER COLUMN id SET DEFAULT nextval('public.extracted_shops_id_seq'::regclass);


--
-- Name: findings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings ALTER COLUMN id SET DEFAULT nextval('public.findings_id_seq'::regclass);


--
-- Name: hlidac_shopu_data id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hlidac_shopu_data ALTER COLUMN id SET DEFAULT nextval('public.hlidac_shopu_data_id_seq'::regclass);


--
-- Name: images id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.images ALTER COLUMN id SET DEFAULT nextval('public.images_id_seq'::regclass);


--
-- Name: ingredients id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingredients ALTER COLUMN id SET DEFAULT nextval('public.ingredients_id_seq'::regclass);


--
-- Name: languages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.languages ALTER COLUMN id SET DEFAULT nextval('public.languages_id_seq'::regclass);


--
-- Name: marketing_calendar_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_calendar_events ALTER COLUMN id SET DEFAULT nextval('public.marketing_calendar_events_id_seq'::regclass);


--
-- Name: marketing_campaigns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_campaigns ALTER COLUMN id SET DEFAULT nextval('public.marketing_campaigns_id_seq'::regclass);


--
-- Name: normalization_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.normalization_rules ALTER COLUMN id SET DEFAULT nextval('public.normalization_rules_id_seq'::regclass);


--
-- Name: offer_price_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history ALTER COLUMN id SET DEFAULT nextval('public.offer_price_history_id_seq'::regclass);


--
-- Name: portal_brands id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_brands ALTER COLUMN id SET DEFAULT nextval('public.portal_brands_id_seq'::regclass);


--
-- Name: portal_categories id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_categories ALTER COLUMN id SET DEFAULT nextval('public.portal_categories_id_seq'::regclass);


--
-- Name: portal_shops id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_shops ALTER COLUMN id SET DEFAULT nextval('public.portal_shops_id_seq'::regclass);


--
-- Name: price_portals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_portals ALTER COLUMN id SET DEFAULT nextval('public.price_portals_id_seq'::regclass);


--
-- Name: product_attributes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_attributes ALTER COLUMN id SET DEFAULT nextval('public.product_attributes_id_seq'::regclass);


--
-- Name: product_categories id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_categories ALTER COLUMN id SET DEFAULT nextval('public.product_categories_id_seq'::regclass);


--
-- Name: product_images id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_images ALTER COLUMN id SET DEFAULT nextval('public.product_images_id_seq'::regclass);


--
-- Name: product_ingredients id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_ingredients ALTER COLUMN id SET DEFAULT nextval('public.product_ingredients_id_seq'::regclass);


--
-- Name: product_matches id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_matches ALTER COLUMN id SET DEFAULT nextval('public.product_matches_id_seq'::regclass);


--
-- Name: product_merge_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_merge_queue ALTER COLUMN id SET DEFAULT nextval('public.product_merge_queue_id_seq'::regclass);


--
-- Name: product_offers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_offers ALTER COLUMN id SET DEFAULT nextval('public.product_offers_id_seq'::regclass);


--
-- Name: product_review_sources id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_review_sources ALTER COLUMN id SET DEFAULT nextval('public.product_review_sources_id_seq'::regclass);


--
-- Name: product_shop_categories id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_shop_categories ALTER COLUMN id SET DEFAULT nextval('public.product_shop_categories_id_seq'::regclass);


--
-- Name: product_url_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_url_queue ALTER COLUMN id SET DEFAULT nextval('public.product_url_queue_id_seq'::regclass);


--
-- Name: products id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products ALTER COLUMN id SET DEFAULT nextval('public.products_id_seq1'::regclass);


--
-- Name: products_legacy id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products_legacy ALTER COLUMN id SET DEFAULT nextval('public.products_id_seq'::regclass);


--
-- Name: review_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_queue ALTER COLUMN id SET DEFAULT nextval('public.review_queue_id_seq'::regclass);


--
-- Name: review_sites id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_sites ALTER COLUMN id SET DEFAULT nextval('public.review_sites_id_seq'::regclass);


--
-- Name: reviews id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews ALTER COLUMN id SET DEFAULT nextval('public.reviews_id_seq'::regclass);


--
-- Name: scrape_jobs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_jobs ALTER COLUMN id SET DEFAULT nextval('public.scrape_jobs_id_seq'::regclass);


--
-- Name: scrape_run_metrics id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_run_metrics ALTER COLUMN id SET DEFAULT nextval('public.scrape_run_metrics_id_seq'::regclass);


--
-- Name: seo_keywords id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_keywords ALTER COLUMN id SET DEFAULT nextval('public.seo_keywords_id_seq'::regclass);


--
-- Name: seo_landing_pages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages ALTER COLUMN id SET DEFAULT nextval('public.seo_landing_pages_id_seq'::regclass);


--
-- Name: seo_metrics id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_metrics ALTER COLUMN id SET DEFAULT nextval('public.seo_metrics_id_seq'::regclass);


--
-- Name: seo_url_patterns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_url_patterns ALTER COLUMN id SET DEFAULT nextval('public.seo_url_patterns_id_seq'::regclass);


--
-- Name: shipping_carriers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shipping_carriers ALTER COLUMN id SET DEFAULT nextval('public.shipping_carriers_id_seq'::regclass);


--
-- Name: shop_categories id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_categories ALTER COLUMN id SET DEFAULT nextval('public.shop_categories_id_seq'::regclass);


--
-- Name: shop_filters id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_filters ALTER COLUMN id SET DEFAULT nextval('public.shop_filters_id_seq'::regclass);


--
-- Name: shop_groups id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_groups ALTER COLUMN id SET DEFAULT nextval('public.shop_groups_id_seq'::regclass);


--
-- Name: shop_loyalty_programs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_loyalty_programs ALTER COLUMN id SET DEFAULT nextval('public.shop_loyalty_programs_id_seq'::regclass);


--
-- Name: shop_payment_methods id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_payment_methods ALTER COLUMN id SET DEFAULT nextval('public.shop_payment_methods_id_seq'::regclass);


--
-- Name: shop_profiles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_profiles ALTER COLUMN id SET DEFAULT nextval('public.shop_profiles_id_seq'::regclass);


--
-- Name: shop_returns_policies id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_returns_policies ALTER COLUMN id SET DEFAULT nextval('public.shop_returns_policies_id_seq'::regclass);


--
-- Name: shop_shipping_options id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_shipping_options ALTER COLUMN id SET DEFAULT nextval('public.shop_shipping_options_id_seq'::regclass);


--
-- Name: shop_usps id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_usps ALTER COLUMN id SET DEFAULT nextval('public.shop_usps_id_seq'::regclass);


--
-- Name: sitemap_crawl_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sitemap_crawl_log ALTER COLUMN id SET DEFAULT nextval('public.sitemap_crawl_log_id_seq'::regclass);


--
-- Name: translations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.translations ALTER COLUMN id SET DEFAULT nextval('public.translations_id_seq'::regclass);


--
-- Name: variant_mappings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.variant_mappings ALTER COLUMN id SET DEFAULT nextval('public.variant_mappings_id_seq'::regclass);


--
-- Name: volume_conversions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.volume_conversions ALTER COLUMN id SET DEFAULT nextval('public.volume_conversions_id_seq'::regclass);


--
-- Name: website_checks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.website_checks ALTER COLUMN id SET DEFAULT nextval('public.website_checks_id_seq'::regclass);


--
-- Name: affiliate_networks affiliate_networks_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.affiliate_networks
    ADD CONSTRAINT affiliate_networks_name_key UNIQUE (name);


--
-- Name: affiliate_networks affiliate_networks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.affiliate_networks
    ADD CONSTRAINT affiliate_networks_pkey PRIMARY KEY (id);


--
-- Name: affiliate_programs affiliate_programs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.affiliate_programs
    ADD CONSTRAINT affiliate_programs_pkey PRIMARY KEY (id);


--
-- Name: ai_sweep_config ai_sweep_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_sweep_config
    ADD CONSTRAINT ai_sweep_config_pkey PRIMARY KEY (id);


--
-- Name: ai_sweep_config ai_sweep_config_sweep_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_sweep_config
    ADD CONSTRAINT ai_sweep_config_sweep_type_key UNIQUE (sweep_type);


--
-- Name: ai_sweep_log ai_sweep_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_sweep_log
    ADD CONSTRAINT ai_sweep_log_pkey PRIMARY KEY (id);


--
-- Name: attribute_values attribute_values_attribute_id_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attribute_values
    ADD CONSTRAINT attribute_values_attribute_id_code_key UNIQUE (attribute_id, code);


--
-- Name: attribute_values attribute_values_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attribute_values
    ADD CONSTRAINT attribute_values_pkey PRIMARY KEY (id);


--
-- Name: attributes attributes_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attributes
    ADD CONSTRAINT attributes_code_key UNIQUE (code);


--
-- Name: attributes attributes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attributes
    ADD CONSTRAINT attributes_pkey PRIMARY KEY (id);


--
-- Name: banner_snapshots banner_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banner_snapshots
    ADD CONSTRAINT banner_snapshots_pkey PRIMARY KEY (id);


--
-- Name: brand_aliases brand_aliases_alias_normalized_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_aliases
    ADD CONSTRAINT brand_aliases_alias_normalized_key UNIQUE (alias_normalized);


--
-- Name: brand_aliases brand_aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_aliases
    ADD CONSTRAINT brand_aliases_pkey PRIMARY KEY (id);


--
-- Name: brand_images brand_images_brand_id_role_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_images
    ADD CONSTRAINT brand_images_brand_id_role_key UNIQUE (brand_id, role);


--
-- Name: brand_images brand_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_images
    ADD CONSTRAINT brand_images_pkey PRIMARY KEY (id);


--
-- Name: brand_protection_alerts brand_protection_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_protection_alerts
    ADD CONSTRAINT brand_protection_alerts_pkey PRIMARY KEY (id);


--
-- Name: brand_subscriptions brand_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_subscriptions
    ADD CONSTRAINT brand_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: brands brands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brands
    ADD CONSTRAINT brands_pkey PRIMARY KEY (id);


--
-- Name: category_mappings category_mappings_competitor_id_source_category_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_mappings
    ADD CONSTRAINT category_mappings_competitor_id_source_category_key UNIQUE (competitor_id, source_category);


--
-- Name: category_mappings category_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_mappings
    ADD CONSTRAINT category_mappings_pkey PRIMARY KEY (id);


--
-- Name: category_tree_snapshots category_tree_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_tree_snapshots
    ADD CONSTRAINT category_tree_snapshots_pkey PRIMARY KEY (id);


--
-- Name: competitor_coverage competitor_coverage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_coverage
    ADD CONSTRAINT competitor_coverage_pkey PRIMARY KEY (id);


--
-- Name: competitors competitors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitors
    ADD CONSTRAINT competitors_pkey PRIMARY KEY (id);


--
-- Name: countries countries_iso2_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.countries
    ADD CONSTRAINT countries_iso2_key UNIQUE (iso2);


--
-- Name: countries countries_iso3_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.countries
    ADD CONSTRAINT countries_iso3_key UNIQUE (iso3);


--
-- Name: countries countries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.countries
    ADD CONSTRAINT countries_pkey PRIMARY KEY (id);


--
-- Name: country_languages country_languages_country_id_language_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.country_languages
    ADD CONSTRAINT country_languages_country_id_language_id_key UNIQUE (country_id, language_id);


--
-- Name: country_languages country_languages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.country_languages
    ADD CONSTRAINT country_languages_pkey PRIMARY KEY (id);


--
-- Name: data_quality_snapshots data_quality_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_quality_snapshots
    ADD CONSTRAINT data_quality_snapshots_pkey PRIMARY KEY (id);


--
-- Name: data_quality_snapshots data_quality_snapshots_snapshot_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.data_quality_snapshots
    ADD CONSTRAINT data_quality_snapshots_snapshot_date_key UNIQUE (snapshot_date);


--
-- Name: exchange_rates exchange_rates_base_currency_target_currency_rate_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates
    ADD CONSTRAINT exchange_rates_base_currency_target_currency_rate_date_key UNIQUE (base_currency, target_currency, rate_date);


--
-- Name: exchange_rates exchange_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates
    ADD CONSTRAINT exchange_rates_pkey PRIMARY KEY (id);


--
-- Name: extracted_shops extracted_shops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extracted_shops
    ADD CONSTRAINT extracted_shops_pkey PRIMARY KEY (id);


--
-- Name: findings findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT findings_pkey PRIMARY KEY (id);


--
-- Name: google_taxonomy google_taxonomy_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_taxonomy
    ADD CONSTRAINT google_taxonomy_pkey PRIMARY KEY (google_id);


--
-- Name: hlidac_shopu_data hlidac_shopu_data_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hlidac_shopu_data
    ADD CONSTRAINT hlidac_shopu_data_pkey PRIMARY KEY (id);


--
-- Name: images images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.images
    ADD CONSTRAINT images_pkey PRIMARY KEY (id);


--
-- Name: images images_source_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.images
    ADD CONSTRAINT images_source_url_key UNIQUE (source_url);


--
-- Name: ingredients ingredients_inci_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingredients
    ADD CONSTRAINT ingredients_inci_name_key UNIQUE (inci_name);


--
-- Name: ingredients ingredients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingredients
    ADD CONSTRAINT ingredients_pkey PRIMARY KEY (id);


--
-- Name: languages languages_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.languages
    ADD CONSTRAINT languages_code_key UNIQUE (code);


--
-- Name: languages languages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.languages
    ADD CONSTRAINT languages_pkey PRIMARY KEY (id);


--
-- Name: marketing_calendar_events marketing_calendar_events_competitor_id_event_date_event_ty_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_calendar_events
    ADD CONSTRAINT marketing_calendar_events_competitor_id_event_date_event_ty_key UNIQUE (competitor_id, event_date, event_type);


--
-- Name: marketing_calendar_events marketing_calendar_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_calendar_events
    ADD CONSTRAINT marketing_calendar_events_pkey PRIMARY KEY (id);


--
-- Name: marketing_campaigns marketing_campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_campaigns
    ADD CONSTRAINT marketing_campaigns_pkey PRIMARY KEY (id);


--
-- Name: normalization_rules normalization_rules_entity_type_pattern_language_shop_group_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.normalization_rules
    ADD CONSTRAINT normalization_rules_entity_type_pattern_language_shop_group_key UNIQUE (entity_type, pattern, language, shop_group_id);


--
-- Name: normalization_rules normalization_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.normalization_rules
    ADD CONSTRAINT normalization_rules_pkey PRIMARY KEY (id);


--
-- Name: offer_price_history offer_price_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history
    ADD CONSTRAINT offer_price_history_pkey PRIMARY KEY (id, recorded_at);


--
-- Name: offer_price_history_2026_04 offer_price_history_2026_04_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history_2026_04
    ADD CONSTRAINT offer_price_history_2026_04_pkey PRIMARY KEY (id, recorded_at);


--
-- Name: offer_price_history_2026_05 offer_price_history_2026_05_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history_2026_05
    ADD CONSTRAINT offer_price_history_2026_05_pkey PRIMARY KEY (id, recorded_at);


--
-- Name: offer_price_history_2026_06 offer_price_history_2026_06_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history_2026_06
    ADD CONSTRAINT offer_price_history_2026_06_pkey PRIMARY KEY (id, recorded_at);


--
-- Name: offer_price_history_2026_07 offer_price_history_2026_07_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offer_price_history_2026_07
    ADD CONSTRAINT offer_price_history_2026_07_pkey PRIMARY KEY (id, recorded_at);


--
-- Name: portal_brands portal_brands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_brands
    ADD CONSTRAINT portal_brands_pkey PRIMARY KEY (id);


--
-- Name: portal_brands portal_brands_portal_id_brand_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_brands
    ADD CONSTRAINT portal_brands_portal_id_brand_name_key UNIQUE (portal_id, brand_name);


--
-- Name: portal_categories portal_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_categories
    ADD CONSTRAINT portal_categories_pkey PRIMARY KEY (id);


--
-- Name: portal_categories portal_categories_portal_id_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_categories
    ADD CONSTRAINT portal_categories_portal_id_url_key UNIQUE (portal_id, url);


--
-- Name: portal_shops portal_shops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_shops
    ADD CONSTRAINT portal_shops_pkey PRIMARY KEY (id);


--
-- Name: portal_shops portal_shops_portal_id_shop_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_shops
    ADD CONSTRAINT portal_shops_portal_id_shop_name_key UNIQUE (portal_id, shop_name);


--
-- Name: price_portals price_portals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_portals
    ADD CONSTRAINT price_portals_pkey PRIMARY KEY (id);


--
-- Name: price_portals price_portals_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_portals
    ADD CONSTRAINT price_portals_slug_key UNIQUE (slug);


--
-- Name: product_attributes product_attributes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_attributes
    ADD CONSTRAINT product_attributes_pkey PRIMARY KEY (id);


--
-- Name: product_attributes product_attributes_product_id_attribute_id_attribute_value__key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_attributes
    ADD CONSTRAINT product_attributes_product_id_attribute_id_attribute_value__key UNIQUE (product_id, attribute_id, attribute_value_id);


--
-- Name: product_categories product_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_categories
    ADD CONSTRAINT product_categories_pkey PRIMARY KEY (id);


--
-- Name: product_categories product_categories_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_categories
    ADD CONSTRAINT product_categories_slug_key UNIQUE (slug);


--
-- Name: product_images product_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_images
    ADD CONSTRAINT product_images_pkey PRIMARY KEY (id);


--
-- Name: product_images product_images_product_id_image_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_images
    ADD CONSTRAINT product_images_product_id_image_id_key UNIQUE (product_id, image_id);


--
-- Name: product_ingredients product_ingredients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_ingredients
    ADD CONSTRAINT product_ingredients_pkey PRIMARY KEY (id);


--
-- Name: product_ingredients product_ingredients_product_id_ingredient_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_ingredients
    ADD CONSTRAINT product_ingredients_product_id_ingredient_id_key UNIQUE (product_id, ingredient_id);


--
-- Name: product_matches product_matches_canonical_product_id_product_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_matches
    ADD CONSTRAINT product_matches_canonical_product_id_product_id_key UNIQUE (canonical_product_id, product_id);


--
-- Name: product_matches product_matches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_matches
    ADD CONSTRAINT product_matches_pkey PRIMARY KEY (id);


--
-- Name: product_merge_queue product_merge_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_merge_queue
    ADD CONSTRAINT product_merge_queue_pkey PRIMARY KEY (id);


--
-- Name: product_merge_queue product_merge_queue_product_a_id_product_b_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_merge_queue
    ADD CONSTRAINT product_merge_queue_product_a_id_product_b_id_key UNIQUE (product_a_id, product_b_id);


--
-- Name: product_offers product_offers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_offers
    ADD CONSTRAINT product_offers_pkey PRIMARY KEY (id);


--
-- Name: product_offers product_offers_shop_id_external_sku_country_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_offers
    ADD CONSTRAINT product_offers_shop_id_external_sku_country_id_key UNIQUE (shop_id, external_sku, country_id);


--
-- Name: product_offers product_offers_shop_id_product_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_offers
    ADD CONSTRAINT product_offers_shop_id_product_url_key UNIQUE (shop_id, product_url);


--
-- Name: product_review_sources product_review_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_review_sources
    ADD CONSTRAINT product_review_sources_pkey PRIMARY KEY (id);


--
-- Name: product_review_sources product_review_sources_product_id_review_site_id_external_p_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_review_sources
    ADD CONSTRAINT product_review_sources_product_id_review_site_id_external_p_key UNIQUE (product_id, review_site_id, external_product_id);


--
-- Name: product_shop_categories product_shop_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_shop_categories
    ADD CONSTRAINT product_shop_categories_pkey PRIMARY KEY (id);


--
-- Name: product_shop_categories product_shop_categories_product_id_shop_category_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_shop_categories
    ADD CONSTRAINT product_shop_categories_product_id_shop_category_id_key UNIQUE (product_id, shop_category_id);


--
-- Name: product_url_queue product_url_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_url_queue
    ADD CONSTRAINT product_url_queue_pkey PRIMARY KEY (id);


--
-- Name: product_url_queue product_url_queue_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_url_queue
    ADD CONSTRAINT product_url_queue_url_key UNIQUE (url);


--
-- Name: products_legacy products_competitor_id_ean_country_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products_legacy
    ADD CONSTRAINT products_competitor_id_ean_country_id_key UNIQUE (competitor_id, ean, country_id);


--
-- Name: products_legacy products_competitor_id_sku_country_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products_legacy
    ADD CONSTRAINT products_competitor_id_sku_country_id_key UNIQUE (competitor_id, sku, country_id);


--
-- Name: products_legacy products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products_legacy
    ADD CONSTRAINT products_pkey PRIMARY KEY (id);


--
-- Name: products products_pkey1; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_pkey1 PRIMARY KEY (id);


--
-- Name: review_queue review_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_queue
    ADD CONSTRAINT review_queue_pkey PRIMARY KEY (id);


--
-- Name: review_sites review_sites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_sites
    ADD CONSTRAINT review_sites_pkey PRIMARY KEY (id);


--
-- Name: review_sites review_sites_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_sites
    ADD CONSTRAINT review_sites_slug_key UNIQUE (slug);


--
-- Name: reviews reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_pkey PRIMARY KEY (id);


--
-- Name: scrape_jobs scrape_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_jobs
    ADD CONSTRAINT scrape_jobs_pkey PRIMARY KEY (id);


--
-- Name: scrape_run_metrics scrape_run_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_run_metrics
    ADD CONSTRAINT scrape_run_metrics_pkey PRIMARY KEY (id);


--
-- Name: seo_keywords seo_keywords_keyword_hreflang_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_keywords
    ADD CONSTRAINT seo_keywords_keyword_hreflang_code_key UNIQUE (keyword, hreflang_code);


--
-- Name: seo_keywords seo_keywords_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_keywords
    ADD CONSTRAINT seo_keywords_pkey PRIMARY KEY (id);


--
-- Name: seo_landing_pages seo_landing_pages_canonical_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_canonical_url_key UNIQUE (canonical_url);


--
-- Name: seo_landing_pages seo_landing_pages_hreflang_code_page_type_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_hreflang_code_page_type_slug_key UNIQUE (hreflang_code, page_type, slug);


--
-- Name: seo_landing_pages seo_landing_pages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_pkey PRIMARY KEY (id);


--
-- Name: seo_metrics seo_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_metrics
    ADD CONSTRAINT seo_metrics_pkey PRIMARY KEY (id);


--
-- Name: seo_url_patterns seo_url_patterns_page_type_hreflang_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_url_patterns
    ADD CONSTRAINT seo_url_patterns_page_type_hreflang_code_key UNIQUE (page_type, hreflang_code);


--
-- Name: seo_url_patterns seo_url_patterns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_url_patterns
    ADD CONSTRAINT seo_url_patterns_pkey PRIMARY KEY (id);


--
-- Name: shipping_carriers shipping_carriers_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shipping_carriers
    ADD CONSTRAINT shipping_carriers_code_key UNIQUE (code);


--
-- Name: shipping_carriers shipping_carriers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shipping_carriers
    ADD CONSTRAINT shipping_carriers_pkey PRIMARY KEY (id);


--
-- Name: shop_categories shop_categories_competitor_id_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_categories
    ADD CONSTRAINT shop_categories_competitor_id_url_key UNIQUE (competitor_id, url);


--
-- Name: shop_categories shop_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_categories
    ADD CONSTRAINT shop_categories_pkey PRIMARY KEY (id);


--
-- Name: shop_filters shop_filters_competitor_id_shop_category_id_filter_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_filters
    ADD CONSTRAINT shop_filters_competitor_id_shop_category_id_filter_name_key UNIQUE (competitor_id, shop_category_id, filter_name);


--
-- Name: shop_filters shop_filters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_filters
    ADD CONSTRAINT shop_filters_pkey PRIMARY KEY (id);


--
-- Name: shop_groups shop_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_groups
    ADD CONSTRAINT shop_groups_pkey PRIMARY KEY (id);


--
-- Name: shop_groups shop_groups_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_groups
    ADD CONSTRAINT shop_groups_slug_key UNIQUE (slug);


--
-- Name: shop_loyalty_programs shop_loyalty_programs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_loyalty_programs
    ADD CONSTRAINT shop_loyalty_programs_pkey PRIMARY KEY (id);


--
-- Name: shop_payment_methods shop_payment_methods_competitor_id_country_id_method_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_payment_methods
    ADD CONSTRAINT shop_payment_methods_competitor_id_country_id_method_key UNIQUE (competitor_id, country_id, method);


--
-- Name: shop_payment_methods shop_payment_methods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_payment_methods
    ADD CONSTRAINT shop_payment_methods_pkey PRIMARY KEY (id);


--
-- Name: shop_profiles shop_profiles_competitor_id_country_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_profiles
    ADD CONSTRAINT shop_profiles_competitor_id_country_id_key UNIQUE (competitor_id, country_id);


--
-- Name: shop_profiles shop_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_profiles
    ADD CONSTRAINT shop_profiles_pkey PRIMARY KEY (id);


--
-- Name: shop_returns_policies shop_returns_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_returns_policies
    ADD CONSTRAINT shop_returns_policies_pkey PRIMARY KEY (id);


--
-- Name: shop_shipping_options shop_shipping_options_competitor_id_country_id_carrier_id_s_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_shipping_options
    ADD CONSTRAINT shop_shipping_options_competitor_id_country_id_carrier_id_s_key UNIQUE (competitor_id, country_id, carrier_id, service_name);


--
-- Name: shop_shipping_options shop_shipping_options_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_shipping_options
    ADD CONSTRAINT shop_shipping_options_pkey PRIMARY KEY (id);


--
-- Name: shop_usps shop_usps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_usps
    ADD CONSTRAINT shop_usps_pkey PRIMARY KEY (id);


--
-- Name: sitemap_crawl_log sitemap_crawl_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sitemap_crawl_log
    ADD CONSTRAINT sitemap_crawl_log_pkey PRIMARY KEY (id);


--
-- Name: translations translations_entity_type_entity_id_language_id_field_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.translations
    ADD CONSTRAINT translations_entity_type_entity_id_language_id_field_name_key UNIQUE (entity_type, entity_id, language_id, field_name);


--
-- Name: translations translations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.translations
    ADD CONSTRAINT translations_pkey PRIMARY KEY (id);


--
-- Name: variant_mappings variant_mappings_pattern_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.variant_mappings
    ADD CONSTRAINT variant_mappings_pattern_key UNIQUE (pattern);


--
-- Name: variant_mappings variant_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.variant_mappings
    ADD CONSTRAINT variant_mappings_pkey PRIMARY KEY (id);


--
-- Name: volume_conversions volume_conversions_pattern_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.volume_conversions
    ADD CONSTRAINT volume_conversions_pattern_key UNIQUE (pattern);


--
-- Name: volume_conversions volume_conversions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.volume_conversions
    ADD CONSTRAINT volume_conversions_pkey PRIMARY KEY (id);


--
-- Name: website_checks website_checks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.website_checks
    ADD CONSTRAINT website_checks_pkey PRIMARY KEY (id);


--
-- Name: idx_aff_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aff_competitor ON public.affiliate_programs USING btree (competitor_id);


--
-- Name: idx_aff_has_program; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aff_has_program ON public.affiliate_programs USING btree (has_program);


--
-- Name: idx_aff_network; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aff_network ON public.affiliate_programs USING btree (network);


--
-- Name: idx_asl_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asl_type ON public.ai_sweep_log USING btree (sweep_type, started_at DESC);


--
-- Name: idx_attr_values_attr; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attr_values_attr ON public.attribute_values USING btree (attribute_id);


--
-- Name: idx_attributes_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attributes_code ON public.attributes USING btree (code);


--
-- Name: idx_attributes_filterable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attributes_filterable ON public.attributes USING btree (is_filterable);


--
-- Name: idx_bi_brand; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bi_brand ON public.brand_images USING btree (brand_id);


--
-- Name: idx_bpa_brand; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bpa_brand ON public.brand_protection_alerts USING btree (brand_id, detected_at DESC);


--
-- Name: idx_bpa_severity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bpa_severity ON public.brand_protection_alerts USING btree (severity, status);


--
-- Name: idx_bpa_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bpa_type ON public.brand_protection_alerts USING btree (alert_type);


--
-- Name: idx_brand_alias_brand; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brand_alias_brand ON public.brand_aliases USING btree (brand_id);


--
-- Name: idx_brand_alias_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brand_alias_norm ON public.brand_aliases USING btree (alias_normalized);


--
-- Name: idx_brands_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brands_category ON public.brands USING btree (category);


--
-- Name: idx_brands_normalized; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_brands_normalized ON public.brands USING btree (name_normalized);


--
-- Name: idx_brands_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brands_parent ON public.brands USING btree (parent_company);


--
-- Name: idx_brands_protection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brands_protection ON public.brands USING btree (protection_active);


--
-- Name: idx_brands_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_brands_slug ON public.brands USING btree (slug);


--
-- Name: idx_bs_campaign; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bs_campaign ON public.banner_snapshots USING btree (campaign_id);


--
-- Name: idx_bs_competitor_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bs_competitor_date ON public.banner_snapshots USING btree (competitor_id, captured_at DESC);


--
-- Name: idx_cat_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cat_parent ON public.product_categories USING btree (parent_id);


--
-- Name: idx_cat_path; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cat_path ON public.product_categories USING btree (path);


--
-- Name: idx_catmap_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_catmap_competitor ON public.category_mappings USING btree (competitor_id);


--
-- Name: idx_catmap_source_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_catmap_source_norm ON public.category_mappings USING btree (source_category_normalized);


--
-- Name: idx_catmap_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_catmap_target ON public.category_mappings USING btree (target_category_id);


--
-- Name: idx_cl_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cl_country ON public.country_languages USING btree (country_id);


--
-- Name: idx_cl_hreflang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cl_hreflang ON public.country_languages USING btree (hreflang_code);


--
-- Name: idx_cl_lang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cl_lang ON public.country_languages USING btree (language_id);


--
-- Name: idx_competitors_country_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_competitors_country_id ON public.competitors USING btree (country_id);


--
-- Name: idx_competitors_shop_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_competitors_shop_group ON public.competitors USING btree (shop_group_id);


--
-- Name: idx_countries_iso2; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_countries_iso2 ON public.countries USING btree (iso2);


--
-- Name: idx_countries_region; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_countries_region ON public.countries USING btree (region);


--
-- Name: idx_coverage_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coverage_country ON public.competitor_coverage USING btree (country);


--
-- Name: idx_coverage_country_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coverage_country_id ON public.competitor_coverage USING btree (country_id);


--
-- Name: idx_coverage_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coverage_domain ON public.competitor_coverage USING btree (domain);


--
-- Name: idx_coverage_paired; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coverage_paired ON public.competitor_coverage USING btree (paired_items DESC);


--
-- Name: idx_crawl_log_shop; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_crawl_log_shop ON public.sitemap_crawl_log USING btree (shop, crawled_at DESC);


--
-- Name: idx_cts_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cts_competitor ON public.category_tree_snapshots USING btree (competitor_id, snapshot_at DESC);


--
-- Name: idx_dqs_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dqs_date ON public.data_quality_snapshots USING btree (snapshot_date DESC);


--
-- Name: idx_es_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_es_country ON public.extracted_shops USING btree (country);


--
-- Name: idx_es_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_es_domain ON public.extracted_shops USING btree (domain);


--
-- Name: idx_es_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_es_source ON public.extracted_shops USING btree (source_portal);


--
-- Name: idx_exr_currency; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exr_currency ON public.exchange_rates USING btree (target_currency, rate_date DESC);


--
-- Name: idx_exr_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exr_date ON public.exchange_rates USING btree (rate_date DESC);


--
-- Name: idx_filters_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_filters_category ON public.shop_filters USING btree (shop_category_id);


--
-- Name: idx_filters_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_filters_competitor ON public.shop_filters USING btree (competitor_id);


--
-- Name: idx_filters_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_filters_name ON public.shop_filters USING btree (filter_name_normalized);


--
-- Name: idx_findings_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_findings_competitor ON public.findings USING btree (competitor_id);


--
-- Name: idx_findings_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_findings_type ON public.findings USING btree (data_type);


--
-- Name: idx_gtax_beauty; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gtax_beauty ON public.google_taxonomy USING btree (is_beauty) WHERE (is_beauty = true);


--
-- Name: idx_gtax_mapped; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gtax_mapped ON public.google_taxonomy USING btree (mapped_category_id);


--
-- Name: idx_gtax_name_en; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gtax_name_en ON public.google_taxonomy USING gin (to_tsvector('english'::regconfig, name_en));


--
-- Name: idx_hlidac_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hlidac_fetched ON public.hlidac_shopu_data USING btree (fetched_at DESC);


--
-- Name: idx_hlidac_url; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_hlidac_url ON public.hlidac_shopu_data USING btree (product_url);


--
-- Name: idx_images_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_images_hash ON public.images USING btree (hash_sha256);


--
-- Name: idx_images_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_images_source ON public.images USING btree (source_url);


--
-- Name: idx_images_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_images_status ON public.images USING btree (status);


--
-- Name: idx_keywords_brand; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keywords_brand ON public.seo_keywords USING btree (related_brand_id);


--
-- Name: idx_keywords_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keywords_category ON public.seo_keywords USING btree (related_category_id);


--
-- Name: idx_keywords_hreflang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keywords_hreflang ON public.seo_keywords USING btree (hreflang_code);


--
-- Name: idx_keywords_intent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keywords_intent ON public.seo_keywords USING btree (intent);


--
-- Name: idx_keywords_normalized; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keywords_normalized ON public.seo_keywords USING btree (keyword_normalized);


--
-- Name: idx_keywords_volume; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keywords_volume ON public.seo_keywords USING btree (search_volume DESC);


--
-- Name: idx_languages_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_languages_code ON public.languages USING btree (code);


--
-- Name: idx_mc_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mc_active ON public.marketing_campaigns USING btree (is_active);


--
-- Name: idx_mc_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mc_competitor ON public.marketing_campaigns USING btree (competitor_id);


--
-- Name: idx_mc_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mc_country ON public.marketing_campaigns USING btree (country_id);


--
-- Name: idx_mc_dates; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mc_dates ON public.marketing_campaigns USING btree (start_date, end_date);


--
-- Name: idx_mc_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mc_type ON public.marketing_campaigns USING btree (campaign_type);


--
-- Name: idx_mce_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mce_competitor ON public.marketing_calendar_events USING btree (competitor_id, event_date);


--
-- Name: idx_mce_year; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mce_year ON public.marketing_calendar_events USING btree (year, event_type);


--
-- Name: idx_norm_canonical; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_norm_canonical ON public.normalization_rules USING btree (canonical);


--
-- Name: idx_norm_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_norm_entity ON public.normalization_rules USING btree (entity_type);


--
-- Name: idx_norm_pattern; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_norm_pattern ON public.normalization_rules USING btree (pattern_normalized);


--
-- Name: idx_norm_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_norm_priority ON public.normalization_rules USING btree (entity_type, priority DESC);


--
-- Name: idx_pa_attribute; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pa_attribute ON public.product_attributes USING btree (attribute_id);


--
-- Name: idx_pa_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pa_product ON public.product_attributes USING btree (product_id);


--
-- Name: idx_pa_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pa_value ON public.product_attributes USING btree (attribute_value_id);


--
-- Name: idx_pb_brand; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pb_brand ON public.portal_brands USING btree (brand_name);


--
-- Name: idx_pb_portal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pb_portal ON public.portal_brands USING btree (portal_id);


--
-- Name: idx_pi_ean; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pi_ean ON public.product_ingredients USING btree (ean);


--
-- Name: idx_pi_image; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pi_image ON public.product_images USING btree (image_id);


--
-- Name: idx_pi_ingredient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pi_ingredient ON public.product_ingredients USING btree (ingredient_id);


--
-- Name: idx_pi_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pi_product ON public.product_ingredients USING btree (product_id);


--
-- Name: idx_pm_canonical; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_canonical ON public.product_matches USING btree (canonical_product_id);


--
-- Name: idx_pm_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_product ON public.product_matches USING btree (product_id);


--
-- Name: idx_pmq_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pmq_score ON public.product_merge_queue USING btree (match_score DESC);


--
-- Name: idx_pmq_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pmq_status ON public.product_merge_queue USING btree (status);


--
-- Name: idx_portal_cat_mapped; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_portal_cat_mapped ON public.portal_categories USING btree (mapped_category_id);


--
-- Name: idx_portal_cat_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_portal_cat_parent ON public.portal_categories USING btree (parent_id);


--
-- Name: idx_portal_cat_path; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_portal_cat_path ON public.portal_categories USING btree (path);


--
-- Name: idx_portal_cat_portal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_portal_cat_portal ON public.portal_categories USING btree (portal_id);


--
-- Name: idx_portal_cat_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_portal_cat_priority ON public.portal_categories USING btree (scrape_priority) WHERE (is_beauty_relevant = true);


--
-- Name: idx_portals_countries; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_portals_countries ON public.price_portals USING gin (countries);


--
-- Name: idx_prod_brand; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_brand ON public.products_legacy USING btree (brand_id);


--
-- Name: idx_prod_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_category ON public.products_legacy USING btree (category_id);


--
-- Name: idx_prod_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_competitor ON public.products_legacy USING btree (competitor_id);


--
-- Name: idx_prod_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_country ON public.products_legacy USING btree (country_id);


--
-- Name: idx_prod_ean; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_ean ON public.products_legacy USING btree (ean);


--
-- Name: idx_prod_in_stock; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_in_stock ON public.products_legacy USING btree (in_stock);


--
-- Name: idx_prod_name_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_name_norm ON public.products_legacy USING btree (name_normalized);


--
-- Name: idx_prod_price; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_price ON public.products_legacy USING btree (price);


--
-- Name: idx_prod_scraped; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_scraped ON public.products_legacy USING btree (scraped_at);


--
-- Name: idx_products_scraped_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_products_scraped_at ON public.products_legacy USING btree (scraped_at DESC);


--
-- Name: idx_prs_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prs_product ON public.product_review_sources USING btree (product_id);


--
-- Name: idx_prs_site; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prs_site ON public.product_review_sources USING btree (review_site_id);


--
-- Name: idx_ps_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ps_domain ON public.portal_shops USING btree (shop_domain);


--
-- Name: idx_ps_matched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ps_matched ON public.portal_shops USING btree (matched_competitor_id);


--
-- Name: idx_ps_portal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ps_portal ON public.portal_shops USING btree (portal_id);


--
-- Name: idx_psc_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_psc_category ON public.product_shop_categories USING btree (shop_category_id);


--
-- Name: idx_psc_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_psc_product ON public.product_shop_categories USING btree (product_id);


--
-- Name: idx_queue_shop_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_queue_shop_status ON public.product_url_queue USING btree (shop, status);


--
-- Name: idx_reviews_ean; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reviews_ean ON public.reviews USING btree (ean);


--
-- Name: idx_reviews_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reviews_product ON public.reviews USING btree (product_id);


--
-- Name: idx_rq_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rq_created ON public.review_queue USING btree (created_at DESC);


--
-- Name: idx_rq_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rq_entity ON public.review_queue USING btree (entity_type, entity_id);


--
-- Name: idx_rq_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rq_status ON public.review_queue USING btree (status, priority);


--
-- Name: idx_rq_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rq_type ON public.review_queue USING btree (issue_type);


--
-- Name: idx_seo_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_seo_competitor ON public.seo_metrics USING btree (competitor_id);


--
-- Name: idx_shop_cat_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shop_cat_competitor ON public.shop_categories USING btree (competitor_id);


--
-- Name: idx_shop_cat_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shop_cat_country ON public.shop_categories USING btree (country_id);


--
-- Name: idx_shop_cat_mapped; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shop_cat_mapped ON public.shop_categories USING btree (mapped_category_id);


--
-- Name: idx_shop_cat_name_norm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shop_cat_name_norm ON public.shop_categories USING btree (name_normalized);


--
-- Name: idx_shop_cat_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shop_cat_parent ON public.shop_categories USING btree (parent_id);


--
-- Name: idx_shop_cat_path; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shop_cat_path ON public.shop_categories USING btree (path);


--
-- Name: idx_shop_groups_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shop_groups_slug ON public.shop_groups USING btree (slug);


--
-- Name: idx_shop_groups_vertical; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shop_groups_vertical ON public.shop_groups USING btree (vertical);


--
-- Name: idx_sj_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sj_competitor ON public.scrape_jobs USING btree (competitor_id);


--
-- Name: idx_sj_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sj_created ON public.scrape_jobs USING btree (created_at);


--
-- Name: idx_sj_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sj_status ON public.scrape_jobs USING btree (status);


--
-- Name: idx_slp_brand; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slp_brand ON public.seo_landing_pages USING btree (brand_id);


--
-- Name: idx_slp_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slp_category ON public.seo_landing_pages USING btree (category_id);


--
-- Name: idx_slp_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slp_country ON public.seo_landing_pages USING btree (country_id);


--
-- Name: idx_slp_hreflang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slp_hreflang ON public.seo_landing_pages USING btree (hreflang_code, page_type, published);


--
-- Name: idx_slp_language; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slp_language ON public.seo_landing_pages USING btree (language_id);


--
-- Name: idx_slp_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slp_type ON public.seo_landing_pages USING btree (page_type, published);


--
-- Name: idx_sp_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sp_competitor ON public.shop_profiles USING btree (competitor_id);


--
-- Name: idx_sp_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sp_country ON public.shop_profiles USING btree (country_id);


--
-- Name: idx_sp_trusted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sp_trusted ON public.shop_profiles USING btree (trusted);


--
-- Name: idx_spm_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_spm_competitor ON public.shop_payment_methods USING btree (competitor_id);


--
-- Name: idx_srm_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_srm_run ON public.scrape_run_metrics USING btree (run_id);


--
-- Name: idx_srm_scraper; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_srm_scraper ON public.scrape_run_metrics USING btree (scraper, started_at DESC);


--
-- Name: idx_sso_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sso_competitor ON public.shop_shipping_options USING btree (competitor_id);


--
-- Name: idx_sso_country; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sso_country ON public.shop_shipping_options USING btree (country_id);


--
-- Name: idx_sso_free; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sso_free ON public.shop_shipping_options USING btree (free_shipping_threshold);


--
-- Name: idx_trans_language; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trans_language ON public.translations USING btree (language_id);


--
-- Name: idx_trans_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trans_lookup ON public.translations USING btree (entity_type, entity_id, language_id);


--
-- Name: idx_trans_reviewed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trans_reviewed ON public.translations USING btree (reviewed);


--
-- Name: idx_usps_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usps_competitor ON public.shop_usps USING btree (competitor_id);


--
-- Name: idx_usps_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usps_type ON public.shop_usps USING btree (usp_type);


--
-- Name: idx_wc_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wc_domain ON public.website_checks USING btree (domain);


--
-- Name: idx_wc_entity_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wc_entity_type ON public.website_checks USING btree (entity_type);


--
-- Name: idx_wc_firecrawl; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wc_firecrawl ON public.website_checks USING btree (firecrawl_status);


--
-- Name: idx_wc_portal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wc_portal ON public.website_checks USING btree (portal_id);


--
-- Name: idx_wc_protection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wc_protection ON public.website_checks USING btree (protection);


--
-- Name: price_history_offer_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX price_history_offer_idx ON ONLY public.offer_price_history USING btree (offer_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_04_offer_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_04_offer_id_recorded_at_idx ON public.offer_price_history_2026_04 USING btree (offer_id, recorded_at DESC);


--
-- Name: price_history_product_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX price_history_product_idx ON ONLY public.offer_price_history USING btree (product_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_04_product_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_04_product_id_recorded_at_idx ON public.offer_price_history_2026_04 USING btree (product_id, recorded_at DESC);


--
-- Name: price_history_shop_country_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX price_history_shop_country_idx ON ONLY public.offer_price_history USING btree (shop_id, country_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_04_shop_id_country_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_04_shop_id_country_id_recorded_at_idx ON public.offer_price_history_2026_04 USING btree (shop_id, country_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_05_offer_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_05_offer_id_recorded_at_idx ON public.offer_price_history_2026_05 USING btree (offer_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_05_product_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_05_product_id_recorded_at_idx ON public.offer_price_history_2026_05 USING btree (product_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_05_shop_id_country_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_05_shop_id_country_id_recorded_at_idx ON public.offer_price_history_2026_05 USING btree (shop_id, country_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_06_offer_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_06_offer_id_recorded_at_idx ON public.offer_price_history_2026_06 USING btree (offer_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_06_product_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_06_product_id_recorded_at_idx ON public.offer_price_history_2026_06 USING btree (product_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_06_shop_id_country_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_06_shop_id_country_id_recorded_at_idx ON public.offer_price_history_2026_06 USING btree (shop_id, country_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_07_offer_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_07_offer_id_recorded_at_idx ON public.offer_price_history_2026_07 USING btree (offer_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_07_product_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_07_product_id_recorded_at_idx ON public.offer_price_history_2026_07 USING btree (product_id, recorded_at DESC);


--
-- Name: offer_price_history_2026_07_shop_id_country_id_recorded_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offer_price_history_2026_07_shop_id_country_id_recorded_at_idx ON public.offer_price_history_2026_07 USING btree (shop_id, country_id, recorded_at DESC);


--
-- Name: offers_availability_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offers_availability_idx ON public.product_offers USING btree (availability);


--
-- Name: offers_country_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offers_country_idx ON public.product_offers USING btree (country_id);


--
-- Name: offers_last_seen_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offers_last_seen_idx ON public.product_offers USING btree (last_seen_at);


--
-- Name: offers_product_country_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offers_product_country_idx ON public.product_offers USING btree (product_id, country_id);


--
-- Name: offers_product_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offers_product_idx ON public.product_offers USING btree (product_id);


--
-- Name: offers_product_price_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offers_product_price_idx ON public.product_offers USING btree (product_id, effective_price) WHERE ((is_active = true) AND (availability = 'in_stock'::public.availability_status));


--
-- Name: offers_raw_data_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offers_raw_data_gin ON public.product_offers USING gin (raw_offer_data jsonb_path_ops);


--
-- Name: offers_shop_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX offers_shop_idx ON public.product_offers USING btree (shop_id);


--
-- Name: products_brand_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_brand_category_idx ON public.products USING btree (brand_id, category_id);


--
-- Name: products_brand_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_brand_idx ON public.products USING btree (brand_id);


--
-- Name: products_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_category_idx ON public.products USING btree (category_id);


--
-- Name: products_gtin12_uk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX products_gtin12_uk ON public.products USING btree (gtin12) WHERE (gtin12 IS NOT NULL);


--
-- Name: products_gtin13_uk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX products_gtin13_uk ON public.products USING btree (gtin13) WHERE (gtin13 IS NOT NULL);


--
-- Name: products_item_group_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_item_group_idx ON public.products USING btree (item_group_id) WHERE (item_group_id IS NOT NULL);


--
-- Name: products_mpn_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_mpn_idx ON public.products USING btree (mpn) WHERE (mpn IS NOT NULL);


--
-- Name: products_title_trgm_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX products_title_trgm_idx ON public.products USING gin (title_normalized public.gin_trgm_ops);


--
-- Name: offer_price_history_2026_04_offer_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_offer_idx ATTACH PARTITION public.offer_price_history_2026_04_offer_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_04_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.offer_price_history_pkey ATTACH PARTITION public.offer_price_history_2026_04_pkey;


--
-- Name: offer_price_history_2026_04_product_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_product_idx ATTACH PARTITION public.offer_price_history_2026_04_product_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_04_shop_id_country_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_shop_country_idx ATTACH PARTITION public.offer_price_history_2026_04_shop_id_country_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_05_offer_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_offer_idx ATTACH PARTITION public.offer_price_history_2026_05_offer_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_05_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.offer_price_history_pkey ATTACH PARTITION public.offer_price_history_2026_05_pkey;


--
-- Name: offer_price_history_2026_05_product_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_product_idx ATTACH PARTITION public.offer_price_history_2026_05_product_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_05_shop_id_country_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_shop_country_idx ATTACH PARTITION public.offer_price_history_2026_05_shop_id_country_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_06_offer_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_offer_idx ATTACH PARTITION public.offer_price_history_2026_06_offer_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_06_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.offer_price_history_pkey ATTACH PARTITION public.offer_price_history_2026_06_pkey;


--
-- Name: offer_price_history_2026_06_product_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_product_idx ATTACH PARTITION public.offer_price_history_2026_06_product_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_06_shop_id_country_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_shop_country_idx ATTACH PARTITION public.offer_price_history_2026_06_shop_id_country_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_07_offer_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_offer_idx ATTACH PARTITION public.offer_price_history_2026_07_offer_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_07_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.offer_price_history_pkey ATTACH PARTITION public.offer_price_history_2026_07_pkey;


--
-- Name: offer_price_history_2026_07_product_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_product_idx ATTACH PARTITION public.offer_price_history_2026_07_product_id_recorded_at_idx;


--
-- Name: offer_price_history_2026_07_shop_id_country_id_recorded_at_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.price_history_shop_country_idx ATTACH PARTITION public.offer_price_history_2026_07_shop_id_country_id_recorded_at_idx;


--
-- Name: affiliate_programs affiliate_programs_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.affiliate_programs
    ADD CONSTRAINT affiliate_programs_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: attribute_values attribute_values_attribute_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attribute_values
    ADD CONSTRAINT attribute_values_attribute_id_fkey FOREIGN KEY (attribute_id) REFERENCES public.attributes(id);


--
-- Name: banner_snapshots banner_snapshots_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banner_snapshots
    ADD CONSTRAINT banner_snapshots_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.marketing_campaigns(id);


--
-- Name: banner_snapshots banner_snapshots_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banner_snapshots
    ADD CONSTRAINT banner_snapshots_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: banner_snapshots banner_snapshots_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.banner_snapshots
    ADD CONSTRAINT banner_snapshots_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: brand_aliases brand_aliases_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_aliases
    ADD CONSTRAINT brand_aliases_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id) ON DELETE CASCADE;


--
-- Name: brand_images brand_images_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_images
    ADD CONSTRAINT brand_images_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id);


--
-- Name: brand_images brand_images_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_images
    ADD CONSTRAINT brand_images_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.images(id);


--
-- Name: brand_protection_alerts brand_protection_alerts_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_protection_alerts
    ADD CONSTRAINT brand_protection_alerts_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id);


--
-- Name: brand_protection_alerts brand_protection_alerts_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_protection_alerts
    ADD CONSTRAINT brand_protection_alerts_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: brand_protection_alerts brand_protection_alerts_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_protection_alerts
    ADD CONSTRAINT brand_protection_alerts_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: brand_subscriptions brand_subscriptions_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_subscriptions
    ADD CONSTRAINT brand_subscriptions_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id);


--
-- Name: brands brands_origin_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brands
    ADD CONSTRAINT brands_origin_country_id_fkey FOREIGN KEY (origin_country_id) REFERENCES public.countries(id);


--
-- Name: category_mappings category_mappings_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_mappings
    ADD CONSTRAINT category_mappings_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: category_mappings category_mappings_shop_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_mappings
    ADD CONSTRAINT category_mappings_shop_group_id_fkey FOREIGN KEY (shop_group_id) REFERENCES public.shop_groups(id);


--
-- Name: category_mappings category_mappings_target_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_mappings
    ADD CONSTRAINT category_mappings_target_category_id_fkey FOREIGN KEY (target_category_id) REFERENCES public.product_categories(id);


--
-- Name: category_tree_snapshots category_tree_snapshots_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_tree_snapshots
    ADD CONSTRAINT category_tree_snapshots_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: category_tree_snapshots category_tree_snapshots_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.category_tree_snapshots
    ADD CONSTRAINT category_tree_snapshots_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: competitor_coverage competitor_coverage_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_coverage
    ADD CONSTRAINT competitor_coverage_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: competitors competitors_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitors
    ADD CONSTRAINT competitors_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: competitors competitors_shop_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitors
    ADD CONSTRAINT competitors_shop_group_id_fkey FOREIGN KEY (shop_group_id) REFERENCES public.shop_groups(id);


--
-- Name: country_languages country_languages_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.country_languages
    ADD CONSTRAINT country_languages_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: country_languages country_languages_language_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.country_languages
    ADD CONSTRAINT country_languages_language_id_fkey FOREIGN KEY (language_id) REFERENCES public.languages(id);


--
-- Name: extracted_shops extracted_shops_matched_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extracted_shops
    ADD CONSTRAINT extracted_shops_matched_competitor_id_fkey FOREIGN KEY (matched_competitor_id) REFERENCES public.competitors(id);


--
-- Name: findings findings_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.findings
    ADD CONSTRAINT findings_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: google_taxonomy google_taxonomy_mapped_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_taxonomy
    ADD CONSTRAINT google_taxonomy_mapped_category_id_fkey FOREIGN KEY (mapped_category_id) REFERENCES public.product_categories(id);


--
-- Name: images images_source_shop_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.images
    ADD CONSTRAINT images_source_shop_id_fkey FOREIGN KEY (source_shop_id) REFERENCES public.competitors(id);


--
-- Name: languages languages_primary_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.languages
    ADD CONSTRAINT languages_primary_country_id_fkey FOREIGN KEY (primary_country_id) REFERENCES public.countries(id);


--
-- Name: marketing_calendar_events marketing_calendar_events_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_calendar_events
    ADD CONSTRAINT marketing_calendar_events_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.marketing_campaigns(id);


--
-- Name: marketing_calendar_events marketing_calendar_events_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_calendar_events
    ADD CONSTRAINT marketing_calendar_events_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: marketing_calendar_events marketing_calendar_events_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_calendar_events
    ADD CONSTRAINT marketing_calendar_events_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: marketing_campaigns marketing_campaigns_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_campaigns
    ADD CONSTRAINT marketing_campaigns_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: marketing_campaigns marketing_campaigns_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_campaigns
    ADD CONSTRAINT marketing_campaigns_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: marketing_campaigns marketing_campaigns_target_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_campaigns
    ADD CONSTRAINT marketing_campaigns_target_brand_id_fkey FOREIGN KEY (target_brand_id) REFERENCES public.brands(id);


--
-- Name: marketing_campaigns marketing_campaigns_target_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marketing_campaigns
    ADD CONSTRAINT marketing_campaigns_target_category_id_fkey FOREIGN KEY (target_category_id) REFERENCES public.product_categories(id);


--
-- Name: normalization_rules normalization_rules_shop_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.normalization_rules
    ADD CONSTRAINT normalization_rules_shop_group_id_fkey FOREIGN KEY (shop_group_id) REFERENCES public.shop_groups(id);


--
-- Name: portal_brands portal_brands_mapped_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_brands
    ADD CONSTRAINT portal_brands_mapped_brand_id_fkey FOREIGN KEY (mapped_brand_id) REFERENCES public.brands(id);


--
-- Name: portal_brands portal_brands_portal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_brands
    ADD CONSTRAINT portal_brands_portal_id_fkey FOREIGN KEY (portal_id) REFERENCES public.price_portals(id);


--
-- Name: portal_categories portal_categories_mapped_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_categories
    ADD CONSTRAINT portal_categories_mapped_category_id_fkey FOREIGN KEY (mapped_category_id) REFERENCES public.product_categories(id);


--
-- Name: portal_categories portal_categories_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_categories
    ADD CONSTRAINT portal_categories_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.portal_categories(id);


--
-- Name: portal_categories portal_categories_portal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_categories
    ADD CONSTRAINT portal_categories_portal_id_fkey FOREIGN KEY (portal_id) REFERENCES public.price_portals(id);


--
-- Name: portal_shops portal_shops_matched_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_shops
    ADD CONSTRAINT portal_shops_matched_competitor_id_fkey FOREIGN KEY (matched_competitor_id) REFERENCES public.competitors(id);


--
-- Name: portal_shops portal_shops_portal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.portal_shops
    ADD CONSTRAINT portal_shops_portal_id_fkey FOREIGN KEY (portal_id) REFERENCES public.price_portals(id);


--
-- Name: product_attributes product_attributes_attribute_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_attributes
    ADD CONSTRAINT product_attributes_attribute_id_fkey FOREIGN KEY (attribute_id) REFERENCES public.attributes(id);


--
-- Name: product_attributes product_attributes_attribute_value_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_attributes
    ADD CONSTRAINT product_attributes_attribute_value_id_fkey FOREIGN KEY (attribute_value_id) REFERENCES public.attribute_values(id);


--
-- Name: product_attributes product_attributes_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_attributes
    ADD CONSTRAINT product_attributes_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_categories product_categories_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_categories
    ADD CONSTRAINT product_categories_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.product_categories(id);


--
-- Name: product_images product_images_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_images
    ADD CONSTRAINT product_images_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.images(id);


--
-- Name: product_images product_images_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_images
    ADD CONSTRAINT product_images_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_ingredients product_ingredients_ingredient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_ingredients
    ADD CONSTRAINT product_ingredients_ingredient_id_fkey FOREIGN KEY (ingredient_id) REFERENCES public.ingredients(id);


--
-- Name: product_ingredients product_ingredients_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_ingredients
    ADD CONSTRAINT product_ingredients_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_matches product_matches_canonical_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_matches
    ADD CONSTRAINT product_matches_canonical_product_id_fkey FOREIGN KEY (canonical_product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_matches product_matches_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_matches
    ADD CONSTRAINT product_matches_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_merge_queue product_merge_queue_merged_into_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_merge_queue
    ADD CONSTRAINT product_merge_queue_merged_into_id_fkey FOREIGN KEY (merged_into_id) REFERENCES public.products(id);


--
-- Name: product_merge_queue product_merge_queue_product_a_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_merge_queue
    ADD CONSTRAINT product_merge_queue_product_a_id_fkey FOREIGN KEY (product_a_id) REFERENCES public.products(id);


--
-- Name: product_merge_queue product_merge_queue_product_b_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_merge_queue
    ADD CONSTRAINT product_merge_queue_product_b_id_fkey FOREIGN KEY (product_b_id) REFERENCES public.products(id);


--
-- Name: product_offers product_offers_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_offers
    ADD CONSTRAINT product_offers_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: product_offers product_offers_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_offers
    ADD CONSTRAINT product_offers_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_offers product_offers_shop_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_offers
    ADD CONSTRAINT product_offers_shop_id_fkey FOREIGN KEY (shop_id) REFERENCES public.competitors(id);


--
-- Name: product_review_sources product_review_sources_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_review_sources
    ADD CONSTRAINT product_review_sources_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_review_sources product_review_sources_review_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_review_sources
    ADD CONSTRAINT product_review_sources_review_site_id_fkey FOREIGN KEY (review_site_id) REFERENCES public.review_sites(id);


--
-- Name: product_shop_categories product_shop_categories_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_shop_categories
    ADD CONSTRAINT product_shop_categories_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: product_shop_categories product_shop_categories_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_shop_categories
    ADD CONSTRAINT product_shop_categories_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: product_shop_categories product_shop_categories_shop_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_shop_categories
    ADD CONSTRAINT product_shop_categories_shop_category_id_fkey FOREIGN KEY (shop_category_id) REFERENCES public.shop_categories(id);


--
-- Name: products_legacy products_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products_legacy
    ADD CONSTRAINT products_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id);


--
-- Name: products products_brand_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_brand_id_fkey1 FOREIGN KEY (brand_id) REFERENCES public.brands(id);


--
-- Name: products_legacy products_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products_legacy
    ADD CONSTRAINT products_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.product_categories(id);


--
-- Name: products products_category_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_category_id_fkey1 FOREIGN KEY (category_id) REFERENCES public.product_categories(id);


--
-- Name: products_legacy products_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products_legacy
    ADD CONSTRAINT products_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: products_legacy products_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products_legacy
    ADD CONSTRAINT products_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: products products_first_seen_shop_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.products
    ADD CONSTRAINT products_first_seen_shop_id_fkey FOREIGN KEY (first_seen_shop_id) REFERENCES public.competitors(id);


--
-- Name: reviews reviews_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: reviews reviews_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;


--
-- Name: scrape_jobs scrape_jobs_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_jobs
    ADD CONSTRAINT scrape_jobs_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: seo_keywords seo_keywords_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_keywords
    ADD CONSTRAINT seo_keywords_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: seo_keywords seo_keywords_language_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_keywords
    ADD CONSTRAINT seo_keywords_language_id_fkey FOREIGN KEY (language_id) REFERENCES public.languages(id);


--
-- Name: seo_keywords seo_keywords_related_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_keywords
    ADD CONSTRAINT seo_keywords_related_brand_id_fkey FOREIGN KEY (related_brand_id) REFERENCES public.brands(id);


--
-- Name: seo_keywords seo_keywords_related_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_keywords
    ADD CONSTRAINT seo_keywords_related_category_id_fkey FOREIGN KEY (related_category_id) REFERENCES public.product_categories(id);


--
-- Name: seo_keywords seo_keywords_target_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_keywords
    ADD CONSTRAINT seo_keywords_target_page_id_fkey FOREIGN KEY (target_page_id) REFERENCES public.seo_landing_pages(id);


--
-- Name: seo_landing_pages seo_landing_pages_brand_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id);


--
-- Name: seo_landing_pages seo_landing_pages_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.product_categories(id);


--
-- Name: seo_landing_pages seo_landing_pages_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: seo_landing_pages seo_landing_pages_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: seo_landing_pages seo_landing_pages_language_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_language_id_fkey FOREIGN KEY (language_id) REFERENCES public.languages(id);


--
-- Name: seo_landing_pages seo_landing_pages_shop_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_landing_pages
    ADD CONSTRAINT seo_landing_pages_shop_group_id_fkey FOREIGN KEY (shop_group_id) REFERENCES public.shop_groups(id);


--
-- Name: seo_metrics seo_metrics_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_metrics
    ADD CONSTRAINT seo_metrics_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: seo_url_patterns seo_url_patterns_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_url_patterns
    ADD CONSTRAINT seo_url_patterns_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: seo_url_patterns seo_url_patterns_language_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_url_patterns
    ADD CONSTRAINT seo_url_patterns_language_id_fkey FOREIGN KEY (language_id) REFERENCES public.languages(id);


--
-- Name: shop_categories shop_categories_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_categories
    ADD CONSTRAINT shop_categories_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: shop_categories shop_categories_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_categories
    ADD CONSTRAINT shop_categories_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: shop_categories shop_categories_mapped_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_categories
    ADD CONSTRAINT shop_categories_mapped_category_id_fkey FOREIGN KEY (mapped_category_id) REFERENCES public.product_categories(id);


--
-- Name: shop_categories shop_categories_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_categories
    ADD CONSTRAINT shop_categories_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.shop_categories(id);


--
-- Name: shop_filters shop_filters_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_filters
    ADD CONSTRAINT shop_filters_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: shop_filters shop_filters_parent_filter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_filters
    ADD CONSTRAINT shop_filters_parent_filter_id_fkey FOREIGN KEY (parent_filter_id) REFERENCES public.shop_filters(id);


--
-- Name: shop_filters shop_filters_shop_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_filters
    ADD CONSTRAINT shop_filters_shop_category_id_fkey FOREIGN KEY (shop_category_id) REFERENCES public.shop_categories(id);


--
-- Name: shop_groups shop_groups_headquarters_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_groups
    ADD CONSTRAINT shop_groups_headquarters_country_id_fkey FOREIGN KEY (headquarters_country_id) REFERENCES public.countries(id);


--
-- Name: shop_loyalty_programs shop_loyalty_programs_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_loyalty_programs
    ADD CONSTRAINT shop_loyalty_programs_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: shop_loyalty_programs shop_loyalty_programs_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_loyalty_programs
    ADD CONSTRAINT shop_loyalty_programs_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: shop_payment_methods shop_payment_methods_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_payment_methods
    ADD CONSTRAINT shop_payment_methods_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: shop_payment_methods shop_payment_methods_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_payment_methods
    ADD CONSTRAINT shop_payment_methods_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: shop_profiles shop_profiles_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_profiles
    ADD CONSTRAINT shop_profiles_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: shop_profiles shop_profiles_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_profiles
    ADD CONSTRAINT shop_profiles_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: shop_returns_policies shop_returns_policies_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_returns_policies
    ADD CONSTRAINT shop_returns_policies_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: shop_returns_policies shop_returns_policies_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_returns_policies
    ADD CONSTRAINT shop_returns_policies_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: shop_shipping_options shop_shipping_options_carrier_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_shipping_options
    ADD CONSTRAINT shop_shipping_options_carrier_id_fkey FOREIGN KEY (carrier_id) REFERENCES public.shipping_carriers(id);


--
-- Name: shop_shipping_options shop_shipping_options_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_shipping_options
    ADD CONSTRAINT shop_shipping_options_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: shop_shipping_options shop_shipping_options_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_shipping_options
    ADD CONSTRAINT shop_shipping_options_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: shop_usps shop_usps_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_usps
    ADD CONSTRAINT shop_usps_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: shop_usps shop_usps_country_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shop_usps
    ADD CONSTRAINT shop_usps_country_id_fkey FOREIGN KEY (country_id) REFERENCES public.countries(id);


--
-- Name: translations translations_language_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.translations
    ADD CONSTRAINT translations_language_id_fkey FOREIGN KEY (language_id) REFERENCES public.languages(id);


--
-- Name: website_checks website_checks_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.website_checks
    ADD CONSTRAINT website_checks_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.competitors(id);


--
-- Name: website_checks website_checks_portal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.website_checks
    ADD CONSTRAINT website_checks_portal_id_fkey FOREIGN KEY (portal_id) REFERENCES public.price_portals(id);


--
-- PostgreSQL database dump complete
--

\unrestrict s4Z6BaobwhHAQ6NOM6c9KxqIiVarcsdsTb3DBSdxkDTPgGrd6SGfzYDTbER2XTS

