-- ============================================================================
-- Migration 003: Add 'firecrawl_jsonld' to feed_source enum
-- ============================================================================
-- Firecrawl-based JSON-LD scraper (jobs/firecrawl_scraper.py) needs its own
-- source value so we can distinguish managed-scrape data from direct curl scrapes.
--
-- Enum values must be added transaction-free because ALTER TYPE ... ADD VALUE
-- cannot be rolled back inside a transaction block in older PG versions.
-- Neon / PG 14+ supports this inside a transaction, so we keep it simple.
--
-- Apply:
--   psql "$DATABASE_URL" -f sql/003_add_firecrawl_source.sql
-- ============================================================================

ALTER TYPE feed_source ADD VALUE IF NOT EXISTS 'firecrawl_jsonld';

-- Verify:
-- SELECT enumlabel FROM pg_enum
-- WHERE enumtypid = 'feed_source'::regtype
-- ORDER BY enumsortorder;
