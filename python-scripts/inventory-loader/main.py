#!/usr/bin/env python3
"""
itential_inventory_loader.py

Loads a normalized list of devices into an Itential Platform Inventory Manager
inventory. Device sourcing (NetBox or otherwise) is intentionally out of scope --
this script starts from a JSON array of already-extracted device records,
either inline (--devices) or from a file (--input).

Runs two ways:
  1. Locally, from a file, for ad-hoc/scheduled syncs.
  2. As an IAG5 python-script service, invoked from an Itential workflow or
     FlowAI agent with `devices` passed inline as a JSON string param -- see
     IAG5 CONTRACT below.

KEY DESIGN CONSTRAINT
----------------------
Inventory Manager's bulk-load endpoint (POST /inventory_manager/v1/nodes/bulk)
is FULL-REPLACE, not an upsert: every call clears all existing nodes in the
target inventory before inserting the new set. There is no incremental/partial
update. Practical implications:

  1. Every run must submit the COMPLETE desired device list in one call.
     Do not chunk/paginate the populate call -- each chunk would wipe out the
     previous one.
  2. This script is meant to be run as a full sync (e.g. on a schedule), not
     as a "diff and patch" tool.
  3. Creating an inventory (POST /inventory_manager/v1/inventories) is a
     separate, one-time step from populating it. The script checks for the
     inventory's existence and only creates it if missing.

SAFETY AT SCALE
----------------
Because every populate call replaces the entire inventory, a bad or
truncated input (e.g. a partial export at 10k+ devices) silently wipes
everything that was there before. Before writing, the script:

  1. Fetches the current node list and computes an added/removed/unchanged
     diff against the incoming set (included in the JSON result).
  2. Optionally backs up the current nodes to a local JSON file (--backup-to)
     and/or embeds them in the JSON result (--include-backup true).
  3. Requires explicit confirmation before the replace (--yes true). Without
     it: interactive local runs get a y/N prompt; non-interactive runs (IAG,
     cron) get back a JSON result with action="confirmation_required" and the
     diff, and make no changes -- the caller re-invokes with --yes true once
     it has reviewed the diff.

IAG5 CONTRACT
----------------
When run as an IAG5 python-script service, every decorator property arrives
as a `--property_name value` CLI flag, and credentials arrive as env vars
from the service's `secrets` block. IAG treats this script's stdout as its
return value, so the script prints exactly one JSON object at the end of
every run (`{"success": bool, "action": ..., ...}`) and relies on stderr
(via `logging`) for progress/diagnostic output. Booleans are passed as the
strings "true"/"false" since CLI flags carry no native type. Exit code is 0
for any handled outcome (success, validation error, confirmation required)
and 1 only for a fatal setup failure (missing platform credentials) -- see
build_platform_client().

AUTH
----
OAuth2 client-credentials (Itential service account) is the default and
recommended mode -- create the service account once (see
create_service_account() below, or do it once via an admin session), then
supply the resulting client_id/client_secret via environment variables (or,
under IAG, via the service's `secrets` block targeting these same names).
Basic auth (username/password) is supported as a fallback for quick testing.

Environment variables (all optional -- see build_platform_client()):
  ITENTIAL_HOST            e.g. platform.example.com
  ITENTIAL_PORT             default: 443
  ITENTIAL_VERIFY_TLS       "true"/"false", default: true
  ITENTIAL_CLIENT_ID
  ITENTIAL_CLIENT_SECRET
  ITENTIAL_USER
  ITENTIAL_PASSWORD

Usage:
  # Local, file-based, dry run:
  python inventory.py \\
      --input devices.json \\
      --inventory_name "NetBox-Devices" \\
      --create_if_missing true \\
      --groups netbox-sync-admins \\
      --dry_run true

  # Local, file-based, real run (skips the interactive prompt):
  python inventory.py \\
      --input devices.json \\
      --inventory_name "NetBox-Devices" \\
      --backup-to backups/netbox-devices-pre-sync.json \\
      --yes true

  # IAG5 / agent-flow style, devices passed inline as a JSON string. Flag names
  # are underscored (--inventory_name, not --inventory-name) because IAG maps
  # decorator schema property names straight to CLI flags of the same spelling:
  python inventory.py \\
      --devices '[{"name":"core-sw-01","primary_ip":"10.0.0.1"}]' \\
      --inventory_name "NetBox-Devices" \\
      --create_if_missing true \\
      --groups netbox-sync-admins \\
      --yes true

Device record shape (field names are whatever your extraction step
produces -- adjust `transform_device` to match):

  [
    {
      "name": "core-sw-01",
      "primary_ip": "10.0.0.1",
      "device_type": "Cisco Catalyst 9300",
      "manufacturer": "Cisco",
      "site": "DAL01",
      "role": "core-switch",
      "platform": "ios",
      "serial": "FCW1234A0BC",
      "status": "active",
      "tags": ["prod", "core"]
    },
    ...
  ]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any
from urllib.parse import quote

import ipsdk

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("itential_inventory_loader")


def _bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


# --------------------------------------------------------------------------
# Auth / client setup
# --------------------------------------------------------------------------

def build_platform_client():
    """
    Builds an ipsdk Platform client from environment variables.
    Prefers OAuth2 client-credentials (service account) if client_id/secret
    are present; falls back to basic auth otherwise.

    Raises RuntimeError (a fatal setup failure, not a business-logic error)
    if no usable credentials are found.
    """
    host = os.environ.get("ITENTIAL_HOST", "localhost")
    port = int(os.environ.get("ITENTIAL_PORT", "0"))
    verify = os.environ.get("ITENTIAL_VERIFY_TLS", "true").lower() != "false"
    client_id = os.environ.get("ITENTIAL_CLIENT_ID")
    client_secret = os.environ.get("ITENTIAL_CLIENT_SECRET")
    user = os.environ.get("ITENTIAL_USER")
    password = os.environ.get("ITENTIAL_PASSWORD")

    if client_id and client_secret:
        log.info("Authenticating to %s via OAuth2 client-credentials", host)
        return ipsdk.platform_factory(
            host=host,
            port=port,
            verify=verify,
            client_id=client_id,
            client_secret=client_secret,
        )

    if user and password:
        log.warning(
            "Authenticating to %s via basic auth -- switch to a service "
            "account (ITENTIAL_CLIENT_ID/ITENTIAL_CLIENT_SECRET) for "
            "production/scheduled runs.",
            host,
        )
        return ipsdk.platform_factory(
            host=host, port=port, verify=verify, user=user, password=password
        )

    raise RuntimeError(
        "No credentials found. Set ITENTIAL_CLIENT_ID/ITENTIAL_CLIENT_SECRET "
        "(preferred) or ITENTIAL_USER/ITENTIAL_PASSWORD."
    )


def create_service_account(platform, name: str, description: str = "") -> dict:
    """
    One-time helper: creates a service account using an already-authenticated
    (e.g. basic-auth admin) platform client, and returns the client_id/secret.
    Run this once, store the output somewhere safe, then switch subsequent
    runs to OAuth2 using build_platform_client().
    """
    resp = platform.post(
        "/oauth/serviceAccounts",
        json={"accountData": {"name": name, "description": description}},
    )
    resp.raise_for_status()
    data = resp.json()
    log.info("Created service account '%s' (client_id=%s)", name, data.get("client_id"))
    return data


# --------------------------------------------------------------------------
# Inventory existence / creation (separate from populate -- NOT an upsert)
# --------------------------------------------------------------------------

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
) -> dict:
    if not groups:
        raise ValueError(
            "At least one group is required to create an inventory "
            "(controls RBAC access to it)."
        )
    payload: dict[str, Any] = {
        "name": name,
        "description": description,
        "groups": groups,
    }
    if tags:
        payload["tags"] = tags

    resp = platform.post("/inventory_manager/v1/inventories", json=payload)
    resp.raise_for_status()
    body = resp.json()
    log.info("Created inventory '%s'", name)
    return body.get("result", body)


def ensure_inventory(
    platform,
    name: str,
    create_if_missing: bool,
    groups: list[str] | None = None,
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

    return create_inventory(platform, name, groups or [])


# --------------------------------------------------------------------------
# Transform: arbitrary device record -> Itential node schema
# --------------------------------------------------------------------------

def transform_device(device: dict) -> dict:
    """
    Maps a raw device record to the Itential node schema:
        { "name": str, "attributes": {...}, "tags": [str] }

    Adjust field mapping here to match whatever your device-sourcing step
    (NetBox or otherwise) actually produces. `name` is the only required
    field on the Itential side.
    """
    name = device.get("name")
    if not name:
        raise ValueError(f"Device record missing required 'name' field: {device}")

    # Everything else becomes a free-form attribute bag.
    attributes = {
        k: v
        for k, v in device.items()
        if k not in ("name", "tags") and v is not None
    }

    tags = device.get("tags") or []

    return {"name": name, "attributes": attributes, "tags": tags}


def build_nodes(devices: list[dict]) -> list[dict]:
    nodes = []
    seen_names = set()
    for device in devices:
        node = transform_device(device)
        if node["name"] in seen_names:
            log.warning("Duplicate device name '%s' -- keeping first, skipping dup.", node["name"])
            continue
        seen_names.add(node["name"])
        nodes.append(node)
    return nodes


def load_devices(args: argparse.Namespace) -> list:
    if args.devices is not None:
        devices = json.loads(args.devices)
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            devices = json.load(f)

    if not isinstance(devices, list):
        raise ValueError("Device input must be a JSON array of device records.")
    return devices


# --------------------------------------------------------------------------
# Safety net for full-replace at scale: diff preview, backup, confirmation
# --------------------------------------------------------------------------

NODE_FETCH_PAGE_SIZE = 500
DIFF_PREVIEW_LIMIT = 10


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


def build_diff(existing_nodes: list[dict], new_nodes: list[dict]) -> dict:
    """Structured added/removed/unchanged diff -- safe to embed in the JSON result even at 10k+ nodes."""
    existing_names = {n["name"] for n in existing_nodes}
    new_names = {n["name"] for n in new_nodes}
    added = sorted(new_names - existing_names)
    removed = sorted(existing_names - new_names)
    unchanged = existing_names & new_names

    return {
        "existing_count": len(existing_names),
        "new_count": len(new_names),
        "added_count": len(added),
        "removed_count": len(removed),
        "unchanged_count": len(unchanged),
        "added_preview": added[:DIFF_PREVIEW_LIMIT],
        "removed_preview": removed[:DIFF_PREVIEW_LIMIT],
    }


def backup_nodes(nodes: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(nodes, f, indent=2)
    log.info("Backed up %d existing node(s) to %s", len(nodes), path)


def should_proceed(explicit_yes: bool) -> bool:
    if explicit_yes:
        return True
    if not sys.stdin.isatty():
        # No controlling terminal (IAG, cron, CI) -- never block on input().
        return False
    answer = input("Type 'yes' to proceed with this full-replace: ").strip().lower()
    return answer == "yes"


# --------------------------------------------------------------------------
# Populate (full replace)
# --------------------------------------------------------------------------

def populate_inventory(platform, inventory_name: str, nodes: list[dict]) -> dict:
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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--devices",
        help="JSON array of device records, passed inline. Used by IAG5/agent/workflow callers "
        "that have no shared filesystem with this script.",
    )
    source.add_argument(
        "--input",
        help="Path to a JSON file with the device list. Used for local/file-based runs.",
    )

    p.add_argument("--inventory_name", required=True, help="Target Inventory Manager inventory name.")
    p.add_argument(
        "--create_if_missing",
        choices=["true", "false"],
        default="false",
        help="Create the inventory if it doesn't already exist.",
    )
    p.add_argument(
        "--groups",
        default="",
        help="Comma-separated group name(s) granted access when creating a new inventory "
        "(required if creating).",
    )
    p.add_argument(
        "--dry_run",
        choices=["true", "false"],
        default="false",
        help="Transform and validate only -- do not call the platform at all.",
    )
    p.add_argument(
        "--yes",
        choices=["true", "false"],
        default="false",
        help="Confirm the full-replace. Without it: local interactive runs get a y/N prompt; "
        "non-interactive callers (IAG, cron) get back a confirmation_required result instead "
        "of writing anything.",
    )
    p.add_argument(
        "--include_backup",
        choices=["true", "false"],
        default="false",
        help="Embed the pre-replace node list in the JSON result (in addition to --backup-to, if set). "
        "Use for IAG/agent callers that can't read a local file back.",
    )
    p.add_argument(
        "--backup-to",
        metavar="PATH",
        help="Also write the inventory's current nodes to this local JSON file before replacing them "
        "(local/file-based runs only -- not reachable by IAG/agent callers).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    create_if_missing = _bool(args.create_if_missing)
    dry_run = _bool(args.dry_run)
    confirmed = _bool(args.yes)
    include_backup = _bool(args.include_backup)
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]

    try:
        devices = load_devices(args)
        nodes = build_nodes(devices)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 0

    log.info("Prepared %d nodes from %d input device records.", len(nodes), len(devices))

    if dry_run:
        print(json.dumps({
            "success": True,
            "action": "dry_run",
            "node_count": len(nodes),
            "nodes_preview": nodes[:3],
        }))
        return 0

    try:
        platform = build_platform_client()
    except RuntimeError as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 1

    try:
        ensure_inventory(
            platform,
            args.inventory_name,
            create_if_missing=create_if_missing,
            groups=groups,
        )
        existing_nodes = fetch_all_nodes(platform, args.inventory_name)
    except ValueError as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 0

    diff = build_diff(existing_nodes, nodes)
    log.warning(
        "Planned change to '%s': existing=%d new=%d added=%d removed=%d unchanged=%d",
        args.inventory_name,
        diff["existing_count"],
        diff["new_count"],
        diff["added_count"],
        diff["removed_count"],
        diff["unchanged_count"],
    )

    if args.backup_to and existing_nodes:
        backup_nodes(existing_nodes, args.backup_to)

    if not should_proceed(confirmed):
        result: dict[str, Any] = {"success": False, "action": "confirmation_required", "diff": diff}
        if include_backup and existing_nodes:
            result["backup"] = existing_nodes
        print(json.dumps(result))
        return 0

    stats = populate_inventory(platform, args.inventory_name, nodes)

    result = {
        "success": not bool(stats.get("errors")),
        "action": "populated",
        "inventory_name": args.inventory_name,
        "diff": diff,
        "populate": stats,
    }
    if include_backup and existing_nodes:
        result["backup"] = existing_nodes
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
