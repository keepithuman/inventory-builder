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

Only 3 inputs ever exist: `devices` (or `input`), `inventory_name`, and a
single `options` JSON blob holding every optional toggle. This keeps the
CLI/decorator surface fixed regardless of how many toggles the script grows —
adding one is a script-only change, no service re-import required.

## Capabilities

- **Full inventory sync** — creates the target inventory if it doesn't exist
  (`options.create_if_missing`), then loads every device record as a node
  (`{name, attributes, tags}`), deduplicating by name.
- **Dry run** (`options.dry_run`) — transforms and validates the input
  locally without contacting the platform at all. Good for checking a device
  export before it touches anything live.
- **Content-aware diff preview before every write** — fetches the
  inventory's current nodes (paginated, so it scales past 10k+ devices) and
  categorizes each name in the incoming set as `added`, `removed`,
  `modified` (same name, different attributes or tags), or `unchanged`
  (same name AND identical content). Always included in the JSON result,
  since the underlying API call is a **full replace, not an upsert** —
  every write clears the inventory first.
- **Confirmation gate** (`options.yes`) — without it, the script returns the
  diff and makes **zero** changes — it doesn't populate, and it doesn't even
  create the inventory if `create_if_missing=true` was also set (`action:
  "confirmation_required"`, plus `would_create_inventory`). Local
  interactive runs get a y/N prompt instead; non-interactive callers (IAG,
  cron) always get the structured refusal, never a hang. This is the main
  guard against a bad or truncated input silently wiping a live inventory.
- **Live preview** (`options.preview`) — the same read-only diff as above,
  but requested explicitly rather than implied by a missing `yes`, and it
  always wins even if `yes=true` is also passed. Unlike `dry_run`, this
  authenticates and reads the actual current platform state.
- **Rollback backup** (`options.backup_to` / `options.include_backup`) —
  snapshots the inventory's pre-replace nodes to a local file and/or embeds
  them directly in the JSON result, so a bad run can be undone.
- **Timestamped diff audit trail** (`options.diff_log_dir`) — writes every
  computed diff to its own timestamped file in that directory, building a
  history across runs instead of only existing in one run's JSON output.
  Pass `"auto"` for a fresh tempfile-backed directory — safe under IAG,
  where each run gets a freshly cloned working directory. Opt-in; omit for
  no file.
- **File-size visibility** — any file the script writes (`backup_to`,
  `diff_log_dir`) is reported back with its actual byte size (`bytes`), so
  growth at scale (thousands of devices) is observed, not assumed.
- **Session census** — snapshots the full platform-wide inventory list
  (count + names) right after authenticating and again right before
  printing the final result, reporting the delta as a `session` block.
  Independent of the per-inventory node diff — catches "something else on
  the platform changed while this ran."
- **Broker actions at creation** (`options.create_broker_actions` +
  `options.cluster_id`) — auto-provisions the four standard actions
  (`get-config`, `set-config`, `run-command`, `is-alive`) on a newly created
  inventory, bound to the given IAG cluster. Only applies when the
  inventory doesn't exist yet.
- **Two input paths** — `devices` (a JSON string, for IAG/agent/workflow
  callers) or `input` (a file path, for local runs).
- **Structured JSON result on every run** — `success`, `action`
  (`dry_run` / `confirmation_required` / `preview` / `populated`), the
  diff, populate statistics (`inserted`/`skipped`/`errors`), and optional
  backup — never a bare stack trace. Diagnostic/progress logging goes to
  stderr, so stdout is always exactly one JSON object.

## Layout

```
services.yaml                                — IAG decorator, repository, and service definition
python-scripts/inventory-loader/main.py            — CLI entrypoint IAG runs: parsing + orchestration
python-scripts/inventory-loader/platform_client.py — auth + HTTP client (stdlib only, zero dependencies)
python-scripts/inventory-loader/inventory_ops.py   — Inventory Manager CRUD + session census
python-scripts/inventory-loader/transform.py       — device record -> node schema, device-list loading
python-scripts/inventory-loader/diff_utils.py      — diff computation, backup/diff-log file writers
samples/devices.json                         — ready-to-use two-device input (see Samples below)
```

Zero third-party dependencies — `platform_client.py` implements OAuth2
client-credentials and the HTTP calls with only the Python standard library
(`urllib`, `ssl`, `base64`). No `pip install` step, no version pinning to
track, and no risk of a dependency's own internals being incompatible with
whatever Python version an IAG host happens to run.

## Parameters

| Name | Type | Required | Default |
|---|---|---|---|
| `devices` / `input` | string | one of the two | — |
| `inventory_name` | string | yes | — |
| `options` | string (JSON object) | no | `"{}"` (all defaults below) |

`devices`: inline JSON array of device records. `input`: path to a JSON file
(local runs only). `inventory_name`: target Inventory Manager inventory
name.

**`options` keys** (all optional; values may be real JSON booleans or the
strings `"true"`/`"false"`):

| Key | Type | Default | Description |
|---|---|---|---|
| `create_if_missing` | bool | `false` | Create the inventory if it doesn't exist. |
| `groups` | string | `""` | Comma-separated group name(s) granted access when creating a new inventory (required if creating). |
| `create_broker_actions` | bool | `false` | Auto-provision the four standard actions on a newly created inventory. Requires `cluster_id`. No effect if the inventory already exists. |
| `cluster_id` | string | `""` | IAG cluster the broker actions are bound to. Required if `create_broker_actions=true`. |
| `dry_run` | bool | `false` | Transform and validate only — no platform calls at all. |
| `yes` | bool | `false` | Confirm the write. Without it, nothing changes — not even inventory creation. |
| `preview` | bool | `false` | Force the live read-only diff and exit without writing. Always wins over `yes=true`. |
| `include_backup` | bool | `false` | Embed the pre-replace node list in the JSON result. |
| `backup_to` | string (path) | `null` | Local-only: also write the pre-replace nodes to this file. |
| `diff_log_dir` | string (path, or `"auto"`) | `""` | Write every computed diff to its own timestamped file in this directory. `"auto"` uses a fresh tempfile-backed directory. Opt-in. |

An unknown key in `options` is a handled error (`success: false`), not a
silent no-op — see Samples below.

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
  --set options='{"dry_run": true}'

# Real load: create with broker actions, confirm the write, keep a diff audit trail
iagctl run service python-script inventory-loader \
  --set devices='[{"name":"core-sw-01","primary_ip":"10.0.0.1"}]' \
  --set inventory_name=NetBox-Devices \
  --set options='{"create_if_missing": true, "groups": "netops-admins", "create_broker_actions": true, "cluster_id": "cluster-itential", "yes": true, "include_backup": true, "diff_log_dir": "auto"}'
```

From an Itential workflow, call it with `GatewayManager.runService`
(`serviceName: "inventory-loader"`), then extract `result.stdout` with a
`query` task and parse it — see the `iag` skill for the full wiring pattern.

## Samples

`samples/devices.json` is a ready-to-use two-device input matching the
record shape above. Output below is pretty-printed for readability — the
script actually emits a single JSON line. All captured from real runs
against a live platform, not invented.

**Dry run** — `--options '{"dry_run": true}'`:

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

**No `options` at all** — defaults to a confirmation-gated, content-aware
diff against `Test-Inventory`'s actual nodes (one record matched exactly —
`unchanged`; one had a different attribute set — `modified`), plus the
session census:

```json
{
  "success": false,
  "action": "confirmation_required",
  "would_create_inventory": false,
  "diff": {
    "existing_count": 2, "new_count": 2,
    "added_count": 0, "removed_count": 0,
    "modified_count": 1, "unchanged_count": 1,
    "added_preview": [], "removed_preview": [], "modified_preview": ["core-sw-01"]
  },
  "session": { "before_count": 3, "after_count": 3, "inventories_added": [], "inventories_removed": [] }
}
```

**`--options '{"preview": true, "yes": true}'`** — `preview` always wins,
even with `yes` also set:

```json
{
  "success": false,
  "action": "preview",
  "would_create_inventory": false,
  "diff": { "existing_count": 2, "new_count": 2, "added_count": 0, "removed_count": 0, "modified_count": 1, "unchanged_count": 1, "added_preview": [], "removed_preview": [], "modified_preview": ["core-sw-01"] },
  "session": { "before_count": 3, "after_count": 3, "inventories_added": [], "inventories_removed": [] }
}
```

**Real run** — `--options '{"create_if_missing": true, "groups": "admins", "create_broker_actions": true, "cluster_id": "cluster-itential", "yes": true, "diff_log_dir": "auto"}'`
against a brand-new inventory name:

```json
{
  "success": true,
  "action": "populated",
  "inventory_name": "Options-Test-Inv-XYZ",
  "created_inventory": true,
  "diff": { "existing_count": 0, "new_count": 1, "added_count": 1, "removed_count": 0, "modified_count": 0, "unchanged_count": 0, "added_preview": ["ghost"], "removed_preview": [], "modified_preview": [] },
  "populate": { "total": 1, "inserted": 1, "skipped": 0, "errors": [] },
  "diff_log": { "path": "/tmp/inventory-diff-d74p9ia1/diff-Options-Test-Inv-XYZ-20260730T174049Z.json", "bytes": 338 },
  "session": { "before_count": 3, "after_count": 4, "inventories_added": ["Options-Test-Inv-XYZ"], "inventories_removed": [] }
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
```json
{"success": false, "error": "cluster_id is required when create_broker_actions is true -- it's the IAG cluster the four standard actions (get-config, set-config, run-command, is-alive) will be bound to."}
```
```json
{"success": false, "error": "Unknown options key(s): ['totally_made_up']. Valid keys: ['backup_to', 'cluster_id', 'create_broker_actions', 'create_if_missing', 'diff_log_dir', 'dry_run', 'groups', 'include_backup', 'preview', 'yes']"}
```

## Dependencies

None. `platform_client.py` implements OAuth2 client-credentials and every
HTTP call with only the Python standard library, so this runs unmodified on
any `python3` IAG happens to have installed — no `requirements.txt`, no
`pip install` step on every run, and no version-compatibility surface
between a dependency's internals and the host's Python version.
