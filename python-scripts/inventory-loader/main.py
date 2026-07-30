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

OPTIONS BLOB
----------------
Only three top-level inputs ever exist: devices/input (the data), inventory_name
(required), and options -- a single JSON object holding every optional toggle
below. This keeps the CLI/decorator surface fixed at 3 inputs regardless of how
many toggles this script grows; adding one is a script-only change, no service
re-import required. Values inside `options` may be real JSON booleans (true) or
the strings "true"/"false" -- both work.

  options = {
    "create_if_missing":     bool, default false
    "groups":                str,  default ""      -- comma-separated
    "create_broker_actions": bool, default false    -- requires cluster_id
    "cluster_id":            str,  default ""
    "dry_run":               bool, default false
    "yes":                   bool, default false
    "preview":               bool, default false
    "include_backup":        bool, default false
    "backup_to":             str,  default null     -- local file path
    "diff_log_dir":          str,  default ""       -- path, or "auto"
  }

SAFETY AT SCALE
----------------
Because every populate call replaces the entire inventory, a bad or
truncated input (e.g. a partial export at 10k+ devices) silently wipes
everything that was there before. Before writing, the script:

  1. Checks (read-only) whether the target inventory exists yet and, if so,
     fetches its current node list -- neither creating nor writing anything.
     Computes an added/removed/unchanged diff against the incoming set, and
     whether the run would create the inventory (would_create_inventory),
     both included in the JSON result.
  2. Optionally backs up the current nodes to a local JSON file
     (options.backup_to) and/or embeds them in the JSON result
     (options.include_backup).
  3. Requires explicit confirmation before creating or writing anything
     (options.yes). Without it: interactive local runs get a y/N prompt;
     non-interactive runs (IAG, cron) get back a JSON result with
     action="confirmation_required" and the diff, and make NO changes at all
     -- not even creating the inventory -- the caller re-invokes with
     yes=true once it has reviewed the diff.
  4. options.preview asks for that same read-only diff explicitly, and
     always wins over options.yes -- useful when you want a live look at
     actual platform state (unlike dry_run, which never touches the
     platform) without any risk of also triggering a write in the same call.

DIFF CATEGORIES
----------------
The diff compares nodes by name AND by content. A name present in both the
current and incoming sets is "modified" if its attributes or tags differ,
"unchanged" only if they're identical -- same name alone is not enough to
call something unchanged.

SESSION CENSUS
----------------
Every run that authenticates (i.e. not dry_run, and past any pre-platform
validation error) also snapshots the full platform-wide inventory list
(count + names) once right after authenticating and again right before
printing the final result, and reports the delta as a "session" block. This
is independent of the per-inventory node diff above -- it catches "someone
else created/deleted an unrelated inventory while this ran."

DIFF LOG
----------------
options.diff_log_dir writes every computed diff to its own timestamped file
in that directory (diff-<inventory>-<UTC-timestamp>.json), building a
history across runs instead of only existing in that one run's JSON output.
Pass "auto" instead of a real path to get a fresh tempfile-backed
directory -- useful under IAG, where each run gets a freshly cloned working
directory and no path is guaranteed to exist or be writable across runs.
Opt-in only: omit it and nothing is written. Any file the script writes
(backup_to or diff_log_dir) is reported back with its actual byte size, so
growth at scale (thousands of devices) is visible rather than assumed.

BROKER ACTIONS AT CREATION
----------------
options.create_broker_actions (with options.cluster_id) auto-provisions the
four standard actions (get-config, set-config, run-command, is-alive) on a
newly created inventory, bound to that IAG cluster -- same mechanism as
`createBrokerActions`/`defaultClusterId` on the platform's create-inventory
endpoint. Only applies when the inventory doesn't exist yet; has no effect
otherwise.

IAG5 CONTRACT
----------------
When run as an IAG5 python-script service, every decorator property arrives
as a `--property_name value` CLI flag, and credentials arrive as env vars
from the service's `secrets` block. IAG treats this script's stdout as its
return value, so the script prints exactly one JSON object at the end of
every run (`{"success": bool, "action": ..., ...}`) and relies on stderr
(via `logging`) for progress/diagnostic output. Exit code is 0 for any
handled outcome (success, validation error, confirmation required) and 1
only for a fatal setup failure (missing platform credentials) -- see
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
      --options '{"create_if_missing": true, "groups": "netbox-sync-admins", "dry_run": true}'

  # Local, file-based, real run (skips the interactive prompt):
  python inventory.py \\
      --input devices.json \\
      --inventory_name "NetBox-Devices" \\
      --options '{"backup_to": "backups/netbox-devices-pre-sync.json", "yes": true}'

  # IAG5 / agent-flow style, devices passed inline as a JSON string. Only 3 flags
  # ever exist here: devices, inventory_name, and options -- IAG maps decorator
  # schema property names straight to CLI flags of the same spelling.
  python inventory.py \\
      --devices '[{"name":"core-sw-01","primary_ip":"10.0.0.1"}]' \\
      --inventory_name "NetBox-Devices" \\
      --options '{"create_if_missing": true, "groups": "netbox-sync-admins", "yes": true}'

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
import re
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import ipsdk

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("itential_inventory_loader")


def _bool(value) -> bool:
    """Accepts a real JSON bool (from the options blob) or a "true"/"false" string."""
    if isinstance(value, bool):
        return value
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
INVENTORY_FETCH_PAGE_SIZE = 100
DIFF_PREVIEW_LIMIT = 10


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


def diff_inventories(before: list[dict], after: list[dict]) -> dict:
    """Platform-wide census delta -- independent of the per-inventory node diff below."""
    before_names = {inv["name"] for inv in before}
    after_names = {inv["name"] for inv in after}
    return {
        "before_count": len(before_names),
        "after_count": len(after_names),
        "inventories_added": sorted(after_names - before_names),
        "inventories_removed": sorted(before_names - after_names),
    }


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

DEFAULT_OPTIONS = {
    "create_if_missing": False,
    "groups": "",
    "create_broker_actions": False,
    "cluster_id": "",
    "dry_run": False,
    "yes": False,
    "preview": False,
    "include_backup": False,
    "backup_to": None,
    "diff_log_dir": "",
}


def parse_options(raw: str | None) -> dict:
    """Parses the --options JSON blob and fills in defaults for anything omitted."""
    options = dict(DEFAULT_OPTIONS)
    if not raw:
        return options
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--options must be a JSON object.")
    unknown = set(parsed) - set(DEFAULT_OPTIONS)
    if unknown:
        raise ValueError(f"Unknown options key(s): {sorted(unknown)}. Valid keys: {sorted(DEFAULT_OPTIONS)}")
    options.update(parsed)
    return options


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
        "--options",
        default=None,
        help="JSON object with every optional toggle (create_if_missing, groups, "
        "create_broker_actions, cluster_id, dry_run, yes, preview, include_backup, "
        "backup_to, diff_log_dir) -- see the OPTIONS BLOB section of this module's "
        "docstring for the full shape and defaults. Omit for all defaults.",
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------
# Orchestration helpers -- each is one phase of main(), independently testable
# --------------------------------------------------------------------------

def resolve_options(options: dict) -> dict:
    """Converts the raw options dict (bools may be JSON bool or "true"/"false" strings) into typed values."""
    return {
        "create_if_missing": _bool(options["create_if_missing"]),
        "create_broker_actions": _bool(options["create_broker_actions"]),
        "cluster_id": options["cluster_id"] or "",
        "groups": [g.strip() for g in (options["groups"] or "").split(",") if g.strip()],
        "dry_run": _bool(options["dry_run"]),
        "yes": _bool(options["yes"]),
        "preview": _bool(options["preview"]),
        "include_backup": _bool(options["include_backup"]),
        "backup_to": options["backup_to"] or None,
        "diff_log_dir": options["diff_log_dir"] or "",
    }


def gather_diff_state(platform, inventory_name: str, nodes: list[dict]) -> tuple[bool, list[dict], dict]:
    """Read-only: checks existence, fetches current nodes, and computes the diff -- creates/writes nothing."""
    existing = find_inventory(platform, inventory_name)
    would_create_inventory = existing is None
    existing_nodes = fetch_all_nodes(platform, inventory_name) if not would_create_inventory else []

    diff = build_diff(existing_nodes, nodes)
    log.warning(
        "Planned change to '%s': would_create=%s existing=%d new=%d added=%d removed=%d modified=%d unchanged=%d",
        inventory_name,
        would_create_inventory,
        diff["existing_count"],
        diff["new_count"],
        diff["added_count"],
        diff["removed_count"],
        diff["modified_count"],
        diff["unchanged_count"],
    )
    return would_create_inventory, existing_nodes, diff


def apply_side_channels(existing_nodes: list[dict], diff: dict, inventory_name: str, opts: dict) -> tuple[dict | None, dict | None]:
    """Writes the optional backup file and diff-log file; returns their file_info (None if not requested)."""
    backup_to_info = None
    if opts["backup_to"] and existing_nodes:
        backup_nodes(existing_nodes, opts["backup_to"])
        backup_to_info = file_info(opts["backup_to"])

    diff_log_info = None
    if opts["diff_log_dir"]:
        diff_log_info = write_diff_log(diff, inventory_name, opts["diff_log_dir"])

    return backup_to_info, diff_log_info


def enrich_result(
    result: dict,
    existing_nodes: list[dict],
    opts: dict,
    backup_to_info: dict | None,
    diff_log_info: dict | None,
    session_before: list[dict],
    platform,
) -> dict:
    """Adds the optional backup/backup_to_file/diff_log/session fields shared by both result shapes below."""
    if opts["include_backup"] and existing_nodes:
        result["backup"] = existing_nodes
    if backup_to_info:
        result["backup_to_file"] = backup_to_info
    if diff_log_info:
        result["diff_log"] = diff_log_info
    result["session"] = diff_inventories(session_before, list_all_inventories(platform))
    return result


def perform_write(platform, inventory_name: str, nodes: list[dict], opts: dict) -> dict:
    """Creates the inventory if needed and populates it. Raises ValueError on a handled validation failure."""
    ensure_inventory(
        platform,
        inventory_name,
        create_if_missing=opts["create_if_missing"],
        groups=opts["groups"],
        create_broker_actions=opts["create_broker_actions"],
        cluster_id=opts["cluster_id"],
    )
    return populate_inventory(platform, inventory_name, nodes)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        opts = resolve_options(parse_options(args.options))
        devices = load_devices(args)
        nodes = build_nodes(devices)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 0

    log.info("Prepared %d nodes from %d input device records.", len(nodes), len(devices))

    if opts["dry_run"]:
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

    # Session census: platform-wide inventory count/names, snapshotted once now and
    # again right before printing the final result -- independent of the per-inventory
    # node diff below. Cheap: inventory counts are small even when node counts aren't.
    session_before = list_all_inventories(platform)

    would_create_inventory, existing_nodes, diff = gather_diff_state(platform, args.inventory_name, nodes)
    backup_to_info, diff_log_info = apply_side_channels(existing_nodes, diff, args.inventory_name, opts)

    # preview=true always wins, even if yes=true was also passed: it's an explicit
    # "show me, don't touch anything" request, not just a missing confirmation.
    proceed = (not opts["preview"]) and should_proceed(opts["yes"])

    if not proceed:
        result: dict[str, Any] = {
            "success": False,
            "action": "preview" if opts["preview"] else "confirmation_required",
            "would_create_inventory": would_create_inventory,
            "diff": diff,
        }
        result = enrich_result(result, existing_nodes, opts, backup_to_info, diff_log_info, session_before, platform)
        print(json.dumps(result))
        return 0

    try:
        stats = perform_write(platform, args.inventory_name, nodes, opts)
    except ValueError as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 0

    result = {
        "success": not bool(stats.get("errors")),
        "action": "populated",
        "inventory_name": args.inventory_name,
        "created_inventory": would_create_inventory,
        "diff": diff,
        "populate": stats,
    }
    result = enrich_result(result, existing_nodes, opts, backup_to_info, diff_log_info, session_before, platform)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
