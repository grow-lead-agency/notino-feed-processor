"""
Job 3: Affiliate XML Feed Downloader (Awin / CJ)
Stahuje XML product feedy po registraci do affiliate sítí.
Aktuálně DISABLED v crontabu — odkomentovat po registraci.
"""
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
import structlog

sys.path.insert(0, "/app")
from jobs.db import upsert_products

logger = structlog.get_logger()

FEEDS_DIR = Path("/app/feeds")
FEEDS_DIR.mkdir(parents=True, exist_ok=True)

# Affiliate feed konfigurace — doplnit po registraci
AWIN_FEEDS = [
    # {"shop": "notino", "feed_id": "XXXXX", "advertiser_id": "YYYYY"},
]

CJ_FEEDS = [
    # {"shop": "parfums", "cid": "XXXXX"},
]

AWIN_API_KEY = os.environ.get("AWIN_API_KEY", "")
CJ_API_KEY = os.environ.get("CJ_API_KEY", "")


def download_awin_feed(feed: dict) -> list[dict]:
    """Download and parse Awin XML product feed."""
    url = (
        f"https://productdata.awin.com/datafeed/download/apikey/{AWIN_API_KEY}"
        f"/language/cs/fid/{feed['feed_id']}/columns/aw_product_id,product_name,"
        f"store_price,aw_deep_link,data_feed_id/format/xml/"
    )
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    records = []
    for item in root.findall(".//prod"):
        records.append({
            "url": item.findtext("uri/awTrack") or "",
            "shop": feed["shop"],
            "title": item.findtext("pName") or "",
            "price": float(item.findtext("price/store") or 0),
            "currency": "CZK",
            "image_url": item.findtext("uri/product") or "",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "raw_json": ET.tostring(item, encoding="unicode"),
        })
    return records


def run():
    if not AWIN_API_KEY and not CJ_API_KEY:
        logger.warning("affiliate_keys_not_configured")
        return

    all_records = []
    for feed in AWIN_FEEDS:
        records = download_awin_feed(feed)
        all_records.extend(records)
        logger.info("awin_feed_done", shop=feed["shop"], count=len(records))

    upsert_products(all_records)
    logger.info("affiliate_done", total=len(all_records))


if __name__ == "__main__":
    run()
