from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests

try:
    from langsmith import traceable
except Exception:  # pragma: no cover
    def traceable(*_args, **_kwargs):  # type: ignore[misc]
        def _decorator(func):
            return func

        return _decorator


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    temperature: float = 0.0
    max_tokens: int = 300
    expect_json: bool = False


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: int
    raw: dict[str, Any]


class LLMProviderError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        default_model: str,
        base_url: str | None = None,
        timeout_seconds: int = 90,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = (base_url or os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")
        self.default_model = default_model.strip()
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise LLMProviderError("OPENROUTER_API_KEY is missing")

    @traceable(name="openrouter_chat", run_type="llm")
    def chat(self, request: LLMRequest, model: str | None = None) -> LLMResponse:
        selected_model = (model or self.default_model).strip()
        if not selected_model:
            raise LLMProviderError("OpenRouter model is not configured")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        referer = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
        x_title = os.getenv("OPENROUTER_X_TITLE", "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        if x_title:
            headers["X-Title"] = x_title

        payload = {
            "model": selected_model,
            "messages": _openai_messages(system=request.system, user=request.user),
            "temperature": request.temperature,
            "stream": False,
            "max_tokens": request.max_tokens,
        }
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        text = _extract_openai_text(data)
        return LLMResponse(
            text=text,
            provider="openrouter",
            model=selected_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw=data,
        )


class GroqClient:
    def __init__(
        self,
        api_key: str,
        default_model: str,
        base_url: str | None = None,
        timeout_seconds: int = 90,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = (base_url or os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")).rstrip("/")
        self.default_model = default_model.strip()
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise LLMProviderError("GROQ_API_KEY is missing")

    @traceable(name="groq_chat", run_type="llm")
    def chat(self, request: LLMRequest, model: str | None = None) -> LLMResponse:
        selected_model = (model or self.default_model).strip()
        if not selected_model:
            raise LLMProviderError("Groq model is not configured")

        payload = {
            "model": selected_model,
            "messages": _openai_messages(system=request.system, user=request.user),
            "temperature": request.temperature,
            "stream": False,
            "max_tokens": request.max_tokens,
        }
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        text = _extract_openai_text(data)
        return LLMResponse(
            text=text,
            provider="groq",
            model=selected_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw=data,
        )


class GeminiDirectClient:
    def __init__(
        self,
        api_key: str,
        default_model: str,
        base_url: str | None = None,
        timeout_seconds: int = 90,
    ) -> None:
        resolved_key = api_key.strip() if api_key else ""
        if not resolved_key:
            resolved_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        self.api_key = resolved_key
        self.base_url = (
            base_url or os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
        ).rstrip("/")
        self.default_model = default_model.strip()
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise LLMProviderError("GEMINI_API_KEY/GOOGLE_API_KEY is missing")

    @traceable(name="gemini_direct_chat", run_type="llm")
    def chat(self, request: LLMRequest, model: str | None = None) -> LLMResponse:
        selected_model = (model or self.default_model).strip()
        if not selected_model:
            raise LLMProviderError("Gemini model is not configured")

        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": request.user}],
                }
            ],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        if request.system.strip():
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}

        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/models/{selected_model}:generateContent",
            params={"key": self.api_key},
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        text = _extract_gemini_text(data)
        return LLMResponse(
            text=text,
            provider="gemini",
            model=selected_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw=data,
        )


class OllamaClient:
    def __init__(
        self,
        default_model: str,
        base_url: str | None = None,
        timeout_seconds: int = 90,
    ) -> None:
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.default_model = default_model.strip()
        self.timeout_seconds = timeout_seconds

    @traceable(name="ollama_chat", run_type="llm")
    def chat(self, request: LLMRequest, model: str | None = None) -> LLMResponse:
        selected_model = (model or self.default_model).strip()
        if not selected_model:
            raise LLMProviderError("Ollama model is not configured")

        options: dict[str, Any] = {
            "temperature": request.temperature,
        }
        if request.max_tokens > 0:
            options["num_predict"] = request.max_tokens

        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": _openai_messages(system=request.system, user=request.user),
            "stream": False,
            "options": options,
        }
        if request.expect_json:
            payload["format"] = "json"

        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/api/chat",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        text = _extract_ollama_text(data)
        return LLMResponse(
            text=text,
            provider="ollama",
            model=selected_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw=data,
        )


def _openai_messages(system: str, user: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    return messages


def _extract_openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise LLMProviderError("provider response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMProviderError("provider response choice is malformed")
    message = first.get("message", {})
    if not isinstance(message, dict):
        raise LLMProviderError("provider response message is malformed")
    content = message.get("content", "")
    text = str(content).strip()
    if not text:
        raise LLMProviderError("provider response content is empty")
    return text


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list) or not candidates:
        raise LLMProviderError("Gemini response has no candidates")
    first = candidates[0]
    if not isinstance(first, dict):
        raise LLMProviderError("Gemini response candidate is malformed")
    content = first.get("content", {})
    if not isinstance(content, dict):
        raise LLMProviderError("Gemini response content is malformed")
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        raise LLMProviderError("Gemini response parts are malformed")
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    result = "\n".join(chunks).strip()
    if not result:
        raise LLMProviderError("Gemini response content is empty")
    return result


def _extract_ollama_text(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    response_text = payload.get("response")
    if isinstance(response_text, str) and response_text.strip():
        return response_text.strip()

    raise LLMProviderError("Ollama response content is empty")
