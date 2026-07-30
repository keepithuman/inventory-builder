"""
Device record -> Itential node schema transform, and device-list loading
from either an inline JSON string (--devices) or a local file (--input).
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("itential_inventory_loader")


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


def load_devices(args) -> list:
    if args.devices is not None:
        devices = json.loads(args.devices)
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            devices = json.load(f)

    if not isinstance(devices, list):
        raise ValueError("Device input must be a JSON array of device records.")
    return devices
