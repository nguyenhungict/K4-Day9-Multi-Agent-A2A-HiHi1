"""Minimal OpenRouter chat client with tool calling.

Uses urllib so the project has no third-party runtime dependency. Every call is
returned with its token accounting and latency attached so the orchestrator can
write an honest trace.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from . import llm_config

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def load_env(path: str) -> None:
    """Read `.env` into os.environ without adding a dependency.

    Existing environment variables win, so CI or a shell export can override
    the file. Values are never logged.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


class LLMError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(self, model: str, generation: Optional[Dict] = None):
        self.model = model
        self.generation = dict(generation or llm_config.GENERATION)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0
        # Cases run concurrently and share one client per agent role.
        self._counter_lock = threading.Lock()

    def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None,
             tool_choice: Optional[str] = None,
             json_object: bool = False) -> Dict:
        payload: Dict = {
            "model": self.model,
            "messages": messages,
            **self.generation,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        if json_object:
            payload["response_format"] = {"type": "json_object"}

        started = time.time()
        response = self._post(payload)
        latency_ms = int((time.time() - started) * 1000)

        usage = response.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        with self._counter_lock:
            self.total_prompt_tokens += prompt_tokens
            self.total_completion_tokens += completion_tokens
            self.call_count += 1

        message = (response.get("choices") or [{}])[0].get("message") or {}
        return {
            "message": message,
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls") or [],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
            "finish_reason": (response.get("choices") or [{}])[0].get("finish_reason"),
        }

    def _post(self, payload: Dict) -> Dict:
        url = f"{llm_config.OPENROUTER_BASE_URL}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        last_error = None

        for attempt in range(llm_config.MAX_RETRIES):
            request = urllib.request.Request(url, data=body, headers=llm_config.build_headers())
            try:
                with urllib.request.urlopen(request, timeout=llm_config.REQUEST_TIMEOUT_S) as resp:
                    parsed = json.loads(resp.read().decode("utf-8"))
                # OpenRouter can return HTTP 200 with an error envelope.
                if "error" in parsed and not parsed.get("choices"):
                    raise LLMError(str(parsed["error"])[:300])
                return parsed
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code == 402:
                    raise LLMError(
                        "OpenRouter returned 402 (out of credit). Top up at "
                        "https://openrouter.ai/settings/credits and rerun."
                    ) from exc
                if exc.code not in _RETRYABLE_STATUS:
                    raise LLMError(last_error) from exc
            except LLMError:
                raise
            except Exception as exc:  # network/timeout
                last_error = f"{type(exc).__name__}: {exc}"

            # Exponential backoff with jitter; 429s are common on shared keys.
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.75))

        raise LLMError(f"exhausted {llm_config.MAX_RETRIES} attempts — {last_error}")


def parse_json_content(content: Optional[str]) -> Optional[Dict]:
    """Best-effort JSON extraction from a small model's reply."""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None
