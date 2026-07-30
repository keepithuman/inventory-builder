"""
Platform authentication.

Builds an ipsdk Platform client from environment variables. Under IAG, the
same env var names are populated by the service's `secrets` block
(ITENTIAL_CLIENT_ID/ITENTIAL_CLIENT_SECRET) and `runtime.env`
(ITENTIAL_HOST/ITENTIAL_PORT) -- see services.yaml.
"""

from __future__ import annotations

import logging
import os

import ipsdk

log = logging.getLogger("itential_inventory_loader")


def build_platform_client():
    """
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
