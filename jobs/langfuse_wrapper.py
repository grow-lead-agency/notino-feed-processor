"""
Langfuse LLM observability wrapper for Notino feed-processor.

Centralizes all LLM calls (Gemini, OpenAI/Crawl4AI) with automatic tracing.
Every call logs: model, prompt, response, tokens, latency, cost, errors.

Usage:
    from jobs.langfuse_wrapper import traced_gemini_call, traced_generation, get_langfuse

    # For Gemini HTTP calls:
    response_text = traced_gemini_call(
        name="category-mapping",
        prompt=prompt_text,
        model="gemini-2.5-flash",
        metadata={"batch": 1, "source": "category_mapper"},
    )

    # For manual generation tracking (e.g. Crawl4AI):
    with traced_generation(name="crawl4ai-extract", model="openai/gpt-4o-mini",
                           input_data={"url": url, "prompt": prompt}) as gen:
        result = do_extraction(url)
        gen.end(output=result)

Env vars (all optional — degrades gracefully if not set):
    LANGFUSE_SECRET_KEY  — Langfuse secret key
    LANGFUSE_PUBLIC_KEY  — Langfuse public key
    LANGFUSE_HOST        — Langfuse base URL (default: https://langfuse.growlead.cz)
"""

import json
import os
import time
from contextlib import contextmanager

# Lazy-loaded Langfuse client
_langfuse = None
_langfuse_enabled = None


def _is_enabled() -> bool:
    """Check if Langfuse keys are configured."""
    global _langfuse_enabled
    if _langfuse_enabled is None:
        _langfuse_enabled = bool(
            os.environ.get("LANGFUSE_SECRET_KEY")
            and os.environ.get("LANGFUSE_PUBLIC_KEY")
        )
        if not _langfuse_enabled:
            print("[langfuse] WARNING: LANGFUSE_SECRET_KEY or LANGFUSE_PUBLIC_KEY not set — tracing disabled", flush=True)
    return _langfuse_enabled


def get_langfuse():
    """Get or create singleton Langfuse client. Returns None if not configured."""
    global _langfuse
    if not _is_enabled():
        return None
    if _langfuse is not None:
        return _langfuse
    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "https://langfuse.growlead.cz"),
            flush_at=50,
            flush_interval=5.0,
        )
        print("[langfuse] Client initialized", flush=True)
        return _langfuse
    except ImportError:
        print("[langfuse] WARNING: langfuse package not installed — tracing disabled", flush=True)
        _langfuse_enabled = False
        return None
    except Exception as e:
        print(f"[langfuse] WARNING: Failed to init client: {e} — tracing disabled", flush=True)
        _langfuse_enabled = False
        return None


def traced_gemini_call(
    *,
    name: str,
    prompt: str,
    gemini_url: str,
    gemini_api_key: str,
    model: str = "gemini-2.5-flash",
    timeout: int = 60,
    max_retries: int = 2,
    metadata: dict | None = None,
) -> str | None:
    """
    Call Gemini API with automatic Langfuse tracing.

    Replaces bare _call_gemini() functions. Handles retries, rate limits,
    and logs everything to Langfuse (prompt, response, latency, tokens, errors).

    Returns response text or None on failure.
    """
    import requests

    lf = get_langfuse()
    trace = None
    generation = None

    if lf:
        trace = lf.trace(
            name=f"gemini/{name}",
            metadata=metadata or {},
            tags=["feed-processor", "gemini"],
        )
        generation = trace.generation(
            name=name,
            model=model,
            input=prompt,
            metadata=metadata or {},
        )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }

    start_time = time.time()
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                gemini_url,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
                params={"key": gemini_api_key} if "?key=" not in gemini_url else None,
            )

            if resp.status_code == 429:
                wait = min(2 ** attempt * 5, 30)
                print(f"[{name}] Gemini 429 rate limit, waiting {wait}s (attempt {attempt})", flush=True)
                time.sleep(wait)
                last_error = f"429 rate limit (attempt {attempt})"
                continue

            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                print(f"[{name}] Gemini {last_error}", flush=True)
                if generation:
                    generation.end(
                        output=None,
                        level="ERROR",
                        status_message=last_error,
                        usage={"total_tokens": 0},
                    )
                return None

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                last_error = "No candidates in response"
                print(f"[{name}] Gemini returned no candidates", flush=True)
                if generation:
                    generation.end(
                        output=None,
                        level="WARNING",
                        status_message=last_error,
                    )
                return None

            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            latency_ms = int((time.time() - start_time) * 1000)

            # Extract token usage if available
            usage_metadata = data.get("usageMetadata", {})
            usage = {}
            if usage_metadata:
                usage = {
                    "input": usage_metadata.get("promptTokenCount", 0),
                    "output": usage_metadata.get("candidatesTokenCount", 0),
                    "total": usage_metadata.get("totalTokenCount", 0),
                }

            if generation:
                generation.end(
                    output=text[:2000],  # truncate for Langfuse storage
                    usage=usage or None,
                    completion_start_time=None,
                    metadata={
                        **(metadata or {}),
                        "latency_ms": latency_ms,
                        "attempt": attempt,
                        "response_length": len(text),
                    },
                )

            return text

        except requests.exceptions.Timeout:
            last_error = f"Timeout (attempt {attempt}/{max_retries})"
            print(f"[{name}] Gemini {last_error}", flush=True)
        except requests.exceptions.RequestException as e:
            last_error = f"Request error: {e}"
            print(f"[{name}] Gemini {last_error}", flush=True)
            if generation:
                generation.end(
                    output=None,
                    level="ERROR",
                    status_message=last_error,
                )
            return None

    # All retries exhausted
    if generation:
        generation.end(
            output=None,
            level="ERROR",
            status_message=f"All {max_retries} retries exhausted. Last: {last_error}",
        )
    return None


@contextmanager
def traced_generation(
    *,
    name: str,
    model: str,
    input_data: dict | str | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
):
    """
    Context manager for tracking non-Gemini LLM calls (e.g. Crawl4AI/OpenAI).

    Usage:
        with traced_generation(name="crawl4ai-shipping", model="openai/gpt-4o-mini",
                               input_data={"url": url}) as gen:
            result = crawl4ai_scrape(url, schema, prompt)
            gen.end(output=result, usage={"input": 500, "output": 200})

    The yielded object has an .end() method. If Langfuse is disabled,
    the context manager yields a no-op stub.
    """

    class NoOpGen:
        def end(self, **kwargs):
            pass

        def update(self, **kwargs):
            pass

    lf = get_langfuse()
    if not lf:
        yield NoOpGen()
        return

    trace = lf.trace(
        name=f"llm/{name}",
        metadata=metadata or {},
        tags=tags or ["feed-processor"],
    )
    generation = trace.generation(
        name=name,
        model=model,
        input=input_data,
        metadata=metadata or {},
    )

    start_time = time.time()

    class GenWrapper:
        def __init__(self, gen):
            self._gen = gen
            self._ended = False

        def end(self, *, output=None, usage=None, level=None, status_message=None):
            if self._ended:
                return
            self._ended = True
            latency_ms = int((time.time() - start_time) * 1000)
            kwargs = {"metadata": {**(metadata or {}), "latency_ms": latency_ms}}
            if output is not None:
                # Truncate large outputs
                if isinstance(output, str) and len(output) > 2000:
                    kwargs["output"] = output[:2000]
                elif isinstance(output, dict):
                    kwargs["output"] = json.dumps(output)[:2000]
                else:
                    kwargs["output"] = output
            if usage:
                kwargs["usage"] = usage
            if level:
                kwargs["level"] = level
            if status_message:
                kwargs["status_message"] = status_message
            self._gen.end(**kwargs)

        def update(self, **kwargs):
            self._gen.update(**kwargs)

    wrapper = GenWrapper(generation)
    try:
        yield wrapper
    except Exception as e:
        wrapper.end(level="ERROR", status_message=str(e))
        raise
    finally:
        # Auto-end if not manually ended
        if not wrapper._ended:
            wrapper.end(level="WARNING", status_message="Generation not explicitly ended")


def flush():
    """Flush any buffered Langfuse events. Call at end of job runs."""
    lf = get_langfuse()
    if lf:
        lf.flush()
