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

Module layout (all in the same IAG `working-directory`, imported as siblings):
  main.py            -- this file: CLI parsing, options resolution, orchestration
  platform_client.py -- auth + HTTP client (stdlib only -- no pip dependencies)
  inventory_ops.py    -- Inventory Manager CRUD + session census
  transform.py        -- device record -> node schema, device-list loading
  diff_utils.py        -- diff computation, backup/diff-log file writers

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
platform_client.build_platform_client().

AUTH
----
OAuth2 client-credentials (Itential service account) is the default and
recommended mode -- create the service account once via an admin session,
then supply the resulting client_id/client_secret via environment variables
(or, under IAG, via the service's `secrets` block targeting these same
names). Basic auth (username/password) is supported as a fallback for
quick testing.

Environment variables (all optional -- see platform_client.build_platform_client()):
  ITENTIAL_HOST            e.g. platform.example.com
  ITENTIAL_PORT             default: 443
  ITENTIAL_VERIFY_TLS       "true"/"false", default: true
  ITENTIAL_CLIENT_ID
  ITENTIAL_CLIENT_SECRET
  ITENTIAL_USER
  ITENTIAL_PASSWORD

Usage:
  # Local, file-based, dry run:
  python main.py \\
      --input devices.json \\
      --inventory_name "NetBox-Devices" \\
      --options '{"create_if_missing": true, "groups": "netbox-sync-admins", "dry_run": true}'

  # Local, file-based, real run (skips the interactive prompt):
  python main.py \\
      --input devices.json \\
      --inventory_name "NetBox-Devices" \\
      --options '{"backup_to": "backups/netbox-devices-pre-sync.json", "yes": true}'

  # IAG5 / agent-flow style, devices passed inline as a JSON string. Only 3 flags
  # ever exist here: devices, inventory_name, and options -- IAG maps decorator
  # schema property names straight to CLI flags of the same spelling.
  python main.py \\
      --devices '[{"name":"core-sw-01","primary_ip":"10.0.0.1"}]' \\
      --inventory_name "NetBox-Devices" \\
      --options '{"create_if_missing": true, "groups": "netbox-sync-admins", "yes": true}'

Device record shape (field names are whatever your extraction step
produces -- adjust `transform.transform_device` to match):

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

from diff_utils import backup_nodes, build_diff, diff_inventories, file_info, write_diff_log
from inventory_ops import ensure_inventory, fetch_all_nodes, find_inventory, list_all_inventories, populate_inventory
from platform_client import build_platform_client
from transform import build_nodes, load_devices

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


def should_proceed(explicit_yes: bool) -> bool:
    if explicit_yes:
        return True
    if not sys.stdin.isatty():
        # No controlling terminal (IAG, cron, CI) -- never block on input().
        return False
    answer = input("Type 'yes' to proceed with this full-replace: ").strip().lower()
    return answer == "yes"


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
