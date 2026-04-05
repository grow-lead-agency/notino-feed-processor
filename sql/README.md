# Database Migrations — notino-datamining

Neon PostgreSQL 17 | Project: `royal-violet-49149731` | Org: Growlead (Launch plan, 10 GB)

## Current state (2026-04-05)

- **75 tables**, 3 ENUMs, 3 custom functions, 4 views, 4 partitions
- **209 MB** / 10 GB
- Full schema dump: `schema-full-2026-04-05.sql` (7,199 lines)

## Migration history

| Migration | Date | What |
|---|---|---|
| `schema.sql` (original) | 2026-04-03 | Initial: sitemap_crawl_log, hlidac_shopu_data |
| `002_refactor_products_gmc.sql` | 2026-04-05 | 3-table GMC split: products → products + product_offers + offer_price_history (partitioned) |
| `003_add_firecrawl_source.sql` | 2026-04-05 | Add `firecrawl_jsonld` to feed_source ENUM |
| *ad-hoc sessions (not in files)* | 2026-04-05 | All remaining 70+ tables created via interactive psql |

## How to recreate from scratch

```bash
# Full schema (all 75 tables + functions + views + seeds)
psql "$DATABASE_URL" -f sql/schema-full-2026-04-05.sql
```

## Key tables

### Core product data (GMC 3-table split)
- `products` — master catalog (1 per unique EAN/product)
- `product_offers` — per shop × country (prices, availability)
- `offer_price_history` — partitioned time-series (monthly: 2026_04..2026_07)
- `products_legacy` — pre-migration backup (can be dropped)

### Competitors & classification
- `competitors` (533), `shop_groups` (38), `countries` (27), `languages` (23)
- `website_checks` — protection scan, sitemap, tech stack, social
- `affiliate_programs`, `affiliate_networks`

### Normalization
- `normalization_rules` — universal pattern → canonical (brands, attributes, values, colors)
- `brand_aliases` — brand dedup helpers
- `variant_mappings` — EDP/EDT/Cologne multi-lang
- `volume_conversions` — ml/oz/g/kg
- `exchange_rates` — daily ECB rates (13 currencies)
- `category_mappings` — shop → master taxonomy

### Quality & review
- `review_queue` — auto-detected issues
- `ai_sweep_config` — AI job schedules
- `product_merge_queue` — dedup candidates

### Custom functions
- `normalize_brand(text)` — unaccent + lowercase
- `normalize_value(entity_type, raw, language)` — universal lookup
- `price_in_eur(price, currency, date)` — EUR conversion

### ENUMs
- `availability_status`: in_stock, out_of_stock, preorder, backorder, limited
- `product_condition`: new, refurbished, used
- `feed_source`: sitemap_scrape, jsonld_scrape, microdata_scrape, affiliate_feed, api, manual, firecrawl_jsonld
