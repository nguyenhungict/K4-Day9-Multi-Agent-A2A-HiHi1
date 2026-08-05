"""Model registry and OpenRouter client settings.

Model names live here, in source, on purpose: the brief requires them to be
declared in code and mirrored into metadata.json. `.env` holds the API key and
nothing else.

Every model listed is <=10B parameters, which is the hard cap in the brief.
"""

from __future__ import annotations

import os
from typing import Dict

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Catalogue of the <=10B OpenRouter models that support tool calling, so the
# parameter count is auditable straight from this file.
MODEL_CATALOG: Dict[str, Dict[str, object]] = {
    "meta-llama/llama-3.1-8b-instruct": {"parameters_b": 8, "context": 131072, "tools": True},
    "qwen/qwen-2.5-7b-instruct": {"parameters_b": 7, "context": 32768, "tools": True},
    "qwen/qwen3-8b": {"parameters_b": 8, "context": 131072, "tools": True},
    "ibm-granite/granite-4.1-8b": {"parameters_b": 8, "context": 131072, "tools": True},
    "mistralai/ministral-8b-2512": {"parameters_b": 8, "context": 262144, "tools": True},
    "nvidia/nemotron-nano-9b-v2:free": {"parameters_b": 9, "context": 128000, "tools": True},
}

# One model for the whole graph keeps the run reproducible and the report
# simple. Override per agent below if a role turns out to need something else.
PRIMARY_MODEL = "meta-llama/llama-3.1-8b-instruct"

AGENT_MODELS: Dict[str, str] = {
    "coordinator": PRIMARY_MODEL,
    "customer": PRIMARY_MODEL,
    "order_product": PRIMARY_MODEL,
    "payment": PRIMARY_MODEL,
    "delivery": PRIMARY_MODEL,
    "policy": PRIMARY_MODEL,
    "verifier": PRIMARY_MODEL,
}

# Deterministic decoding: the same case must produce the same verdict on a
# rerun, otherwise the trace is not reproducible evidence.
GENERATION = {
    "temperature": 0.0,
    "top_p": 1.0,
    "max_tokens": 1024,
    "seed": 20260805,
}

MAX_RETRIES = 4
REQUEST_TIMEOUT_S = 90


def get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is empty. Put your key in .env "
            "(copy .env.example) before running the agent pipeline."
        )
    return key


def build_headers() -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
    }
    title = os.environ.get("OPENROUTER_APP_TITLE", "").strip()
    site = os.environ.get("OPENROUTER_SITE_URL", "").strip()
    if title:
        headers["X-Title"] = title
    if site:
        headers["HTTP-Referer"] = site
    return headers


def models_in_use() -> Dict[str, Dict[str, object]]:
    """Model -> spec for every model the graph actually calls; feeds metadata.json."""
    return {name: MODEL_CATALOG[name] for name in sorted(set(AGENT_MODELS.values()))}


def assert_within_param_cap(cap_b: int = 10) -> None:
    """Guard the brief's <=10B rule at startup rather than at grading time."""
    for name, spec in models_in_use().items():
        if spec["parameters_b"] > cap_b:
            raise RuntimeError(f"{name} is {spec['parameters_b']}B, over the {cap_b}B cap")
