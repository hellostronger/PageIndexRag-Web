import os
import sys
import json
import time
import hashlib
import asyncio
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict

from server.services import llm as llm_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = PROJECT_ROOT / "results"
UPLOADS_DIR = PROJECT_ROOT / "uploads"
METADATA_FILE = RESULTS_DIR / "_metadata.json"

# Ensure dirs exist
RESULTS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)


# ========== Progress Tracking ==========

@dataclass
class TaskProgress:
    status: str = "pending"
    stage: str = ""
    percent: int = 0
    message: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None
    waiters: list = field(default_factory=list)


class ProgressTracker:
    def __init__(self):
        self._tasks: Dict[str, TaskProgress] = {}

    def create(self, task_id: str):
        self._tasks[task_id] = TaskProgress()

    def get(self, task_id: str) -> Optional[TaskProgress]:
        return self._tasks.get(task_id)

    def update(self, task_id: str, stage: str, percent: int, message: str = ""):
        task = self._tasks.get(task_id)
        if not task:
            return
        task.stage = stage
        task.percent = percent
        task.message = message
        task.status = "processing"
        for event in task.waiters:
            event.set()

    def complete(self, task_id: str, result: dict = None):
        task = self._tasks.get(task_id)
        if not task:
            return
        task.status = "completed"
        task.percent = 100
        task.result = result
        for event in task.waiters:
            event.set()

    def fail(self, task_id: str, error: str):
        task = self._tasks.get(task_id)
        if not task:
            return
        task.status = "failed"
        task.error = error
        for event in task.waiters:
            event.set()

    async def subscribe(self, task_id: str):
        """Async generator yielding progress updates."""
        task = self._tasks.get(task_id)
        if not task:
            return

        last_percent = -1
        while task.status not in ("completed", "failed"):
            if task.percent != last_percent:
                last_percent = task.percent
                yield {
                    "stage": task.stage,
                    "percent": task.percent,
                    "message": task.message
                }
            event = asyncio.Event()
            task.waiters.append(event)
            try:
                await asyncio.wait_for(event.wait(), timeout=30)
            except asyncio.TimeoutError:
                # Send heartbeat
                yield {"stage": task.stage, "percent": task.percent, "message": "Still processing..."}
            finally:
                if event in task.waiters:
                    task.waiters.remove(event)

        # Yield final state
        if task.status == "completed":
            yield {"status": "completed", "percent": 100}
        else:
            yield {"status": "failed", "message": task.error}


progress_tracker = ProgressTracker()


# ========== Metadata Storage ==========

def generate_document_id(filename: str) -> str:
    raw = f"{filename}-{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def load_all_metadata() -> dict:
    if METADATA_FILE.exists():
        with open(METADATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_metadata(doc_id: str, meta: dict):
    all_meta = load_all_metadata()
    all_meta[doc_id] = meta
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(all_meta, f, ensure_ascii=False, indent=2)


def delete_metadata(doc_id: str):
    all_meta = load_all_metadata()
    all_meta.pop(doc_id, None)
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(all_meta, f, ensure_ascii=False, indent=2)


# ========== PDF Processing (subprocess) ==========

# Runs in a child interpreter: the kernel drives its own asyncio.run() and
# process pools, which cannot share a loop with uvicorn's. Everything it needs
# arrives as JSON — the tree and the page texts both come back the same way.
_PDF_INDEX_SCRIPT = r'''
import sys, json, os, re

sys.path.insert(0, r"{root}")

with open(r"{params}", "r", encoding="utf-8") as f:
    params = json.load(f)

# LiteLLM's openai/ provider reads both from the environment, so no connection
# kwargs have to reach every call inside the kernel.
if params.get("api_key"):
    os.environ["OPENAI_API_KEY"] = params["api_key"]
if params.get("api_base"):
    os.environ["OPENAI_BASE_URL"] = params["api_base"]

import PyPDF2

# PyPDF2 decodes broken ToUnicode maps with surrogatepass; lone surrogates
# cannot be encoded to UTF-8 and would crash the JSON save below.
_SURROGATES = re.compile("[\ud800-\udfff]")

def scrub(text):
    return _SURROGATES.sub("\ufffd", text)

pdf_path = params["pdf_path"]
with open(pdf_path, "rb") as f:
    reader = PyPDF2.PdfReader(f)
    page_texts = [scrub(page.extract_text() or "") for page in reader.pages]

if not any(t.strip() for t in page_texts):
    raise ValueError("PDF has no extractable text layer (scanned or image-only). "
                     "Run OCR on it first.")

model = params["model"]
summary_model = params.get("summary_model") or model

if params.get("mode") == "standard":
    from pageindex.page_index_classic import page_index_main
    from pageindex.utils import ConfigLoader, count_tokens

    opt = ConfigLoader().load(params["classic_opt"])
    tree = page_index_main(
        pdf_path,
        opt,
        page_list=[(text, count_tokens(text, model=model)) for text in page_texts],
    )
else:
    from pageindex.flash import page_index_flash
    from pageindex.flash.api import flash_rejection_reason

    tree = page_index_flash(
        pdf_path,
        summary=params.get("summary", True),
        summary_model=summary_model,
        optimize=params.get("optimize", "full"),
        optimize_model=params.get("optimize_model") or summary_model,
        summary_max_words=params.get("summary_max_words"),
        use_embedded_toc=params.get("use_embedded_toc", True),
    )
    reason = flash_rejection_reason(tree, standard_hint="mode=standard")
    if reason:
        raise ValueError(reason)

with open(r"{result}", "w", encoding="utf-8") as f:
    json.dump({"tree": tree, "pages": page_texts}, f, ensure_ascii=False)
print("DONE")
'''


async def process_pdf(file_path: str, params: dict) -> tuple:
    """Index a PDF with the 0.2.x kernel in a child interpreter.

    Returns ``(tree, page_texts)``. Flash mode keeps the page texts out of the
    tree and they are stored beside it instead, so a question only ever pays
    for the pages its reasoning landed on.
    """
    import subprocess
    import tempfile

    params = dict(params, pdf_path=file_path)

    opt_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8')
    json.dump(params, opt_file, ensure_ascii=False)
    opt_file.close()

    result_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8')
    result_file.close()

    script = _PDF_INDEX_SCRIPT.format(
        root=str(PROJECT_ROOT),
        params=opt_file.name,
        result=result_file.name,
    )

    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-c', script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    for path in (opt_file.name, result_file.name):
        try:
            os.unlink(path)
        except Exception:
            pass

    if proc.returncode != 0:
        error_msg = stderr.decode('utf-8', errors='replace').strip()
        # The kernel's own traceback is noise here; its last line is the reason.
        tail = [line for line in error_msg.splitlines() if line.strip()]
        raise RuntimeError(tail[-1] if tail else "PDF processing failed")

    with open(result_file.name, "r", encoding="utf-8") as f:
        payload = json.load(f)

    return payload["tree"], payload.get("pages") or []


async def process_markdown(file_path: str, params: dict) -> tuple:
    """md_to_tree is async and cooperates with our event loop.

    Markdown nodes carry their own text (``if_add_node_text``), so there is no
    separate page store to keep for them.
    """
    from pageindex.page_index_md import md_to_tree

    tree = await md_to_tree(
        md_path=file_path,
        if_thinning=params.get("if_thinning", False),
        min_token_threshold=params.get("min_token_threshold"),
        if_add_node_summary=params.get("if_add_node_summary", "yes"),
        summary_token_threshold=params.get("summary_token_threshold"),
        model=params.get("model"),
        summary_model=params.get("summary_model") or params.get("model"),
        if_add_doc_description=params.get("if_add_doc_description", "no"),
        if_add_node_text=params.get("if_add_node_text", "yes"),
        if_add_node_id=params.get("if_add_node_id", "yes"),
    )
    return tree, None


# ========== Orchestration ==========

def _index_params() -> dict:
    """Model roles and credentials, as the Settings UI left them."""
    return llm_config.llm_settings()


def _classic_opt(cfg: dict, settings: dict) -> dict:
    """The processing knobs standard mode reads (flash takes its own args)."""
    return {
        "model": settings["index_model"] or None,
        "summary_model": settings["summary_model"] or settings["index_model"] or None,
        "toc_check_page_num": cfg.get("toc_check_page_num", 20),
        "max_page_num_each_node": cfg.get("max_page_num_each_node", 10),
        "max_token_num_each_node": cfg.get("max_token_num_each_node", 20000),
        "if_add_node_id": cfg.get("if_add_node_id", "yes"),
        "if_add_node_summary": cfg.get("if_add_node_summary", "yes"),
        "if_add_doc_description": cfg.get("if_add_doc_description", "no"),
        "if_add_node_text": cfg.get("if_add_node_text", "no"),
    }


def _md_params(cfg: dict, settings: dict) -> dict:
    model = settings["index_model"]
    return {
        "model": model,
        "summary_model": settings["summary_model"] or model,
        "if_thinning": cfg.get("if_thinning", False),
        "min_token_threshold": cfg.get("min_token_threshold", 5000),
        "if_add_node_summary": cfg.get("if_add_node_summary", "yes"),
        "summary_token_threshold": cfg.get("summary_token_threshold", 200),
        "if_add_doc_description": cfg.get("if_add_doc_description", "no"),
        "if_add_node_text": "yes",
        "if_add_node_id": cfg.get("if_add_node_id", "yes"),
    }


async def orchestrate_processing(document_id: str, file_path: str, file_type: str):
    """Wraps the actual processing with estimated progress stages."""
    tracker = progress_tracker
    cfg = llm_config.read_config_yaml()
    settings = _index_params()

    tracker.update(document_id, "uploading", 5, "File received, starting processing...")
    await asyncio.sleep(0.5)

    try:
        if file_type == "pdf":
            mode = cfg.get("mode", "flash")
            label = "PageIndex Flash" if mode == "flash" else "standard mode"
            tracker.update(document_id, "extracting_text", 15,
                           f"Extracting page text, then indexing with {label}...")
            await asyncio.sleep(0.3)
            tracker.update(document_id, "analyzing_structure", 30,
                           "Writing node summaries with the indexing model...")

            params = {
                "api_key": settings["api_key"],
                "api_base": settings["api_base"],
                "model": settings["index_model"],
                "summary_model": settings["summary_model"],
                "mode": mode,
                "optimize": cfg.get("optimize", "full"),
                "summary": cfg.get("summary", True),
                "use_embedded_toc": cfg.get("use_embedded_toc", True),
                "summary_max_words": cfg.get("summary_max_words"),
                "classic_opt": _classic_opt(cfg, settings),
            }

            result, pages = await process_pdf(file_path, params)

        elif file_type == "markdown":
            tracker.update(document_id, "parsing_markdown", 15, "Parsing Markdown structure...")
            await asyncio.sleep(0.3)
            tracker.update(document_id, "building_tree", 30, "Building tree with LLM analysis...")

            result, pages = await process_markdown(file_path, _md_params(cfg, settings))

        else:
            # txt, json, csv, word → convert to Markdown first, then process
            from server.services.converter_service import convert_to_markdown

            type_labels = {
                "text": "TXT",
                "json": "JSON",
                "csv": "CSV",
                "word": "Word",
            }
            label = type_labels.get(file_type, file_type.upper())

            tracker.update(document_id, "converting", 10,
                           f"Converting {label} to Markdown...")
            await asyncio.sleep(0.3)

            # Convert to markdown
            md_path = str(UPLOADS_DIR / f"{document_id}_converted.md")
            convert_to_markdown(file_path, md_path, file_type)

            tracker.update(document_id, "parsing_markdown", 25,
                           f"Parsing converted Markdown structure...")
            await asyncio.sleep(0.3)
            tracker.update(document_id, "building_tree", 40,
                           "Building tree with LLM analysis...")

            result, pages = await process_markdown(md_path, _md_params(cfg, settings))

            # Clean up converted file
            try:
                os.unlink(md_path)
            except Exception:
                pass

        tracker.update(document_id, "saving", 90, "Saving results...")
        await asyncio.sleep(0.2)

        # Save result
        output_path = RESULTS_DIR / f"{document_id}.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        # Page texts live beside the tree: the chat layer pulls only the pages a
        # retrieved node spans.
        pages_path = RESULTS_DIR / f"{document_id}_pages.json"
        if pages:
            with open(pages_path, "w", encoding="utf-8") as f:
                json.dump(pages, f, ensure_ascii=False)
        elif pages_path.exists():
            pages_path.unlink()

        meta = load_all_metadata().get(document_id, {})
        meta["status"] = "completed"
        meta["page_count"] = len(pages) if pages else None
        save_metadata(document_id, meta)

        tracker.complete(document_id, result)

    except Exception as e:
        meta = load_all_metadata().get(document_id, {})
        meta["status"] = "failed"
        meta["error"] = str(e)
        save_metadata(document_id, meta)
        tracker.fail(document_id, str(e))