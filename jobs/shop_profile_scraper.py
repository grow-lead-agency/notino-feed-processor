"""
Job: Shop Profile Scraper
Cíl: scrape about/info/payment/returns/loyalty stránky ze shopů bez profile_checked_at.
Plní 5 tabulek: shop_profiles, shop_payment_methods, shop_returns_policies, shop_usps, shop_loyalty_programs.

Workflow per shop:
  1. Homepage → Firecrawl AI extract → shop_usps (free shipping badge, garance, etc.)
  2. About/info stránka → Firecrawl AI extract → shop_profiles (headline, about, pros, cons)
  3. Payment stránka → Firecrawl AI extract → shop_payment_methods
  4. Returns stránka → Firecrawl AI extract → shop_returns_policies
  5. Loyalty stránka → Firecrawl AI extract → shop_loyalty_programs (pokud existuje)

Cron: denně — batch ~20 shopů per run (Firecrawl budget control).
Interval: 30d refresh (profile_checked_at).

Notes:
  - Shops often 404 on these pages — handled gracefully (skip, still mark checked).
  - config_override from shop_scrape_config can specify exact page paths.
  - Rate limiting: 1 req/s between Firecrawl calls.
  - All upserts are idempotent (DELETE + INSERT or ON CONFLICT DO UPDATE).
"""
import os
import sys
import time
import json

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.langfuse_wrapper import traced_generation


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

MAX_SHOPS_PER_RUN = 20
FIRECRAWL_TIMEOUT_SECONDS = 60
FIRECRAWL_MAX_RETRIES = 2
RATE_LIMIT_DELAY = 1.0  # seconds between Firecrawl calls

# Common page path candidates per page type (ordered by specificity)
COMMON_PAGES = {
    "about": [
        # CZ/SK
        "o-nas", "o-spolecnosti", "o-firme", "nase-pribehy",
        # EN
        "about", "about-us", "our-story", "company", "who-we-are",
        # DE
        "uber-uns", "ueber-uns", "unternehmen",
        # FR
        "a-propos", "qui-sommes-nous",
        # IT
        "chi-siamo",
        # ES
        "sobre-nosotros", "quienes-somos",
        # NL
        "over-ons",
        # PL
        "o-nas",
        # HU
        "rolunk",
    ],
    "payment": [
        # CZ/SK
        "platba", "platebni-metody", "zpusoby-platby", "platobne-moznosti",
        "doprava-a-platba",
        # EN
        "payment", "payment-methods", "payment-options", "how-to-pay",
        # DE
        "zahlungsmethoden", "zahlungsarten", "zahlung", "bezahlung",
        # FR
        "paiement", "modes-de-paiement",
        # IT
        "pagamento", "metodi-di-pagamento",
        # ES
        "pago", "metodos-de-pago",
        # NL
        "betaling", "betaalmethoden",
        # PL
        "platnosci", "metody-platnosci",
    ],
    "returns": [
        # CZ/SK
        "reklamace", "vraceni-zbozi", "vraceni", "odstoupeni-od-smlouvy",
        "reklamacni-rad",
        # EN
        "returns", "return-policy", "refund-policy", "returns-and-refunds",
        "returns-policy",
        # DE
        "widerrufsrecht", "widerrufsbelehrung", "rueckgabe", "retoure",
        "widerrufsrecht-und-rueckgabe",
        # FR
        "retours", "politique-de-retour",
        # IT
        "resi", "politica-di-reso",
        # ES
        "devoluciones", "politica-de-devoluciones",
        # NL
        "retourneren", "retourbeleid",
        # PL
        "zwroty", "reklamacje",
    ],
    "loyalty": [
        # CZ/SK
        "vernostni-program", "vernostni-body", "bonusovy-program", "vernostni-klub",
        # EN
        "loyalty", "loyalty-program", "rewards", "rewards-program", "bonus-program",
        # DE
        "treueprogramm", "bonusprogramm",
        # FR
        "programme-fidelite",
        # IT
        "programma-fedelta",
        # ES
        "programa-fidelidad",
        # NL
        "loyaliteitsprogramma",
        # PL
        "program-lojalnosciowy",
    ],
}


# ----------------------------------------------------------------------------
# Firecrawl extraction schemas & prompts
# ----------------------------------------------------------------------------
PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "Short tagline or headline of the e-shop (1 sentence max).",
        },
        "intro": {
            "type": "string",
            "description": "1-2 sentence intro / elevator pitch about what the shop sells.",
        },
        "about": {
            "type": "string",
            "description": "Longer about text (2-5 sentences). History, mission, values.",
        },
        "pros": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key selling points / advantages of this shop (3-8 items).",
        },
        "cons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any disadvantages or limitations mentioned or implied.",
        },
        "best_for": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Who is this shop best for? E.g. 'budget shoppers', 'luxury beauty fans', 'professional hairdressers'.",
        },
        "customer_rating": {
            "type": "number",
            "description": "Average customer rating if displayed (1.0-5.0 scale). Omit if not found.",
        },
        "trust_indicators": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Trust badges, certifications, awards, year founded, etc.",
        },
    },
}

PROFILE_PROMPT = (
    "Extract e-shop profile information from this about/info page. "
    "Find the shop's tagline/headline, an intro sentence, longer about text, "
    "key selling points (pros), disadvantages (cons), who the shop is best for, "
    "customer rating if shown, and trust indicators (certifications, awards, year founded)."
)

PAYMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "payment_methods": {
            "type": "array",
            "description": "List of payment methods accepted by this e-shop.",
            "items": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "description": (
                            "Payment method type. One of: cod, card, transfer, wallet, bnpl, cash, "
                            "apple_pay, google_pay, paypal, klarna, twisto, gopay, comgate, stripe, adyen, other"
                        ),
                    },
                    "fee_amount": {
                        "type": "number",
                        "description": "Fixed fee for this method (e.g. 39 CZK for COD). Omit if free.",
                    },
                    "fee_pct": {
                        "type": "number",
                        "description": "Percentage fee (e.g. 1.5 for 1.5%). Omit if none.",
                    },
                    "is_free": {
                        "type": "boolean",
                        "description": "True if no fee for this method.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Additional detail (e.g. 'Visa, Mastercard, Maestro').",
                    },
                },
                "required": ["method"],
            },
        },
    },
    "required": ["payment_methods"],
}

PAYMENT_PROMPT = (
    "Extract payment methods from this e-shop page. For each method, identify: "
    "method type (cod=cash on delivery, card=credit/debit card, transfer=bank transfer, "
    "wallet=digital wallet, bnpl=buy now pay later, apple_pay, google_pay, paypal, klarna, "
    "twisto, gopay, comgate, stripe, adyen, or other), "
    "any associated fee amount or percentage, whether it's free, "
    "and any description. Return as payment_methods array."
)

RETURNS_SCHEMA = {
    "type": "object",
    "properties": {
        "return_days": {
            "type": "integer",
            "description": "Number of days for returning items (e.g. 14, 30, 60).",
        },
        "free_returns": {
            "type": "boolean",
            "description": "True if return shipping is free.",
        },
        "return_fee": {
            "type": "number",
            "description": "Fee for returning items (shipping cost). Omit if free.",
        },
        "conditions": {
            "type": "string",
            "description": "General conditions/requirements for returns (1-3 sentences).",
        },
        "exceptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Product categories or conditions excluded from returns.",
        },
    },
}

RETURNS_PROMPT = (
    "Extract the return/refund policy from this e-shop page. "
    "Find: number of days for returns, whether returns are free, "
    "any return shipping fee, general conditions, "
    "and exceptions (product types that cannot be returned)."
)

USPS_SCHEMA = {
    "type": "object",
    "properties": {
        "usps": {
            "type": "array",
            "description": "Unique selling propositions / key value messages found on the page.",
            "items": {
                "type": "object",
                "properties": {
                    "usp_type": {
                        "type": "string",
                        "description": (
                            "Type of USP. One of: free_shipping, fast_delivery, free_returns, "
                            "price_guarantee, money_back, authenticity_guarantee, "
                            "large_selection, loyalty_program, expert_advice, "
                            "eco_friendly, samples, gift_wrapping, discount, other"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title of the USP as displayed (e.g. 'Free shipping over 50 EUR').",
                    },
                    "description": {
                        "type": "string",
                        "description": "Longer description if available.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Specific value (e.g. '50 EUR', '24h', '14 days').",
                    },
                    "is_free": {
                        "type": "boolean",
                        "description": "True if this USP is about something being free.",
                    },
                    "days_number": {
                        "type": "integer",
                        "description": "Number of days if relevant (delivery days, return days).",
                    },
                },
                "required": ["usp_type", "title"],
            },
        },
    },
    "required": ["usps"],
}

USPS_PROMPT = (
    "Extract unique selling propositions (USPs) and key value messages from this e-shop homepage. "
    "Look for: free shipping badges, delivery speed promises, return guarantees, "
    "price match/guarantee, authenticity badges, loyalty program mentions, "
    "expert advice, eco-friendly claims, free samples, gift wrapping, discount banners. "
    "Focus on the header bar, hero section, and trust badge area. "
    "Classify each USP type: free_shipping, fast_delivery, free_returns, "
    "price_guarantee, money_back, authenticity_guarantee, large_selection, "
    "loyalty_program, expert_advice, eco_friendly, samples, gift_wrapping, discount, other."
)

LOYALTY_SCHEMA = {
    "type": "object",
    "properties": {
        "program_name": {
            "type": "string",
            "description": "Name of the loyalty program.",
        },
        "program_type": {
            "type": "string",
            "description": "Type: points, cashback, tier, discount, vip, or other.",
        },
        "tiers": {
            "type": "array",
            "description": "Tier levels if any (e.g. Silver, Gold, Platinum).",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "threshold": {"type": "string"},
                    "benefits": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "earn_rate": {
            "type": "string",
            "description": "How points/cashback are earned (e.g. '1 point per 1 EUR', '5% cashback').",
        },
        "redemption_rate": {
            "type": "string",
            "description": "How rewards are redeemed (e.g. '100 points = 1 EUR discount').",
        },
        "benefits": {
            "type": "array",
            "items": {"type": "string"},
            "description": "General benefits of joining the program.",
        },
        "join_requirements": {
            "type": "string",
            "description": "How to join (free registration, purchase required, etc.).",
        },
    },
}

LOYALTY_PROMPT = (
    "Extract loyalty/rewards program details from this e-shop page. "
    "Find: program name, type (points/cashback/tier/discount/vip), "
    "tier levels and their benefits, how points/rewards are earned, "
    "how they can be redeemed, general benefits of the program, "
    "and requirements to join."
)


# ----------------------------------------------------------------------------
# Firecrawl helper
# ----------------------------------------------------------------------------
def firecrawl_extract(url: str, prompt: str, schema: dict) -> dict | None:
    """Call Firecrawl /v1/scrape with AI extraction schema. Traced via Langfuse."""
    if not FIRECRAWL_API_KEY:
        print("[shop_profile] FATAL: FIRECRAWL_API_KEY missing", flush=True)
        return None

    payload = {
        "url": url,
        "formats": ["json"],
        "jsonOptions": {
            "schema": schema,
            "prompt": prompt,
        },
        "proxy": "stealth",
        "onlyMainContent": True,
        "waitFor": 2000,
        "timeout": FIRECRAWL_TIMEOUT_SECONDS * 1000,
    }
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }

    with traced_generation(
        name="firecrawl-shop-profile",
        model="firecrawl/extract",
        input_data={"url": url, "prompt": prompt},
        metadata={"job": "shop_profile_scraper"},
        tags=["feed-processor", "firecrawl", "shop-profile"],
    ) as gen:
        for attempt in range(FIRECRAWL_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    FIRECRAWL_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=FIRECRAWL_TIMEOUT_SECONDS + 10,
                )
            except requests.RequestException as e:
                if attempt < FIRECRAWL_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[shop_profile] Network error for {url}: {e}", flush=True)
                gen.end(level="ERROR", status_message=f"Network error: {e}")
                return None

            if resp.status_code == 429:
                if attempt < FIRECRAWL_MAX_RETRIES:
                    wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                    time.sleep(min(wait, 30))
                    continue
                gen.end(level="ERROR", status_message="429 rate limited, retries exhausted")
                return None

            if resp.status_code in (403, 404):
                gen.end(level="WARNING", status_message=f"HTTP {resp.status_code}")
                return None

            if resp.status_code >= 500:
                if attempt < FIRECRAWL_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                gen.end(level="ERROR", status_message=f"HTTP {resp.status_code} after retries")
                return None

            if resp.status_code != 200:
                gen.end(level="WARNING", status_message=f"HTTP {resp.status_code}")
                return None

            try:
                body = resp.json()
            except ValueError:
                gen.end(level="ERROR", status_message="Invalid JSON response")
                return None

            if not body.get("success"):
                gen.end(level="WARNING", status_message="Firecrawl success=false")
                return None

            data = body.get("data") or {}
            result = data.get("json")
            gen.end(output=result, usage={"total": 1})
            return result

        gen.end(level="ERROR", status_message="All retries exhausted")
        return None


def head_check(url: str) -> bool:
    """Quick HEAD check — returns True if page likely exists (2xx/3xx)."""
    try:
        head = requests.head(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
            timeout=10,
            allow_redirects=True,
        )
        return head.status_code < 400
    except requests.RequestException:
        return False


# ----------------------------------------------------------------------------
# URL discovery
# ----------------------------------------------------------------------------
def discover_page_urls(shop_url: str, config_override: dict | None = None) -> dict:
    """
    Discover page URLs for about/payment/returns/loyalty.
    Priority: config_override > HEAD check on common patterns.
    Returns {"about": url_or_None, "payment": url_or_None, ...}
    """
    base = shop_url.rstrip("/")
    result = {"about": None, "payment": None, "returns": None, "loyalty": None}

    # 1. config_override has exact paths
    scrape_pages = {}
    if config_override and isinstance(config_override, dict):
        scrape_pages = config_override.get("scrape_pages", {})

    for page_type in result:
        # Try override first
        if page_type in scrape_pages:
            override_path = scrape_pages[page_type].strip("/")
            candidate = f"{base}/{override_path}"
            if head_check(candidate):
                result[page_type] = candidate
                continue

        # Try common patterns
        for path in COMMON_PAGES.get(page_type, []):
            candidate = f"{base}/{path}"
            if head_check(candidate):
                result[page_type] = candidate
                break

    return result


# ----------------------------------------------------------------------------
# Extraction + save functions
# ----------------------------------------------------------------------------
VALID_PAYMENT_METHODS = frozenset([
    "cod", "card", "transfer", "wallet", "bnpl", "cash",
    "apple_pay", "google_pay", "paypal", "klarna", "twisto",
    "gopay", "comgate", "stripe", "adyen", "other",
])


def normalize_payment_method(raw: str | None) -> str:
    """Map LLM output to valid CHECK constraint values."""
    if not raw:
        return "other"
    r = raw.strip().lower().replace(" ", "_").replace("-", "_")

    # Direct match
    if r in VALID_PAYMENT_METHODS:
        return r

    # Fuzzy mapping
    mapping = {
        "credit_card": "card", "debit_card": "card", "visa": "card",
        "mastercard": "card", "maestro": "card", "amex": "card",
        "cash_on_delivery": "cod", "dobierka": "cod", "dobirka": "cod",
        "nachnahme": "cod",
        "bank_transfer": "transfer", "wire_transfer": "transfer",
        "prevod": "transfer", "prevodem": "transfer", "uberweisung": "transfer",
        "digital_wallet": "wallet", "e_wallet": "wallet",
        "buy_now_pay_later": "bnpl",
        "apple": "apple_pay", "applepay": "apple_pay",
        "google": "google_pay", "googlepay": "google_pay",
        "pay_pal": "paypal",
    }
    if r in mapping:
        return mapping[r]

    # Substring match for common names
    for key, val in mapping.items():
        if key in r or r in key:
            return val

    return "other"


def extract_and_save_profile(shop_id: int, country_id: int | None, about_url: str) -> int:
    """Extract profile from about page, upsert into shop_profiles. Returns 1 on success, 0 on failure."""
    print(f"  [profile] Extracting from {about_url}", flush=True)
    data = firecrawl_extract(about_url, PROFILE_PROMPT, PROFILE_SCHEMA)
    if not data:
        print(f"  [profile] No data extracted", flush=True)
        return 0

    headline = (data.get("headline") or "")[:500] or None
    intro = (data.get("intro") or "")[:1000] or None
    about = (data.get("about") or "")[:5000] or None
    pros = data.get("pros") if isinstance(data.get("pros"), list) else None
    cons = data.get("cons") if isinstance(data.get("cons"), list) else None
    best_for = data.get("best_for") if isinstance(data.get("best_for"), list) else None

    customer_rating = data.get("customer_rating")
    try:
        customer_rating = float(customer_rating) if customer_rating is not None else None
        if customer_rating is not None and (customer_rating < 0 or customer_rating > 5):
            customer_rating = None
    except (TypeError, ValueError):
        customer_rating = None

    if not headline and not intro and not about:
        print(f"  [profile] Empty extraction — skipping", flush=True)
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shop_profiles (
                    shop_id, country_id, headline, intro, about,
                    pros, cons, best_for, customer_rating
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (shop_id, country_id) DO UPDATE SET
                    headline = EXCLUDED.headline,
                    intro = EXCLUDED.intro,
                    about = EXCLUDED.about,
                    pros = EXCLUDED.pros,
                    cons = EXCLUDED.cons,
                    best_for = EXCLUDED.best_for,
                    customer_rating = EXCLUDED.customer_rating,
                    updated_at = NOW()
                """,
                (shop_id, country_id, headline, intro, about, pros, cons, best_for, customer_rating),
            )
            conn.commit()
        print(f"  [profile] Saved profile for shop {shop_id}", flush=True)
        return 1
    except Exception as e:
        conn.rollback()
        print(f"  [profile] DB error: {e}", flush=True)
        return 0
    finally:
        put_conn(conn)


def extract_and_save_payments(shop_id: int, country_id: int | None, payment_url: str) -> int:
    """Extract payment methods, upsert into shop_payment_methods. Returns count of methods saved."""
    print(f"  [payment] Extracting from {payment_url}", flush=True)
    data = firecrawl_extract(payment_url, PAYMENT_PROMPT, PAYMENT_SCHEMA)
    if not data:
        print(f"  [payment] No data extracted", flush=True)
        return 0

    methods = data.get("payment_methods")
    if not isinstance(methods, list) or not methods:
        print(f"  [payment] No payment methods found", flush=True)
        return 0

    saved = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for m in methods:
                if not isinstance(m, dict):
                    continue

                method = normalize_payment_method(m.get("method"))

                fee_amount = m.get("fee_amount")
                try:
                    fee_amount = float(fee_amount) if fee_amount is not None else None
                except (TypeError, ValueError):
                    fee_amount = None

                fee_pct = m.get("fee_pct")
                try:
                    fee_pct = float(fee_pct) if fee_pct is not None else None
                except (TypeError, ValueError):
                    fee_pct = None

                is_free = m.get("is_free")
                if is_free is None:
                    is_free = fee_amount is None and fee_pct is None

                description = (m.get("description") or "")[:500] or None

                try:
                    cur.execute(
                        """
                        INSERT INTO shop_payment_methods (
                            shop_id, country_id, method, fee_amount, fee_pct, is_free, description
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (shop_id, country_id, method) DO UPDATE SET
                            fee_amount = EXCLUDED.fee_amount,
                            fee_pct = EXCLUDED.fee_pct,
                            is_free = EXCLUDED.is_free,
                            description = EXCLUDED.description,
                            updated_at = NOW()
                        """,
                        (shop_id, country_id, method, fee_amount, fee_pct, is_free, description),
                    )
                    saved += 1
                except Exception as e:
                    print(f"  [payment] Insert error for method '{method}': {e}", flush=True)

            conn.commit()
        print(f"  [payment] Saved {saved} methods for shop {shop_id}", flush=True)
    except Exception as e:
        conn.rollback()
        print(f"  [payment] DB error: {e}", flush=True)
        saved = 0
    finally:
        put_conn(conn)

    return saved


def extract_and_save_returns(shop_id: int, country_id: int | None, returns_url: str) -> int:
    """Extract returns policy, upsert into shop_returns_policies. Returns 1 on success, 0 on failure."""
    print(f"  [returns] Extracting from {returns_url}", flush=True)
    data = firecrawl_extract(returns_url, RETURNS_PROMPT, RETURNS_SCHEMA)
    if not data:
        print(f"  [returns] No data extracted", flush=True)
        return 0

    return_days = data.get("return_days")
    try:
        return_days = int(return_days) if return_days is not None else None
    except (TypeError, ValueError):
        return_days = None

    free_returns = data.get("free_returns")
    if not isinstance(free_returns, bool):
        free_returns = None

    return_fee = data.get("return_fee")
    try:
        return_fee = float(return_fee) if return_fee is not None else None
    except (TypeError, ValueError):
        return_fee = None

    conditions = (data.get("conditions") or "")[:2000] or None
    exceptions = data.get("exceptions") if isinstance(data.get("exceptions"), list) else None

    if return_days is None and free_returns is None and conditions is None:
        print(f"  [returns] Empty extraction — skipping", flush=True)
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # No unique constraint on shop_returns_policies — delete + insert
            cur.execute(
                "DELETE FROM shop_returns_policies WHERE shop_id = %s AND country_id IS NOT DISTINCT FROM %s",
                (shop_id, country_id),
            )
            cur.execute(
                """
                INSERT INTO shop_returns_policies (
                    shop_id, country_id, return_days, free_returns, return_fee,
                    conditions, exceptions, scraped_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                (shop_id, country_id, return_days, free_returns, return_fee, conditions, exceptions),
            )
            conn.commit()
        print(f"  [returns] Saved returns policy for shop {shop_id} ({return_days}d, free={free_returns})", flush=True)
        return 1
    except Exception as e:
        conn.rollback()
        print(f"  [returns] DB error: {e}", flush=True)
        return 0
    finally:
        put_conn(conn)


def extract_and_save_usps(shop_id: int, country_id: int | None, homepage_url: str) -> int:
    """Extract USPs from homepage, save into shop_usps. Returns count saved."""
    print(f"  [usps] Extracting from {homepage_url}", flush=True)
    data = firecrawl_extract(homepage_url, USPS_PROMPT, USPS_SCHEMA)
    if not data:
        print(f"  [usps] No data extracted", flush=True)
        return 0

    usps = data.get("usps")
    if not isinstance(usps, list) or not usps:
        print(f"  [usps] No USPs found", flush=True)
        return 0

    saved = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # No unique constraint — delete existing and re-insert
            cur.execute(
                "DELETE FROM shop_usps WHERE shop_id = %s AND country_id IS NOT DISTINCT FROM %s",
                (shop_id, country_id),
            )

            for u in usps:
                if not isinstance(u, dict):
                    continue

                usp_type = (u.get("usp_type") or "other").strip().lower().replace(" ", "_")
                title = (u.get("title") or "").strip()
                if not title:
                    continue

                description = (u.get("description") or "")[:1000] or None
                value = (u.get("value") or "")[:200] or None
                is_free = u.get("is_free") if isinstance(u.get("is_free"), bool) else None

                days_number = u.get("days_number")
                try:
                    days_number = int(days_number) if days_number is not None else None
                except (TypeError, ValueError):
                    days_number = None

                try:
                    cur.execute(
                        """
                        INSERT INTO shop_usps (
                            shop_id, country_id, usp_type, title, description,
                            value, days_number, is_free, scraped_at, source_url
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                        """,
                        (shop_id, country_id, usp_type, title[:500], description,
                         value, days_number, is_free, homepage_url),
                    )
                    saved += 1
                except Exception as e:
                    print(f"  [usps] Insert error for '{title[:50]}': {e}", flush=True)

            conn.commit()
        print(f"  [usps] Saved {saved} USPs for shop {shop_id}", flush=True)
    except Exception as e:
        conn.rollback()
        print(f"  [usps] DB error: {e}", flush=True)
        saved = 0
    finally:
        put_conn(conn)

    return saved


def extract_and_save_loyalty(shop_id: int, country_id: int | None, loyalty_url: str) -> int:
    """Extract loyalty program, save into shop_loyalty_programs. Returns 1 on success, 0 on failure."""
    print(f"  [loyalty] Extracting from {loyalty_url}", flush=True)
    data = firecrawl_extract(loyalty_url, LOYALTY_PROMPT, LOYALTY_SCHEMA)
    if not data:
        print(f"  [loyalty] No data extracted", flush=True)
        return 0

    program_name = (data.get("program_name") or "").strip()
    if not program_name:
        print(f"  [loyalty] No program name found — skipping", flush=True)
        return 0

    program_type = (data.get("program_type") or "other").strip().lower()
    tiers = data.get("tiers") if isinstance(data.get("tiers"), list) else None
    benefits = data.get("benefits") if isinstance(data.get("benefits"), list) else None
    join_requirements = (data.get("join_requirements") or "")[:1000] or None

    # Parse earn/redemption rates — try to extract numeric value
    earn_rate = _parse_rate(data.get("earn_rate"))
    redemption_rate = _parse_rate(data.get("redemption_rate"))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # No unique constraint — delete + insert for idempotency
            cur.execute(
                "DELETE FROM shop_loyalty_programs WHERE shop_id = %s AND country_id IS NOT DISTINCT FROM %s",
                (shop_id, country_id),
            )
            cur.execute(
                """
                INSERT INTO shop_loyalty_programs (
                    shop_id, country_id, program_name, program_type,
                    tiers, earn_rate, redemption_rate, benefits,
                    join_requirements, scraped_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    shop_id, country_id, program_name[:500], program_type,
                    json.dumps(tiers) if tiers else None,
                    earn_rate, redemption_rate, benefits, join_requirements,
                ),
            )
            conn.commit()
        print(f"  [loyalty] Saved loyalty program '{program_name}' for shop {shop_id}", flush=True)
        return 1
    except Exception as e:
        conn.rollback()
        print(f"  [loyalty] DB error: {e}", flush=True)
        return 0
    finally:
        put_conn(conn)


def _parse_rate(raw: str | None) -> float | None:
    """Try to extract a numeric rate from LLM text like '5% cashback' or '1 point per 1 EUR'."""
    if not raw:
        return None
    import re
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", raw)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


# ----------------------------------------------------------------------------
# Main per-shop orchestrator
# ----------------------------------------------------------------------------
def scrape_shop_profile(shop_id: int, domain: str, shop_url: str, country_id: int | None = None,
                        config_override: dict | None = None) -> int:
    """
    Main entry — scrape all profile pages for one shop.
    Returns total items saved across all 5 tables.
    """
    print(f"\n[{domain}] Starting shop profile scrape...", flush=True)
    total = 0

    # 1. Discover page URLs
    pages = discover_page_urls(shop_url, config_override)
    print(f"[{domain}] Discovered pages: "
          + ", ".join(f"{k}={'YES' if v else 'no'}" for k, v in pages.items()),
          flush=True)

    # 2. Homepage USPs (always use shop_url directly)
    try:
        total += extract_and_save_usps(shop_id, country_id, shop_url)
    except Exception as e:
        print(f"[{domain}] USPs error: {e}", flush=True)
    time.sleep(RATE_LIMIT_DELAY)

    # 3. About/profile page
    if pages["about"]:
        try:
            total += extract_and_save_profile(shop_id, country_id, pages["about"])
        except Exception as e:
            print(f"[{domain}] Profile error: {e}", flush=True)
        time.sleep(RATE_LIMIT_DELAY)

    # 4. Payment page
    if pages["payment"]:
        try:
            total += extract_and_save_payments(shop_id, country_id, pages["payment"])
        except Exception as e:
            print(f"[{domain}] Payment error: {e}", flush=True)
        time.sleep(RATE_LIMIT_DELAY)

    # 5. Returns page
    if pages["returns"]:
        try:
            total += extract_and_save_returns(shop_id, country_id, pages["returns"])
        except Exception as e:
            print(f"[{domain}] Returns error: {e}", flush=True)
        time.sleep(RATE_LIMIT_DELAY)

    # 6. Loyalty page (optional — many shops don't have one)
    if pages["loyalty"]:
        try:
            total += extract_and_save_loyalty(shop_id, country_id, pages["loyalty"])
        except Exception as e:
            print(f"[{domain}] Loyalty error: {e}", flush=True)
        time.sleep(RATE_LIMIT_DELAY)

    # Mark shop as profile-checked
    mark_profile_checked(shop_id)
    print(f"[{domain}] Profile scrape done — {total} items saved", flush=True)
    return total


# ----------------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------------
def get_shops_to_scrape(limit: int = MAX_SHOPS_PER_RUN) -> list[tuple]:
    """
    Load shops without profile data.
    Priority: shops with offers (active) first, then by id.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, url, country_id
                FROM shops
                WHERE deleted_at IS NULL
                  AND profile_checked_at IS NULL
                  AND url IS NOT NULL
                ORDER BY
                    (offers_checked_at IS NOT NULL) DESC,
                    id
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        put_conn(conn)


def mark_profile_checked(shop_id: int) -> None:
    """Update shops.profile_checked_at = NOW()."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shops SET profile_checked_at = NOW() WHERE id = %s AND deleted_at IS NULL",
                (shop_id,),
            )
            conn.commit()
    finally:
        put_conn(conn)


# ----------------------------------------------------------------------------
# Entrypoint (standalone batch mode)
# ----------------------------------------------------------------------------
def main() -> None:
    start_ts = time.time()
    print(f"=== Shop Profile Scraper starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    if not FIRECRAWL_API_KEY:
        print("[shop_profile] ERROR: FIRECRAWL_API_KEY missing — aborting", flush=True)
        sys.exit(1)

    shops = get_shops_to_scrape()
    print(f"Processing {len(shops)} shops (max {MAX_SHOPS_PER_RUN}/run)", flush=True)

    if not shops:
        print("No shops to scrape. Done.", flush=True)
        return

    total_items = 0
    for shop_row in shops:
        shop_id, name, url, country_id = shop_row
        try:
            count = scrape_shop_profile(shop_id, name, url, country_id)
            total_items += count
        except Exception as e:
            print(f"[{name}] Fatal error: {e}", flush=True)
            # Still mark as checked to avoid infinite retries
            mark_profile_checked(shop_id)

    elapsed = time.time() - start_ts
    print(
        f"=== Done: {total_items} items saved | "
        f"{len(shops)} shops processed | {elapsed:.1f}s ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
