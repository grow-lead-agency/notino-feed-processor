# Notino Feed Processor

## Co to je

Cron-based Docker service pro stahování produktových dat z beauty e-shopů.
Běží na Coolify (growlead-node-03). Zapisuje do Neon PG `notino-datamining`.

## Stack

- Python 3.12 + supercronic (cron v Dockeru)
- requests, lxml, beautifulsoup4, psycopg2-binary, structlog, tenacity
- Neon PostgreSQL (connection string v env `DATABASE_URL`)

## Cron jobs

| Job | Soubor | Interval | Co dělá |
|---|---|---|---|
| Sitemap crawler | `jobs/sitemap_crawler.py` | 6h | Stahuje sitemap XML z 316 shopů, parsuje product URLs |
| JSON-LD scraper | `jobs/jsonld_scraper.py` | denně 2:00 | Curl product pages, extrahuje JSON-LD Product schema (easy/medium shopy) |
| Hlídač Shopů | `jobs/hlidac_shopu.py` | denně 4:00 | Volá API `api.hlidacshopu.cz/v2/detail` pro CZ/SK price history |
| Affiliate feeds | `jobs/affiliate_feeds.py` | 6h | Stahuje Awin/CJ XML feedy (DISABLED — čeká na registraci) |
| Firecrawl scraper | `jobs/firecrawl_scraper.py` | denně 3:00 | Managed scraping přes Firecrawl API — hard shopy s CF challenge/DataDome, source='firecrawl_jsonld' |
| Sitemap diff | integrováno v sitemap_crawler | denně | Detekuje nové/smazané produkty |

## Deploy

```bash
# Push to GitHub → Coolify auto-deploy
git push origin main

# Manuální redeploy
source ~/.secrets/.env.master
curl -s "$COOLIFY_API_URL/api/v1/deploy?uuid=rtgj2inqwjjsms4ifgfh5a05" \
  -H "Authorization: Bearer $COOLIFY_API_TOKEN"
```

## Env vars (v Coolify)

| Var | Popis |
|---|---|
| `DATABASE_URL` | Neon PG connection string |
| `LOG_LEVEL` | INFO / DEBUG |
| `AWIN_API_KEY` | Awin publisher API key (prázdný, čeká na registraci) |
| `FIRECRAWL_API_KEY` | Firecrawl API key pro hard/protected shopy (CF challenge, DataDome) |

## Lokální vývoj

```bash
cd DEV/clients/notino/feed-processor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://neondb_owner:npg_s0bkdQueT6BA@ep-soft-forest-agqhbeg0.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require"
python jobs/jsonld_scraper.py
```

## DB

Sdílená DB pro všechny Notino tooly: `DEV/clients/notino/db/`
Schema, migrace, README — vše tam. Feed-processor zapisuje do:
- `products`, `product_offers`, `offer_price_history` (core)
- `brands` (auto-create)
- `scrape_run_metrics`, `firecrawl_usage` (monitoring)
- `sitemap_crawl_log`, `hlidac_shopu_data` (enrichment)

## GitHub

Repo: `grow-lead-agency/notino-feed-processor`
Coolify UUID: `rtgj2inqwjjsms4ifgfh5a05`
Server: growlead-node-03
