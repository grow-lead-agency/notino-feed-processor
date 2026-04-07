# Notino Feed Processor

## Co to je

Cron-based Docker service pro stahování produktových dat z beauty e-shopů.
Běží na Coolify (growlead-node-03). Zapisuje do Neon PG `notino-datamining`.

## Stack

- Python 3.12 + supercronic (cron v Dockeru)
- requests, lxml, beautifulsoup4, psycopg2-binary, structlog, tenacity
- Neon PostgreSQL (connection string v env `DATABASE_URL`)
- Redis queue — Coolify-managed (viz níže)

## Cron jobs

| Job | Soubor | Interval | Co dělá |
|---|---|---|---|
| Sitemap crawler | `jobs/sitemap_crawler.py` | 6h | Stahuje sitemap XML z 316 shopů, parsuje product URLs |
| JSON-LD scraper | `jobs/jsonld_scraper.py` | denně 2:00 | Curl product pages, extrahuje JSON-LD Product schema (easy/medium shopy) |
| Hlídač Shopů | `jobs/hlidac_shopu.py` | každé 4h | **Dual flow:** (1) Price sync pro CZ/SK hard/blocked shopy → `product_offers` + `offer_price_history`, (2) Enrichment → `hlidac_shopu_data`. Free alternative to Firecrawl. Max 500 URLs/run, 200ms rate limit. |
| Affiliate feeds | `jobs/affiliate_feeds.py` | 6h | Stahuje Awin/CJ XML feedy (DISABLED — čeká na registraci) |
| Firecrawl scraper | `jobs/firecrawl_scraper.py` | denně 3:00 | Managed scraping přes Firecrawl API — hard shopy s CF challenge/DataDome, source='firecrawl_jsonld' |
| Shipping scraper | `jobs/shipping_scraper.py` | hodinově :15 | Scrape shipping info stránek — extrahuje shipping_zones + shipping_methods. Engine: Crawl4AI → Firecrawl fallback. Batch 50 shopů/run. |
| Legal scraper | `jobs/legal_scraper.py` | hodinově :45 | Scrape imprint stránek — extrahuje legal_name, VAT ID, IČO. Engine: Crawl4AI → Firecrawl fallback. Batch 50 shopů/run. |
| Sitemap diff | integrováno v sitemap_crawler | denně | Detekuje nové/smazané produkty |
| Langfuse exporter | `jobs/langfuse_exporter.py` | */15 min | Exportuje AI cost data z Langfuse API → `ai_cost_log` tabulka pro Grafana |

## Deploy

```bash
# Push to GitHub → Coolify auto-deploy
git push origin main

# Manuální redeploy
COOLIFY_API_URL=$(grep COOLIFY_API_URL ~/.secrets/.env.master | cut -d= -f2-)
COOLIFY_API_TOKEN=$(grep COOLIFY_API_TOKEN ~/.secrets/.env.master | cut -d= -f2-)
curl -s "$COOLIFY_API_URL/api/v1/deploy?uuid=rtgj2inqwjjsms4ifgfh5a05" \
  -H "Authorization: Bearer $COOLIFY_API_TOKEN"
```

## Env vars (v Coolify)

| Var | Popis |
|---|---|
| `DATABASE_URL` | Neon PG connection string |
| `REDIS_URL` | Redis connection string — automaticky nastaven na Coolify Redis (viz níže) |
| `LOG_LEVEL` | INFO / DEBUG |
| `AWIN_API_KEY` | Awin publisher API key (prázdný, čeká na registraci) |
| `FIRECRAWL_API_KEY` | Firecrawl API key pro hard/protected shopy (CF challenge, DataDome) — fallback only |
| `CRAWL4AI_URL` | Crawl4AI self-hosted URL (default: `http://crawl4ai:11235`) — primary scraping engine |
| `OPENROUTER_API_KEY` | OpenRouter API key pro Crawl4AI LLM extraction (gpt-4o-mini) |
| `GEMINI_API_KEY` | Google Gemini API key pro category mapper + product categorizer |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key (z langfuse.growlead.cz) |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key |
| `LANGFUSE_HOST` | Langfuse URL (default: `https://langfuse.growlead.cz`) |

## Redis (Coolify-managed)

Redis je **Coolify-managed Database resource** na growlead-node-03 — ne manuální `docker run`.

| | |
|---|---|
| **Coolify UUID** | `ix7k6j0ve5i8mm11f8t7e68b` |
| **Image** | `redis:7-alpine` |
| **Internal hostname** | `ix7k6j0ve5i8mm11f8t7e68b` (na coolify docker network) |
| **Port** | 6379 (interní only — žádný public port) |
| **Auth** | Ano — password v Coolify env `REDIS_PASSWORD` |
| **AOF persistence** | Ano |
| **Resources** | 0.5 CPU / 768 MB RAM |
| **Network** | `coolify` (172.18.x.x) |
| **Public port** | Žádný — není dostupný zvenčí |

Starý manuální container `notino-redis` (bridge network, bez auth) byl odstraněn 2026-04-06.

```bash
# Připojení přes SSH tunnel (debug)
ssh -L 6399:ix7k6j0ve5i8mm11f8t7e68b:6379 hetzner-node-03
# pak lokálně: redis-cli -p 6399 -a <password>

# Queue stats z feed-processor containeru
ssh hetzner-node-03 'docker exec rtgj2inqwjjsms4ifgfh5a05-* python3 /app/jobs/job_queue.py stats'
```

## Bull Board (Queue monitoring UI)

| | |
|---|---|
| **URL** | https://queue.notino.growlead.dev |
| **Coolify UUID** | `qbah89qk0rghfu2hlx5k2ayp` |
| **Image** | `deadly0/bull-board:latest` |
| **Auth** | Username: `admin` / Password: v Coolify env `USER_PASSWORD` |
| **Přístup** | HTTPS přes Traefik, Let's Encrypt SSL, auth required |

Credentials jsou uloženy v Coolify env vars pro `notino-bull-board` — ne v kódu ani gitu.
Pro aktuální heslo: Coolify Dashboard → notino-bull-board → Environment Variables → `USER_PASSWORD`.

## Lokální vývoj

```bash
cd DEV/clients/notino/feed-processor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://neondb_owner:npg_s0bkdQueT6BA@ep-soft-forest-agqhbeg0.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require"
# Redis pro lokální testy: spusť redis-cli přes SSH tunnel (viz výše)
python jobs/jsonld_scraper.py
```

## DB

Sdílená DB pro všechny Notino tooly: `DEV/clients/notino/db/`
Schema, migrace, README — vše tam. Feed-processor zapisuje do:
- `products`, `product_offers`, `offer_price_history` (core)
- `brands` (auto-create)
- `shipping_zones`, `shipping_methods` (shipping scraper)
- `legal_entities` + `shops.legal_entity_id` (legal scraper)
- `scrape_run_metrics`, `firecrawl_usage` (monitoring)
- `sitemap_crawl_log`, `hlidac_shopu_data` (enrichment)

## GitHub

Repo: `grow-lead-agency/notino-feed-processor`
Coolify UUID: `rtgj2inqwjjsms4ifgfh5a05`
Server: growlead-node-03
