from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from dotenv import set_key

from server.services import llm as llm_config

router = APIRouter(prefix="/api/config", tags=["config"])

ENV_PATH = llm_config.ENV_PATH

# The knobs a 0.2.x index run reads. `mode` picks the pipeline: flash builds the
# tree from layout statistics and only asks the model for summaries; standard
# asks the model for the structure too.
PROCESSING_KEYS = (
    "mode", "optimize", "summary", "use_embedded_toc", "summary_max_words",
    "toc_check_page_num", "max_page_num_each_node", "max_token_num_each_node",
    "if_add_node_id", "if_add_node_summary", "if_add_doc_description",
    "if_add_node_text", "if_thinning", "min_token_threshold",
    "summary_token_threshold",
)

MODEL_KEYS = ("model", "index_model", "summary_model", "chat_model")


def _ensure_env() -> None:
    if not ENV_PATH.exists():
        ENV_PATH.write_text(
            "# PageIndex Web credentials. OPENAI_API_KEY also serves any\n"
            "# OpenAI-compatible endpoint; OPENAI_BASE_URL points at it.\n",
            encoding="utf-8",
        )


async def get_config():
    """Get current LLM and processing configuration."""
    settings = llm_config.llm_settings()
    cfg = llm_config.read_config_yaml()

    api_key = settings["api_key"]
    api_key_preview = f"...{api_key[-4:]}" if len(api_key) > 4 else ""

    index_model = settings["index_model"] or cfg.get("model", "")
    chat_model = settings["chat_model"] or cfg.get("model", "")

    processing = {
        key: cfg[key] for key in PROCESSING_KEYS if key in cfg
    }
    processing.setdefault("mode", "flash")
    processing.setdefault("optimize", "full")
    processing.setdefault("summary", True)
    processing.setdefault("use_embedded_toc", True)

    return {
        "llm": {
            # `model` stays for older builds of the settings form; it is the
            # index model, which is what the tree is built with.
            "model": index_model,
            "index_model": index_model,
            "summary_model": settings["summary_model"] or index_model,
            "chat_model": chat_model,
            "api_base_url": settings["api_base"],
            "api_key_set": bool(api_key),
            "api_key_preview": api_key_preview,
        },
        "processing": processing,
    }


async def update_config(request: Request):
    """Update configuration (partial update)."""
    body = await request.json()

    _ensure_env()

    # Update LLM settings
    llm = body.get("llm", {})
    if llm.get("api_key"):
        set_key(str(ENV_PATH), "OPENAI_API_KEY", llm["api_key"])
    if "api_base_url" in llm:
        base = (llm["api_base_url"] or "").strip()
        if base:
            set_key(str(ENV_PATH), "OPENAI_BASE_URL", base)
        else:
            _drop_env_key("OPENAI_BASE_URL")

    # Update config.yaml — only the three model roles; the processing knobs
    # live in `processing` and are handled below.
    cfg = llm_config.read_config_yaml()
    incoming = {key: llm[key] for key in MODEL_KEYS if llm.get(key)}
    if "model" in incoming:
        incoming.setdefault("index_model", incoming["model"])
        incoming.setdefault("chat_model", incoming["model"])
    for key, value in incoming.items():
        cfg[key] = value
    # `model` is the legacy all-roles spelling; keeping it in sync keeps an
    # older config.yaml readable by this app.
    cfg["model"] = incoming.get("index_model", cfg.get("index_model", cfg.get("model")))

    processing = body.get("processing", {})
    for key in PROCESSING_KEYS:
        if key in processing:
            cfg[key] = processing[key]

    llm_config.write_config_yaml(cfg)
    llm_config.load_env()

    return await get_config()


def _drop_env_key(key: str) -> None:
    """Remove a key from .env (python-dotenv has no unset)."""
    if not ENV_PATH.exists():
        return
    kept = [
        line for line in ENV_PATH.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith(f"{key}=")
    ]
    ENV_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")


async def test_connection(request: Request):
    """Test the LLM API connection, over the same path the app itself uses."""
    body = await request.json()
    llm = body.get("llm") or body

    api_key = llm.get("api_key") or llm_config.llm_settings()["api_key"]
    api_base = (llm.get("api_base_url")
                if "api_base_url" in llm else llm_config.llm_settings()["api_base"])
    model = llm.get("model") or llm.get("chat_model") or llm_config.llm_settings()["chat_model"]

    if not api_key:
        return {"success": False, "error": "API key is not set"}
    if not model:
        return {"success": False, "error": "Model is not set"}

    try:
        import time

        import litellm

        from pageindex.utils import _quiet_litellm, _repair_litellm_types

        _repair_litellm_types()
        _quiet_litellm()

        kwargs = {"api_key": api_key}
        if api_base:
            kwargs["api_base"] = api_base

        start = time.time()
        response = await litellm.acompletion(**{
            "model": llm_config.litellm_model(model),
            "messages": [{"role": "user", "content": "Hello, respond with just 'OK'."}],
            "drop_params": True,
            "max_retries": 0,
            # Reasoning models (GLM, o-series, DeepSeek R1 ...) spend the
            # first tokens on reasoning_content and leave content empty; a
            # small budget makes this probe report a blank reply from a
            # perfectly healthy endpoint.
            "max_tokens": 512,
            **kwargs,
        })
        latency = int((time.time() - start) * 1000)

        content = response.choices[0].message.content
        if not content:
            # Fall back to the reasoning trace so the probe still says
            # something truthful about a model that answered that way.
            content = (getattr(response.choices[0].message, "reasoning_content", None)
                       or "") or None

        return {
            "success": True,
            "latency_ms": latency,
            "model_name": model,
            "response": content,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


router.add_api_route("", get_config, methods=["GET"])
router.add_api_route("", update_config, methods=["PATCH"])
router.add_api_route("/test-connection", test_connection, methods=["POST"])