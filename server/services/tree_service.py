import json
import copy
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = PROJECT_ROOT / "results"

# Flash trees carry page ranges, not page text; the texts are stored beside
# the tree at index time so a question only reads the pages it lands on.
_PAGES_SUFFIX = "_pages.json"


def load_tree(document_id: str) -> dict:
    """Load the full tree JSON from results/."""
    # Try document_id.json first, then fallback to filename-based pattern
    tree_path = RESULTS_DIR / f"{document_id}.json"
    if not tree_path.exists():
        # Search by matching pattern in metadata
        tree_path = RESULTS_DIR / f"{document_id}_structure.json"
    if not tree_path.exists():
        raise FileNotFoundError(f"Tree not found for document: {document_id}")
    with open(tree_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_pages(document_id: str) -> list:
    """Page texts saved at index time, or [] when the document has none."""
    pages_path = RESULTS_DIR / f"{document_id}{_PAGES_SUFFIX}"
    if not pages_path.exists():
        return []
    with open(pages_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_skeleton(tree: dict) -> dict:
    """Return tree with 'text' fields stripped (recursive)."""
    def strip_text(node):
        result = {k: v for k, v in node.items() if k != "text"}
        if "nodes" in result:
            result["nodes"] = [strip_text(child) for child in result["nodes"]]
        return result

    return {
        "doc_name": tree.get("doc_name", ""),
        "structure": [strip_text(n) for n in tree.get("structure", [])]
    }


def find_nodes_by_ids(tree: dict, node_ids: list) -> list:
    """Find nodes by their IDs, including their text content."""
    results = []
    target_ids = set(node_ids)

    def walk(node):
        if node.get("node_id") in target_ids:
            results.append(node)
        for child in node.get("nodes", []):
            walk(child)

    for top_node in tree.get("structure", []):
        walk(top_node)

    return results


def _node_location(node: dict) -> str:
    """Where a node lives, phrased for a citation.

    PDF nodes span pages; Markdown nodes carry a line number instead.
    """
    start = node.get("start_index")
    end = node.get("end_index")
    if start is not None:
        return f"pages {start}-{end}" if end is not None and end != start else f"page {start}"
    line = node.get("line_num")
    return f"line {line}" if line is not None else "unknown location"


def attach_node_text(nodes: list, pages: list, max_pages_per_node: int = 12) -> list:
    """Give every retrieved node the content its pages hold.

    Markdown nodes already carry ``text``; a Flash tree does not, so its pages
    are joined here. A node wider than the cap keeps only its opening pages —
    the rest of a very long chapter is rarely what answers a question, and the
    model has to fit in a context window either way.
    """
    attached = []
    for node in nodes:
        enriched = copy.deepcopy(node)
        if not enriched.get("text") and pages:
            start = enriched.get("start_index")
            end = enriched.get("end_index", start)
            if start is not None:
                window = list(range(int(start), int(end) + 1))[:max_pages_per_node]
                enriched["text"] = "\n\n".join(
                    pages[i - 1] for i in window if 1 <= i <= len(pages)
                ).strip()
        enriched["_location"] = _node_location(node)
        attached.append(enriched)
    return attached


def get_node_by_id(tree: dict, node_id: str) -> dict | None:
    """Get a single node by its ID."""
    nodes = find_nodes_by_ids(tree, [node_id])
    return nodes[0] if nodes else None


def count_all_nodes(tree: dict) -> int:
    """Count total number of nodes in the tree."""
    count = 0

    def walk(node):
        nonlocal count
        count += 1
        for child in node.get("nodes", []):
            walk(child)

    for top_node in tree.get("structure", []):
        walk(top_node)

    return count