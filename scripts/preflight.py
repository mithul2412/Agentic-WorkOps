from __future__ import annotations

import os
import sys
from urllib import error as urlerror
from urllib import request as urlrequest
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT
ENV_FILE = PACKAGE_ROOT / ".env"
POLICY_FILE = PACKAGE_ROOT / "config" / "manager_policies.yaml"


def _load_env_fallback(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        parsed = os.path.expandvars(value.strip())
        if (parsed.startswith('"') and parsed.endswith('"')) or (parsed.startswith("'") and parsed.endswith("'")):
            parsed = parsed[1:-1]
        os.environ[key] = parsed


def _is_set(key: str) -> bool:
    return bool(os.getenv(key, "").strip())


def _provider(key: str, default: str) -> str:
    return os.getenv(key, default).strip().lower()


def _path_from_env(key: str) -> Path | None:
    value = os.getenv(key, "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    return path


def _check_manager(failures: list[str], warnings: list[str]) -> None:
    active_policies = _active_manager_policies()
    if not active_policies:
        failures.append(
            "No active manager providers found. Ensure config/manager_policies.yaml has manager policies (ollama/openrouter/groq/gemini)."
        )
        return

    providers = sorted({policy.get("provider", "") for policy in active_policies})
    unsupported = sorted(provider for provider in providers if provider not in {"openrouter", "groq", "gemini", "ollama"})
    if unsupported:
        failures.append(
            "Unsupported manager providers in runtime: "
            + ", ".join(unsupported)
            + ". Use ollama/openrouter/groq/gemini policies."
        )
        return

    provider_api_ready = {
        "openrouter": _is_set("OPENROUTER_API_KEY"),
        "groq": _is_set("GROQ_API_KEY"),
        "gemini": _is_set("GEMINI_API_KEY") or _is_set("GOOGLE_API_KEY"),
    }
    if "openrouter" in providers and not provider_api_ready["openrouter"]:
        warnings.append("OPENROUTER_API_KEY is missing; OpenRouter manager policies will be skipped.")
    if "groq" in providers and not provider_api_ready["groq"]:
        warnings.append("GROQ_API_KEY is missing; Groq manager policies will be skipped.")
    if "gemini" in providers and not provider_api_ready["gemini"]:
        warnings.append("GEMINI_API_KEY/GOOGLE_API_KEY is missing; Gemini manager policies will be skipped.")

    ollama_ready = False
    if "ollama" in providers:
        model_missing_ids: list[str] = []
        base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip() or "http://127.0.0.1:11434"
        for policy in active_policies:
            if policy.get("provider") != "ollama":
                continue
            model_env = str(policy.get("model_env", "")).strip()
            model_default = str(policy.get("model_default", "")).strip()
            model_name = os.getenv(model_env, "").strip() if model_env else ""
            if not model_name:
                model_name = model_default
            if not model_name:
                model_missing_ids.append(str(policy.get("id", "unknown")))

            base_url_env = str(policy.get("base_url_env", "")).strip()
            if base_url_env:
                configured = os.getenv(base_url_env, "").strip()
                if configured:
                    base_url = configured

        if model_missing_ids:
            failures.append(
                "Ollama manager policies missing model configuration for: " + ", ".join(model_missing_ids)
            )
        else:
            health_url = f"{base_url.rstrip('/')}/api/tags"
            try:
                req = urlrequest.Request(health_url, method="GET")
                with urlrequest.urlopen(req, timeout=5) as resp:  # noqa: S310
                    if resp.status >= 400:
                        raise RuntimeError(f"HTTP {resp.status}")
                ollama_ready = True
            except (urlerror.URLError, RuntimeError) as exc:
                failures.append(f"Ollama manager provider is configured but unreachable at {health_url}: {exc}")

    has_ready_provider = ollama_ready or any(provider_api_ready.get(provider, False) for provider in providers)
    if not has_ready_provider:
        failures.append(
            "No active manager provider is ready. Configure reachable Ollama and/or API credentials for OpenRouter/Groq/Gemini."
        )


def _active_manager_policies() -> list[dict[str, str]]:
    if not POLICY_FILE.exists():
        return []
    loaded = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        return []

    policies: dict[str, dict[str, str]] = {}
    for row in loaded.get("policies", []):
        if not isinstance(row, dict):
            continue
        policy_id = str(row.get("id", "")).strip()
        if not policy_id:
            continue
        provider = str(row.get("provider", "")).strip().lower()
        policy_type = str(row.get("type", "")).strip().lower()
        if not provider:
            if policy_type in {"llm_api", "sota_api"}:
                provider = "openrouter"
            else:
                provider = "unknown"
        policies[policy_id] = {
            "id": policy_id,
            "provider": provider,
            "model_env": str(row.get("model_env", "")).strip(),
            "model_default": str(row.get("model_default", "")).strip(),
            "base_url_env": str(row.get("base_url_env", "")).strip(),
        }

    selector = loaded.get("selector", {}) if isinstance(loaded.get("selector"), dict) else {}
    active_ids = []
    default_policy_id = str(selector.get("default_policy_id", "")).strip()
    if default_policy_id:
        active_ids.append(default_policy_id)
    for item in selector.get("candidate_policy_ids", []):
        text = str(item).strip()
        if text:
            active_ids.append(text)
    if not active_ids:
        active_ids = list(policies.keys())

    active_policies: list[dict[str, str]] = []
    for policy_id in active_ids:
        policy = policies.get(policy_id)
        if policy:
            active_policies.append(policy)
    return active_policies


def _check_jira(failures: list[str]) -> None:
    if _provider("JIRA_PROVIDER", "mock") != "real":
        return
    required = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
    missing = [key for key in required if not _is_set(key)]
    if missing:
        failures.append(f"Jira real mode missing env vars: {', '.join(missing)}")


def _check_bitbucket(failures: list[str]) -> None:
    if _provider("BITBUCKET_PROVIDER", "mock") != "real":
        return
    missing = []
    for key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO_SLUG", "BITBUCKET_API_BASE"):
        if not _is_set(key):
            missing.append(key)
    has_token = _is_set("BITBUCKET_API_TOKEN")
    has_basic = _is_set("BITBUCKET_USERNAME") and _is_set("BITBUCKET_APP_PASSWORD")
    if not has_token and not has_basic:
        missing.extend(["BITBUCKET_API_TOKEN or (BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD)"])
    for key in ("BITBUCKET_SOURCE_BRANCH", "BITBUCKET_DESTINATION_BRANCH"):
        if not _is_set(key):
            missing.append(key)
    if missing:
        failures.append(f"Bitbucket real mode missing env vars: {', '.join(missing)}")


def _check_confluence(failures: list[str]) -> None:
    if _provider("CONFLUENCE_PROVIDER", "mock") != "real":
        return
    required = ("CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN")
    missing = [key for key in required if not _is_set(key)]
    if missing:
        failures.append(f"Confluence real mode missing env vars: {', '.join(missing)}")


def _check_tavily(failures: list[str]) -> None:
    if _provider("TAVILY_PROVIDER", "mock") != "real":
        return
    if not _is_set("TAVILY_API_KEY"):
        failures.append("Tavily real mode missing env var: TAVILY_API_KEY")


def _check_google(failures: list[str], warnings: list[str]) -> None:
    calendar_provider = _provider("CALENDAR_PROVIDER", "google")
    email_provider = _provider("EMAIL_PROVIDER", "google")
    if calendar_provider != "google" and email_provider != "google":
        return

    secret = _path_from_env("GOOGLE_CLIENT_SECRET_FILE")
    if not secret:
        failures.append("GOOGLE_CLIENT_SECRET_FILE is required for Google Calendar/Gmail providers.")
    elif not secret.exists():
        failures.append(f"GOOGLE_CLIENT_SECRET_FILE does not exist: {secret}")

    token = _path_from_env("GOOGLE_TOKEN_FILE")
    if not token:
        warnings.append("GOOGLE_TOKEN_FILE is empty; OAuth token will be created on first auth flow.")
    elif not token.exists():
        warnings.append(f"GOOGLE_TOKEN_FILE does not exist yet (expected before first OAuth run): {token}")


def main() -> int:
    if ENV_FILE.exists():
        if load_dotenv:
            load_dotenv(dotenv_path=ENV_FILE, override=False)
        else:
            _load_env_fallback(ENV_FILE)

    failures: list[str] = []
    warnings: list[str] = []

    _check_manager(failures, warnings)
    _check_jira(failures)
    _check_bitbucket(failures)
    _check_confluence(failures)
    _check_tavily(failures)
    _check_google(failures, warnings)

    print("=== Runtime Preflight ===")
    print(f"env_file: {ENV_FILE}")
    print(f"jira_provider: {_provider('JIRA_PROVIDER', 'mock')}")
    print(f"bitbucket_provider: {_provider('BITBUCKET_PROVIDER', 'mock')}")
    print(f"confluence_provider: {_provider('CONFLUENCE_PROVIDER', 'mock')}")
    print(f"tavily_provider: {_provider('TAVILY_PROVIDER', 'mock')}")
    print(f"calendar_provider: {_provider('CALENDAR_PROVIDER', 'google')}")
    print(f"email_provider: {_provider('EMAIL_PROVIDER', 'google')}")

    if warnings:
        print("\nWarnings:")
        for item in warnings:
            print(f"- {item}")

    if failures:
        print("\nFailures:")
        for item in failures:
            print(f"- {item}")
        return 1

    print("\nPreflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
