"""
Diff computation and file-based artifacts.

Content-aware node diff (added/removed/modified/unchanged, not just
name-aware), the platform-wide inventory-list delta (session census), and
file writers (backup, timestamped diff log) that report their own byte size.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

log = logging.getLogger("itential_inventory_loader")

DIFF_PREVIEW_LIMIT = 10


def build_diff(existing_nodes: list[dict], new_nodes: list[dict]) -> dict:
    """
    Structured added/removed/modified/unchanged diff -- safe to embed in the JSON
    result even at 10k+ nodes. A name present in both sets is "modified" if its
    attributes or tags differ, "unchanged" only if they're identical -- matching
    names alone doesn't mean nothing changed.
    """
    existing_by_name = {n["name"]: n for n in existing_nodes}
    new_by_name = {n["name"]: n for n in new_nodes}
    existing_names = set(existing_by_name)
    new_names = set(new_by_name)

    added = sorted(new_names - existing_names)
    removed = sorted(existing_names - new_names)

    modified = []
    unchanged = []
    for name in sorted(existing_names & new_names):
        old_node = existing_by_name[name]
        new_node = new_by_name[name]
        same = (
            (old_node.get("attributes") or {}) == (new_node.get("attributes") or {})
            and sorted(old_node.get("tags") or []) == sorted(new_node.get("tags") or [])
        )
        (unchanged if same else modified).append(name)

    return {
        "existing_count": len(existing_names),
        "new_count": len(new_names),
        "added_count": len(added),
        "removed_count": len(removed),
        "modified_count": len(modified),
        "unchanged_count": len(unchanged),
        "added_preview": added[:DIFF_PREVIEW_LIMIT],
        "removed_preview": removed[:DIFF_PREVIEW_LIMIT],
        "modified_preview": modified[:DIFF_PREVIEW_LIMIT],
    }


def diff_inventories(before: list[dict], after: list[dict]) -> dict:
    """Platform-wide census delta -- independent of the per-inventory node diff above."""
    before_names = {inv["name"] for inv in before}
    after_names = {inv["name"] for inv in after}
    return {
        "before_count": len(before_names),
        "after_count": len(after_names),
        "inventories_added": sorted(after_names - before_names),
        "inventories_removed": sorted(before_names - after_names),
    }


def backup_nodes(nodes: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(nodes, f, indent=2)
    log.info("Backed up %d existing node(s) to %s", len(nodes), path)


def file_info(path: str) -> dict:
    """Actual on-disk size of a file this script wrote -- so growth at scale is observed, not assumed."""
    return {"path": path, "bytes": os.path.getsize(path)}


def write_diff_log(diff: dict, inventory_name: str, dir_path: str) -> dict:
    """
    Writes one timestamped diff snapshot per call, building a history across runs.
    dir_path="auto" resolves to a fresh tempfile-backed directory -- useful under IAG,
    where each run gets a freshly cloned working directory with no guaranteed-writable
    path across runs.
    """
    if dir_path.strip().lower() == "auto":
        dir_path = tempfile.mkdtemp(prefix="inventory-diff-")
    else:
        os.makedirs(dir_path, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", inventory_name)
    path = os.path.join(dir_path, f"diff-{safe_name}-{timestamp}.json")

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"inventory_name": inventory_name, "timestamp": timestamp, "diff": diff}, f, indent=2)
    log.info("Wrote diff log to %s", path)
    return file_info(path)
