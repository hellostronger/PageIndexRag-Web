import json
import re

from server.services import llm as llm_config
from server.services.tree_service import (attach_node_text, find_nodes_by_ids,
                                          get_skeleton, load_pages, load_tree)

TREE_SEARCH_PROMPT = """You are given a question and a tree structure of a document.
Each node contains a node id, node title, and a corresponding summary.
Your task is to find all nodes that are likely to contain the answer to the question.

Question: {question}

Document tree structure:
{tree_skeleton}

Please reply in the following JSON format:
{{
    "thinking": "<Your thinking process on which nodes are relevant to the question>",
    "node_list": ["node_id_1", "node_id_2"]
}}
Directly return the final JSON structure. Do not output anything else."""

ANSWER_PROMPT = """Based on the following document excerpts, answer the user's question.
Cite the relevant section titles in your answer.

Question: {question}

Document excerpts:
{context}

Provide a comprehensive answer in the same language as the question, based only on the provided excerpts."""


def _extract_json(text: str) -> dict:
    """Extract JSON from LLM response text."""
    # Try to find JSON in code blocks first
    match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if match:
        text = match.group(1).strip()
    # Try to find raw JSON
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"thinking": text, "node_list": []}


def _client_kwargs() -> dict:
    """Credentials and the routed model name for LiteLLM."""
    settings = llm_config.llm_settings()
    llm_config.apply_to_environ(settings)
    kwargs = {}
    if settings["api_key"]:
        kwargs["api_key"] = settings["api_key"]
    if settings["api_base"]:
        kwargs["api_base"] = settings["api_base"]
    return kwargs


async def run_rag_pipeline(document_id: str, question: str, chat_history: list):
    """Async generator yielding (event_type, data) tuples for SSE."""
    import litellm

    from pageindex.utils import _quiet_litellm, _repair_litellm_types

    _repair_litellm_types()
    _quiet_litellm()

    settings = llm_config.llm_settings()
    model = llm_config.litellm_model(settings["chat_model"])
    backend = _client_kwargs()

    # Load tree and, for Flash documents, the page texts it points at
    tree = load_tree(document_id)
    pages = load_pages(document_id)
    skeleton = get_skeleton(tree)

    # --- Phase 1: Tree Search ---
    yield ("phase", {"phase": "tree_search", "message": "Searching document tree..."})

    search_prompt = TREE_SEARCH_PROMPT.format(
        question=question,
        tree_skeleton=json.dumps(skeleton["structure"], ensure_ascii=False, indent=2)
    )

    try:
        search_response = await litellm.acompletion(**{
            "model": model,
            "messages": [{"role": "user", "content": search_prompt}],
            "drop_params": True,
            "max_retries": 0,
            **backend,
        })
        search_text = search_response.choices[0].message.content
    except Exception as e:
        yield ("error", {"message": f"Tree search failed: {str(e)}"})
        return

    # Parse response
    parsed = _extract_json(search_text or "")
    thinking = parsed.get("thinking", "")
    node_ids = parsed.get("node_list", [])
    if isinstance(node_ids, str):
        node_ids = [node_ids]

    yield ("thinking", {"content": thinking})

    # Resolve node titles, and give each one the content its pages hold
    matched_nodes = attach_node_text(find_nodes_by_ids(tree, node_ids), pages)
    node_titles = [n.get("title", "Unknown") for n in matched_nodes]
    yield ("nodes_found", {
        "node_ids": node_ids,
        "node_titles": node_titles
    })

    if not matched_nodes:
        yield ("answer_chunk", {"content": "No relevant sections found in the document for this question."})
        yield ("done", {"referenced_nodes": [], "tokens_used": {}})
        return

    # --- Phase 2: Answer Generation with Streaming ---
    yield ("phase", {"phase": "generating_answer",
                     "message": f"Generating answer from {len(matched_nodes)} relevant sections..."})

    # Build context from matched nodes
    context_parts = []
    for node in matched_nodes:
        text = node.get("text") or node.get("summary", "")
        title = node.get("title", "Unknown")
        location = node.get("_location", "unknown location")
        context_parts.append(f"[{title}] ({location}):\n{text}")
    context = "\n\n---\n\n".join(context_parts)

    answer_prompt = ANSWER_PROMPT.format(question=question, context=context)

    # Build message list with history
    messages = []
    for msg in chat_history[-6:]:  # Last 6 messages for context
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": answer_prompt})

    try:
        stream = await litellm.acompletion(**{
            "model": model,
            "messages": messages,
            "drop_params": True,
            "max_retries": 0,
            "stream": True,
            **backend,
        })

        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield ("answer_chunk", {"content": delta.content})

    except Exception as e:
        yield ("error", {"message": f"Answer generation failed: {str(e)}"})
        return

    yield ("done", {
        "referenced_nodes": node_ids,
        "tokens_used": {}
    })