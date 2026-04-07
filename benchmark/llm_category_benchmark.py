"""
LLM Category Mapping Benchmark — Gemma 4 vs Mistral Small vs Gemma 3

Tests multiple models on the same category mapping task via OpenRouter.
Uses high-confidence rule-based mappings as gold standard for accuracy measurement.

Usage:
    python benchmark/llm_category_benchmark.py
    python benchmark/llm_category_benchmark.py --samples 100  # more test samples
    python benchmark/llm_category_benchmark.py --models gemma4-26b  # single model
"""
import os
import sys
import json
import time
import argparse

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobs.db import get_conn, put_conn

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Models to benchmark
MODELS = {
    "gemma4-26b": {
        "id": "google/gemma-4-26b-a4b-it",
        "label": "Gemma 4 26B MoE",
        "cost_input": 0.13,
        "cost_output": 0.40,
        "json_mode": False,  # Gemma returns single object in json_mode, use raw
    },
    "gemma4-31b": {
        "id": "google/gemma-4-31b-it",
        "label": "Gemma 4 31B Dense",
        "cost_input": 0.14,
        "cost_output": 0.40,
        "json_mode": False,
    },
    "mistral-small": {
        "id": "mistralai/mistral-small-3.1-24b-instruct",
        "label": "Mistral Small 3.1 24B",
        "cost_input": 0.03,
        "cost_output": 0.11,
        "json_mode": True,
    },
    "gemma3-27b": {
        "id": "google/gemma-3-27b-it",
        "label": "Gemma 3 27B",
        "cost_input": 0.08,
        "cost_output": 0.16,
        "json_mode": False,
    },
    "gemma3-27b-free": {
        "id": "google/gemma-3-27b-it:free",
        "label": "Gemma 3 27B (free)",
        "cost_input": 0.0,
        "cost_output": 0.0,
        "json_mode": False,
    },
}

DEFAULT_SAMPLES = 50
BATCH_SIZE = 25  # categories per API call
TIMEOUT = 60


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_master_categories() -> list[dict]:
    """Load all master product categories."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, slug, parent_id
                FROM product_categories
                ORDER BY id
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


def load_gold_set(n: int) -> list[dict]:
    """
    Load N category mappings with high confidence (gold standard).
    Uses rule-based exact/keyword matches as ground truth.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cm.source_category, cm.target_category_id,
                       pc.name AS target_name,
                       cm.confidence, cm.mapping_type
                FROM category_mappings cm
                JOIN product_categories pc ON pc.id = cm.target_category_id
                WHERE cm.confidence >= 0.85
                  AND cm.target_category_id IS NOT NULL
                  AND cm.mapping_type IN ('shop:exact_name', 'shop:exact_slug',
                                          'shop:keyword', 'google:exact_name',
                                          'google:exact_slug', 'google:keyword')
                ORDER BY RANDOM()
                LIMIT %s
            """, (n,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# OpenRouter call
# ---------------------------------------------------------------------------
def call_openrouter(model_id: str, prompt: str, json_mode: bool = True) -> dict:
    """
    Call OpenRouter API. Returns dict with:
    - text: response text
    - input_tokens, output_tokens, total_tokens
    - latency_ms
    - error: error message if failed
    """
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://growlead.cz",
        "X-Title": "EcomRadar Benchmark",
    }

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 8192,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    start = time.time()
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=TIMEOUT)
        latency_ms = int((time.time() - start) * 1000)

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10))
            print(f"    Rate limited, waiting {wait}s...", flush=True)
            time.sleep(wait)
            # Retry once
            start = time.time()
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=TIMEOUT)
            latency_ms = int((time.time() - start) * 1000)

        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "latency_ms": latency_ms}

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return {"error": "No choices in response", "latency_ms": latency_ms}

        text = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})

        return {
            "text": text,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "latency_ms": latency_ms,
            "model_actual": data.get("model", model_id),
        }
    except requests.RequestException as e:
        return {"error": str(e), "latency_ms": int((time.time() - start) * 1000)}


# ---------------------------------------------------------------------------
# Benchmark execution
# ---------------------------------------------------------------------------
def build_prompt(master_cats: list[dict], test_categories: list[dict]) -> str:
    """Build the category mapping prompt (same as production)."""
    master_context = json.dumps(
        [{"id": mc["id"], "name": mc["name"]} for mc in master_cats],
        ensure_ascii=False,
    )

    source_context = json.dumps(
        [{"source_id": i, "name": tc["source_category"]}
         for i, tc in enumerate(test_categories)],
        ensure_ascii=False,
    )

    return (
        "You are a beauty product category classifier.\n\n"
        "Master taxonomy:\n"
        f"{master_context}\n\n"
        f"Map ALL {len(test_categories)} source categories below to the most appropriate master category.\n"
        "You MUST return a JSON array with EXACTLY one entry per source category.\n"
        "Format: [{\"source_id\": 0, \"master_id\": 17, \"confidence\": 0.95}, {\"source_id\": 1, \"master_id\": 3, \"confidence\": 0.85}, ...]\n"
        "Rules:\n"
        "- confidence: 0.95+ for exact match, 0.80-0.94 for good match, below 0.80 for uncertain\n"
        "- If no reasonable match exists, set master_id to null and confidence to 0.0\n"
        "- Consider that names may be in Czech, Slovak, German, French, Italian, Spanish, Polish, etc.\n"
        f"- Return EXACTLY {len(test_categories)} items in the JSON array\n\n"
        "Source categories to map:\n"
        f"{source_context}"
    )


def parse_response(text: str) -> list[dict]:
    """Parse JSON array from LLM response. Handles edge cases."""
    if not text:
        return []
    text = text.strip()
    # Handle markdown code blocks
    if "```" in text:
        import re
        match = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
        if match:
            text = match.group(1)

    try:
        data = json.loads(text)
        # Direct array
        if isinstance(data, list):
            return data
        # Single object → wrap in array
        if isinstance(data, dict):
            if "mappings" in data:
                return data["mappings"]
            if "source_id" in data:
                return [data]
        return []
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find array in text
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    # Try to find single object
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict) and "source_id" in obj:
                return [obj]
        except (json.JSONDecodeError, ValueError):
            pass

    return []


def evaluate_batch(predictions: list[dict], gold: list[dict]) -> dict:
    """
    Evaluate predictions against gold standard.
    Returns accuracy metrics.
    """
    # Build gold lookup: source_id → expected_master_id
    gold_lookup = {i: g["target_category_id"] for i, g in enumerate(gold)}

    correct = 0
    incorrect = 0
    missing = 0
    total = len(gold)

    pred_lookup = {p.get("source_id"): p for p in predictions}

    for i in range(total):
        expected = gold_lookup.get(i)
        pred = pred_lookup.get(i)

        if pred is None:
            missing += 1
            continue

        predicted = pred.get("master_id")
        if predicted == expected:
            correct += 1
        else:
            incorrect += 1

    return {
        "total": total,
        "correct": correct,
        "incorrect": incorrect,
        "missing": missing,
        "accuracy": round(correct / total * 100, 1) if total > 0 else 0,
        "coverage": round((total - missing) / total * 100, 1) if total > 0 else 0,
    }


def run_benchmark(model_key: str, model_config: dict, master_cats: list[dict],
                  gold_set: list[dict]) -> dict:
    """Run benchmark for a single model. Returns results dict."""
    model_id = model_config["id"]
    label = model_config["label"]

    print(f"\n{'='*60}", flush=True)
    print(f"  Model: {label} ({model_id})", flush=True)
    print(f"{'='*60}", flush=True)

    all_predictions = []
    all_eval = {"total": 0, "correct": 0, "incorrect": 0, "missing": 0}
    total_input_tokens = 0
    total_output_tokens = 0
    latencies = []
    errors = 0

    # Process in batches
    for i in range(0, len(gold_set), BATCH_SIZE):
        batch = gold_set[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(gold_set) - 1) // BATCH_SIZE + 1

        prompt = build_prompt(master_cats, batch)
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} cats, ~{len(prompt)} chars)...", flush=True)

        use_json_mode = model_config.get("json_mode", True)
        result = call_openrouter(model_id, prompt, json_mode=use_json_mode)

        if result.get("error"):
            print(f"    ERROR: {result['error']}", flush=True)
            errors += 1
            latencies.append(result.get("latency_ms", 0))
            continue

        latencies.append(result["latency_ms"])
        total_input_tokens += result.get("input_tokens", 0)
        total_output_tokens += result.get("output_tokens", 0)

        predictions = parse_response(result.get("text", ""))
        print(f"    → {len(predictions)} predictions, {result['latency_ms']}ms, "
              f"{result.get('input_tokens', 0)}+{result.get('output_tokens', 0)} tokens", flush=True)

        # Evaluate
        batch_eval = evaluate_batch(predictions, batch)
        print(f"    → Accuracy: {batch_eval['accuracy']}% ({batch_eval['correct']}/{batch_eval['total']}), "
              f"Coverage: {batch_eval['coverage']}%", flush=True)

        all_predictions.extend(predictions)
        all_eval["total"] += batch_eval["total"]
        all_eval["correct"] += batch_eval["correct"]
        all_eval["incorrect"] += batch_eval["incorrect"]
        all_eval["missing"] += batch_eval["missing"]

        # Brief pause between batches
        if i + BATCH_SIZE < len(gold_set):
            time.sleep(1)

    # Calculate final metrics
    accuracy = round(all_eval["correct"] / all_eval["total"] * 100, 1) if all_eval["total"] > 0 else 0
    coverage = round((all_eval["total"] - all_eval["missing"]) / all_eval["total"] * 100, 1) if all_eval["total"] > 0 else 0

    # Cost estimate
    cost_input = (total_input_tokens / 1_000_000) * model_config["cost_input"]
    cost_output = (total_output_tokens / 1_000_000) * model_config["cost_output"]
    total_cost = cost_input + cost_output

    # Latency stats
    avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0
    p95_latency = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) >= 2 else avg_latency

    return {
        "model_key": model_key,
        "model_id": model_id,
        "label": label,
        "accuracy": accuracy,
        "coverage": coverage,
        "correct": all_eval["correct"],
        "incorrect": all_eval["incorrect"],
        "missing": all_eval["missing"],
        "total": all_eval["total"],
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "cost_usd": round(total_cost, 4),
        "avg_latency_ms": avg_latency,
        "p95_latency_ms": p95_latency,
        "errors": errors,
        "cost_per_1k_cats": round(total_cost / (all_eval["total"] / 1000), 4) if all_eval["total"] > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="LLM Category Mapping Benchmark")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help=f"Number of test categories (default: {DEFAULT_SAMPLES})")
    parser.add_argument("--models", nargs="+", default=list(MODELS.keys()),
                        choices=list(MODELS.keys()),
                        help="Models to benchmark")
    args = parser.parse_args()

    if not OPENROUTER_API_KEY:
        print("FATAL: OPENROUTER_API_KEY not set", flush=True)
        sys.exit(1)

    print(f"=== LLM Category Mapping Benchmark ===", flush=True)
    print(f"Samples: {args.samples}, Models: {args.models}", flush=True)

    # Load data
    print("\nLoading master categories...", flush=True)
    master_cats = load_master_categories()
    print(f"  {len(master_cats)} master categories", flush=True)

    print(f"Loading gold set ({args.samples} samples)...", flush=True)
    gold_set = load_gold_set(args.samples)
    print(f"  {len(gold_set)} gold samples loaded", flush=True)

    if len(gold_set) < 10:
        print("WARNING: Very few gold samples — results may not be reliable", flush=True)

    # Run benchmarks
    results = []
    for model_key in args.models:
        model_config = MODELS[model_key]
        result = run_benchmark(model_key, model_config, master_cats, gold_set)
        results.append(result)

    # Print summary
    print(f"\n{'='*80}", flush=True)
    print(f"  BENCHMARK RESULTS", flush=True)
    print(f"{'='*80}", flush=True)
    print(f"{'Model':<30s} {'Acc%':>6s} {'Cov%':>6s} {'Tokens':>8s} {'Cost $':>8s} {'$/1K':>8s} {'Lat ms':>8s} {'Errs':>5s}", flush=True)
    print(f"{'-'*80}", flush=True)

    for r in sorted(results, key=lambda x: -x["accuracy"]):
        print(
            f"{r['label']:<30s} "
            f"{r['accuracy']:>5.1f}% "
            f"{r['coverage']:>5.1f}% "
            f"{r['total_tokens']:>8d} "
            f"{r['cost_usd']:>7.4f} "
            f"{r['cost_per_1k_cats']:>7.4f} "
            f"{r['avg_latency_ms']:>7d} "
            f"{r['errors']:>5d}",
            flush=True,
        )

    print(f"{'-'*80}", flush=True)

    # Save results
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nDetailed results saved to {output_path}", flush=True)

    # Winner
    best = max(results, key=lambda x: x["accuracy"])
    cheapest = min(results, key=lambda x: x["cost_per_1k_cats"] if x["cost_per_1k_cats"] > 0 else float("inf"))
    print(f"\nBest accuracy: {best['label']} ({best['accuracy']}%)", flush=True)
    print(f"Cheapest: {cheapest['label']} (${cheapest['cost_per_1k_cats']}/1K categories)", flush=True)


if __name__ == "__main__":
    main()
