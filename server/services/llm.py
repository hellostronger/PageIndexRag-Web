"""Single source of truth for LLM credentials and model routing.

PageIndex 0.2.x talks to models through LiteLLM, not through a bare OpenAI
client. This module keeps the Settings UI (``.env`` + ``pageindex/config.yaml``)
and the two call sites (the indexing subprocess, the chat pipeline) speaking the
same dialect:

* credentials become ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``, which LiteLLM's
  ``openai/`` provider reads on its own, so no connection kwargs have to be
  threaded through every call;
* model names are normalised by the kernel's own ``_litellm_model``, so a bare
  ``gpt-4o`` becomes ``openai/gpt-4o`` while ``anthropic/claude-...`` stays on
  its own provider.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
CONFIG_YAML_PATH = PROJECT_ROOT / "pageindex" / "config.yaml"


def load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH, override=True)


def read_config_yaml() -> dict:
    import yaml

    if not CONFIG_YAML_PATH.exists():
        return {}
    with open(CONFIG_YAML_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_config_yaml(data: dict) -> None:
    import yaml

    with open(CONFIG_YAML_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def llm_settings() -> dict:
    """Credentials and the three model roles, as the UI edits them.

    ``index_model`` builds the tree and writes node summaries, ``chat_model``
    answers questions. Both default to the kernel's own defaults, so an
    unconfigured install still starts.
    """
    load_env()
    cfg = read_config_yaml()
    return {
        "api_key": os.getenv("OPENAI_API_KEY") or os.getenv("CHATGPT_API_KEY", ""),
        # API_BASE_URL is the 0.1.x spelling and still honoured; an empty
        # string means "official endpoint", which LiteLLM expresses by the
        # variable simply being absent.
        "api_base": (os.getenv("OPENAI_BASE_URL") or os.getenv("API_BASE_URL") or ""),
        "index_model": cfg.get("index_model") or cfg.get("model") or "",
        "summary_model": cfg.get("summary_model") or "",
        "chat_model": cfg.get("chat_model") or cfg.get("model") or "",
    }


def apply_to_environ(settings: dict) -> None:
    """Publish credentials so LiteLLM picks them up without explicit kwargs."""
    if settings.get("api_key"):
        os.environ["OPENAI_API_KEY"] = settings["api_key"]
    if settings.get("api_base"):
        os.environ["OPENAI_BASE_URL"] = settings["api_base"]
    else:
        os.environ.pop("OPENAI_BASE_URL", None)


def litellm_model(model: str) -> str:
    """``gpt-4o`` -> ``openai/gpt-4o``; ``anthropic/claude-...`` unchanged.

    Delegates to the kernel so this app rejects an unknown provider prefix with
    the same message, and with the same fix, as the SDK itself.
    """
    from pageindex.utils import _litellm_model

    return _litellm_model(model)