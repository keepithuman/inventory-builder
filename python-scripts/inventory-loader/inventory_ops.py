"""
Inventory Manager CRUD.

Existence checks, creation (with optional broker actions), platform-wide
inventory listing (session census), and node fetch/populate. Full-replace
semantics live in populate_inventory() -- see its docstring and the
KEY DESIGN CONSTRAINT section of main.py.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

log = logging.getLogger("itential_inventory_loader")

NODE_FETCH_PAGE_SIZE = 500
INVENTORY_FETCH_PAGE_SIZE = 100


def find_inventory(platform, name: str) -> dict | None:
    resp = platform.get("/inventory_manager/v1/inventories", params={"names": [name]})
    resp.raise_for_status()
    body = resp.json()
    results = (body.get("result") or {}).get("data", [])
    for inv in results:
        if inv.get("name") == name:
            return inv
    return None


def create_inventory(
    platform,
    name: str,
    groups: list[str],
    description: str = "",
    tags: list[str] | None = None,
    create_broker_actions: bool = False,
    cluster_id: str = "",
) -> dict:
    if not groups:
        raise ValueError(
            "At least one group is required to create an inventory "
            "(controls RBAC access to it)."
        )
    if create_broker_actions and not cluster_id:
        raise ValueError(
            "cluster_id is required when create_broker_actions is true -- it's the "
            "IAG cluster the four standard actions (get-config, set-config, "
            "run-command, is-alive) will be bound to."
        )
    payload: dict[str, Any] = {
        "name": name,
        "description": description,
        "groups": groups,
    }
    if tags:
        payload["tags"] = tags
    if create_broker_actions:
        payload["createBrokerActions"] = True
        payload["defaultClusterId"] = cluster_id

    resp = platform.post("/inventory_manager/v1/inventories", json=payload)
    resp.raise_for_status()
    body = resp.json()
    log.info(
        "Created inventory '%s'%s",
        name,
        f" with broker actions on cluster '{cluster_id}'" if create_broker_actions else "",
    )
    return body.get("result", body)


def ensure_inventory(
    platform,
    name: str,
    create_if_missing: bool,
    groups: list[str] | None = None,
    create_broker_actions: bool = False,
    cluster_id: str = "",
) -> dict:
    existing = find_inventory(platform, name)
    if existing:
        log.info("Inventory '%s' already exists -- will populate it.", name)
        return existing

    if not create_if_missing:
        raise ValueError(
            f"Inventory '{name}' does not exist and create_if_missing was "
            "not set. Refusing to guess -- pass create_if_missing=true to create it."
        )

    return create_inventory(
        platform,
        name,
        groups or [],
        create_broker_actions=create_broker_actions,
        cluster_id=cluster_id,
    )


def list_all_inventories(platform) -> list[dict]:
    """Pages through every inventory on the platform (used for the session census, not scoped to one inventory)."""
    all_inventories: list[dict] = []
    page = 1
    while True:
        resp = platform.get(
            "/inventory_manager/v1/inventories",
            params={"page": page, "pageSize": INVENTORY_FETCH_PAGE_SIZE},
        )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        data = result.get("data", [])
        all_inventories.extend(data)
        if page >= result.get("totalPages", 1) or not data:
            break
        page += 1
    return all_inventories


def fetch_all_nodes(platform, inventory_identifier: str) -> list[dict]:
    """Pages through every existing node in an inventory (may be 10k+)."""
    all_nodes: list[dict] = []
    page = 1
    while True:
        resp = platform.get(
            f"/inventory_manager/v1/inventories/{quote(inventory_identifier, safe='')}/nodes",
            params={"page": page, "pageSize": NODE_FETCH_PAGE_SIZE},
        )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        data = result.get("data", [])
        all_nodes.extend(data)
        if page >= result.get("totalPages", 1) or not data:
            break
        page += 1
    return all_nodes


def populate_inventory(platform, inventory_name: str, nodes: list[dict]) -> dict:
    """
    FULL-REPLACE, not an upsert: clears all existing nodes in the target inventory
    before inserting the new set. See main.py's KEY DESIGN CONSTRAINT docstring
    section for why every caller must submit the complete desired device list.
    """
    log.warning(
        "Populating '%s' with %d nodes -- this REPLACES all existing nodes "
        "in the inventory.",
        inventory_name,
        len(nodes),
    )
    resp = platform.post(
        "/inventory_manager/v1/nodes/bulk",
        json={"inventory_identifier": inventory_name, "nodes": nodes},
    )
    resp.raise_for_status()
    body = resp.json()

    stats = (body.get("result") or {}).get("statistics", {})
    log.info(
        "Populate result: total=%s inserted=%s skipped=%s errors=%d",
        stats.get("total"),
        stats.get("inserted"),
        stats.get("skipped"),
        len(stats.get("errors") or []),
    )
    for err in stats.get("errors") or []:
        log.error("  node error: %s", err)

    return stats
