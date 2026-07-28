# inventory-builder

A script that full-replace loads a list of devices into an Itential Platform
**Inventory Manager** inventory — plus the IAG5 service wrapper that makes it
callable from an Itential workflow or a FlowAI agent tool, passing the device
list inline as JSON.

Runs two ways:
- **Locally**, from a JSON file, for ad-hoc or cron-scheduled syncs.
- **As an IAG5 `python-script` service**, invoked from a workflow
  (`GatewayManager.runService`) or an agent, with no shared filesystem
  required between the caller and the script.

## Capabilities

- **Full inventory sync** — creates the target inventory if it doesn't exist
  (`create_if_missing`), then loads every device record as a node
  (`{name, attributes, tags}`), deduplicating by name.
- **Dry run** (`dry_run=true`) — transforms and validates the input locally
  without contacting the platform at all. Good for checking a device export
  before it touches anything live.
- **Diff preview before every write** — fetches the inventory's current
  nodes (paginated, so it scales past 10k+ devices) and computes an
  added/removed/unchanged summary against the incoming set. Always included
  in the JSON result, since the underlying API call is a **full replace, not
  an upsert** — every write clears the inventory first.
- **Confirmation gate** (`yes`) — without it, the script returns the diff and
  makes **zero** changes — it doesn't populate, and it doesn't even create the
  inventory if `create_if_missing=true` was also set (`action:
  "confirmation_required"`, plus `would_create_inventory`). Local interactive
  runs get a y/N prompt instead; non-interactive callers (IAG, cron) always
  get the structured refusal, never a hang. This is the main guard against a
  bad or truncated input silently wiping a live inventory.
- **Live preview** (`preview=true`) — the same read-only diff as above
  (existing nodes, added/removed/unchanged, `would_create_inventory`), but
  requested explicitly rather than implied by a missing `yes`, and it always
  wins even if `yes=true` is also passed. Unlike `dry_run`, this authenticates
  and reads the actual current platform state.
- **Rollback backup** (`backup_to` / `include_backup`) — snapshots the
  inventory's pre-replace nodes to a local file and/or embeds them directly
  in the JSON result, so a bad run can be undone.
- **Two input paths** — `devices` (a JSON string, for IAG/agent/workflow
  callers) or `input` (a file path, for local runs).
- **Structured JSON result on every run** — `success`, `action`
  (`dry_run` / `confirmation_required` / `populated`), the diff, populate
  statistics (`inserted`/`skipped`/`errors`), and optional backup — never a
  bare stack trace. Diagnostic/progress logging goes to stderr, so stdout is
  always exactly one JSON object.

## Layout

```
services.yaml                              — IAG decorator, repository, and service definition
python-scripts/inventory-loader/main.py            — the script IAG runs (and the local CLI entrypoint)
python-scripts/inventory-loader/requirements.txt   — pip dependencies (ipsdk, pinned — see below)
samples/devices.json                       — ready-to-use two-device input (see Samples below)
```

## Parameters

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `devices` / `input` | string | one of the two | — | `devices`: inline JSON array of device records. `input`: path to a JSON file (local runs only). |
| `inventory_name` | string | yes | — | Target Inventory Manager inventory name. |
| `create_if_missing` | string (`"true"`/anything else) | no | `"false"` | Create the inventory if it doesn't exist. |
| `groups` | string | no | `""` | Comma-separated group name(s) granted access when creating a new inventory (required if creating). |
| `dry_run` | string (`"true"`/anything else) | no | `"false"` | Transform and validate only — no platform calls at all. |
| `yes` | string (`"true"`/anything else) | no | `"false"` | Confirm the write. Without it, nothing changes — not even inventory creation. |
| `preview` | string (`"true"`/anything else) | no | `"false"` | Force the live read-only diff and exit without writing. Always wins over `yes=true`. |
| `include_backup` | string (`"true"`/anything else) | no | `"false"` | Embed the pre-replace node list in the JSON result. |
| `backup_to` | string (path) | no | — | Local-only: also write the pre-replace nodes to this file. |

Device records need only a `name` field; everything else becomes a
free-form node attribute (`tags` is pulled out separately). See the
script's module docstring for the full record shape and more usage
examples.

### Device vars (connection/credential attributes)

Loading a device into Inventory Manager makes it *visible*; it doesn't make
it *actionable*. To run an IAG action (`get-config`, `run-command`, etc.)
against a loaded node, the node's `attributes` need the connection fields
Inventory Manager/IAG actually read at execution time. These are just
regular device-record fields — the script has no special handling for them,
they pass through like anything else:

| Field | Purpose |
|---|---|
| `itential_host` | device IP/hostname the action connects to |
| `itential_port` | SSH/connection port |
| `itential_platform` | driver platform key, e.g. `cisco_ios`, `arista_eos` |
| `itential_driver` | connection library, e.g. `netmiko` |
| `itential_user` / `itential_password` | credentials — a secret reference, not a raw value |

**The secret-reference syntax is platform-specific — verify against a real
node before assuming a format.** This repo's target platform uses
`$SECRET_<vault-path> $KEY_<key>` (space-separated) — not the dot-notation
(`$SECRET.path.key`) shown as a generic example in the `itential-inventory`
skill doc. The `cat8k-01` record in `samples/devices.json` is not a made-up
example — it's the exact `attributes` of a real, working node from this
platform's `Workshop` inventory (`GET
/inventory_manager/v1/inventories/Workshop/nodes`), confirming this
attribute set actually resolves and connects.

## Auth

OAuth2 client-credentials (Itential service account) via environment
variables — in the IAG service these are injected from `secrets:` in
`services.yaml`:

| Env var | Source in this repo |
|---|---|
| `ITENTIAL_HOST` / `ITENTIAL_PORT` | `runtime.env` in `services.yaml` |
| `ITENTIAL_CLIENT_ID` / `ITENTIAL_CLIENT_SECRET` | IAG secrets `itential-client-id` / `itential-client-secret` |

## Running as an IAG5 service

```bash
iagctl db import services.yaml --validate   # check first
iagctl db import services.yaml              # then import

# Dry run
iagctl run service python-script inventory-loader \
  --set devices='[{"name":"core-sw-01","primary_ip":"10.0.0.1"}]' \
  --set inventory_name=NetBox-Devices \
  --set dry_run=true

# Real load, with rollback backup embedded in the result
iagctl run service python-script inventory-loader \
  --set devices='[{"name":"core-sw-01","primary_ip":"10.0.0.1"}]' \
  --set inventory_name=NetBox-Devices \
  --set create_if_missing=true \
  --set groups=netops-admins \
  --set yes=true \
  --set include_backup=true
```

From an Itential workflow, call it with `GatewayManager.runService`
(`serviceName: "inventory-loader"`), then extract `result.stdout` with a
`query` task and parse it — see the `iag` skill for the full wiring pattern.

## Samples

`samples/devices.json` is a ready-to-use two-device input matching the
record shape above. Output below is pretty-printed for readability — the
script actually emits a single JSON line.

**Dry run** — `--set devices=@samples/devices.json --set dry_run=true` (or
`--input samples/devices.json --dry_run true` locally):

```json
{
  "success": true,
  "action": "dry_run",
  "node_count": 2,
  "nodes_preview": [
    {
      "name": "cat8k-01",
      "attributes": {
        "itential_host": "192.168.228.201", "itential_port": 22,
        "itential_user": "$SECRET_devices/ios_cat8k $KEY_username", "itential_password": "$SECRET_devices/ios_cat8k $KEY_password",
        "itential_platform": "cisco_ios", "itential_driver": "netmiko", "netbox_id": 2
      },
      "tags": ["ios"]
    },
    {
      "name": "core-sw-02",
      "attributes": {
        "itential_host": "10.0.0.2", "itential_port": 22, "itential_platform": "arista_eos", "itential_driver": "netmiko",
        "itential_user": "$SECRET_devices/core_switches $KEY_username", "itential_password": "$SECRET_devices/core_switches $KEY_password"
      },
      "tags": ["prod", "core"]
    }
  ]
}
```

**Real run without `yes`** — the confirmation gate stops the write (and any
inventory creation) and reports what *would* happen:

```json
{
  "success": false,
  "action": "confirmation_required",
  "would_create_inventory": false,
  "diff": {
    "existing_count": 2,
    "new_count": 2,
    "added_count": 1,
    "removed_count": 1,
    "unchanged_count": 1,
    "added_preview": ["new-sw-03"],
    "removed_preview": ["core-sw-02"]
  }
}
```

**`preview=true` against an inventory that doesn't exist yet** — same
read-only shape, `action` distinguishes an explicit ask from an implied one:

```json
{
  "success": false,
  "action": "preview",
  "would_create_inventory": true,
  "diff": {
    "existing_count": 0,
    "new_count": 1,
    "added_count": 1,
    "removed_count": 0,
    "unchanged_count": 0,
    "added_preview": ["ghost-sw"],
    "removed_preview": []
  }
}
```

**Real run with `yes=true` and `include_backup=true`**:

```json
{
  "success": true,
  "action": "populated",
  "inventory_name": "Test-Inventory",
  "created_inventory": false,
  "diff": {
    "existing_count": 2,
    "new_count": 2,
    "added_count": 1,
    "removed_count": 1,
    "unchanged_count": 1,
    "added_preview": ["new-sw-03"],
    "removed_preview": ["core-sw-02"]
  },
  "populate": { "total": 2, "inserted": 2, "skipped": 0, "errors": [] },
  "backup": [
    { "_id": "...", "inventory_id": "...", "name": "core-sw-01", "attributes": { "...": "..." }, "tags": ["prod", "core"] },
    { "_id": "...", "inventory_id": "...", "name": "core-sw-02", "attributes": { "...": "..." }, "tags": ["prod", "core"] }
  ]
}
```

**Handled errors** — always valid JSON with `success: false`, never a bare
traceback:

```json
{"success": false, "error": "Device record missing required 'name' field: {'primary_ip': '10.0.0.1'}"}
```
```json
{"success": false, "error": "Inventory 'Does-Not-Exist' does not exist and create_if_missing was not set. Refusing to guess -- pass create_if_missing=true to create it."}
```

## Requirements pin

`requirements.txt` pins `ipsdk==0.4.0`. IAG5 `python-script` services run
under the host's default `python3` with no documented way to select a
different interpreter — on Python 3.9 hosts, `ipsdk>=0.5.0` fails to import
(it uses `str | None` syntax internally without a `__future__` import,
which only works on 3.10+). Bump this pin only after confirming the target
IAG host's `python3 --version`.
