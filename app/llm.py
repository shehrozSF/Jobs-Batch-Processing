"""A small client for Cerebras' OpenAI-style /chat/completions endpoint."""

import copy
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel


class LLMError(Exception):
    """Base for errors from the model API."""


class RetryableError(LLMError):
    """Temporary: rate limit, server error, timeout. Worth trying again."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class FatalError(LLMError):
    """Every remaining call would fail the same way: bad key, no billing, daily
    quota spent. The batch should stop, not retry."""


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's JSON schema in the form strict mode accepts: $refs inlined,
    every property required (nullable ones may still be null), no extra keys."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
            return walk({**target, **{k: v for k, v in node.items() if k != "$ref"}})
        out = {}
        for key, value in node.items():
            if key in ("title", "default"):  # annotation keywords, not field names
                continue
            if key == "properties":  # keep field names, walk their schemas
                out[key] = {name: walk(sub) for name, sub in value.items()}
            else:
                out[key] = walk(value)
        if "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(schema)


class CerebrasClient:
    def __init__(self, settings, limiter=None, http: httpx.Client | None = None):
        self.settings = settings
        self.limiter = limiter
        self.http = http or httpx.Client(timeout=settings.request_timeout)

    def complete(self, system: str, messages: list[dict], schema: dict) -> Completion:
        if self.limiter is not None:
            self.limiter.acquire()
        payload = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "job_posting", "strict": True, "schema": schema},
            },
            "temperature": 0,
        }
        try:
            response = self.http.post(
                f"{self.settings.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
            )
        except httpx.HTTPError as exc:  # timeouts, dropped connections
            raise RetryableError(f"network error: {exc}") from exc

        self._raise_for_status(response)
        body = response.json()
        try:
            choice = body["choices"][0]
            usage = body["usage"]
            text = choice["message"]["content"]
            input_tokens, output_tokens = usage["prompt_tokens"], usage["completion_tokens"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RetryableError(f"malformed response: {str(body)[:300]}") from exc
        if choice.get("finish_reason") != "stop":
            raise RetryableError(f"incomplete answer (finish_reason={choice.get('finish_reason')})")
        return Completion(text, input_tokens, output_tokens)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        message = f"HTTP {status}: {response.text[:300]}"
        if status == 429:
            if response.headers.get("x-ratelimit-remaining-requests-day") == "0":
                raise FatalError(f"daily request quota spent. {message}")
            retry_after = response.headers.get("retry-after")
            raise RetryableError(message, float(retry_after) if retry_after else 60.0)
        if status >= 500:
            raise RetryableError(message)
        # 401 bad key, 402 billing, 400 bad request: repeating it changes nothing.
        raise FatalError(message)
