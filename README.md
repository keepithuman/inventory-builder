# inventory-builder

IAG5 python-script service that full-replace loads a device list into an
Itential Platform Inventory Manager inventory. Callable from an Itential
workflow (`GatewayManager.runService`) or a FlowAI agent tool, passing
`devices` inline as a JSON string.

- `services.yaml` — IAG decorator, repository, and service definition.
  Import with `iagctl db import services.yaml`.
- `python-scripts/inventory-loader/main.py` — the script IAG runs.
- `python-scripts/inventory-loader/requirements.txt` — pip dependencies (`ipsdk`).

See the script's module docstring for the full IAG5 contract, safety
behavior (diff preview, backup, confirmation), and usage examples.
