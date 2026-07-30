"""
Platform authentication and HTTP client.

Zero third-party dependencies -- everything here is the Python standard
library (urllib, ssl, base64). Builds a minimal Platform client (`.get`/
`.post`, each returning an object with `.raise_for_status()`/`.json()`,
matching the interface every other module in this script already expects)
from environment variables. Under IAG, the same env var names are populated
by the service's `secrets` block (ITENTIAL_CLIENT_ID/ITENTIAL_CLIENT_SECRET)
and `runtime.env` (ITENTIAL_HOST/ITENTIAL_PORT) -- see services.yaml.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("itential_inventory_loader")

REQUEST_TIMEOUT = 30


class Response:
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self._body}")

    def json(self):
        return json.loads(self._body) if self._body else {}


class Platform:
    """Bearer-token or Basic-auth HTTP client for the Itential Platform REST API."""

    def __init__(self, base_url: str, token: str | None = None, basic_auth: tuple[str, str] | None = None, verify: bool = True):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._basic_auth = basic_auth
        self._ssl_context = None if verify else ssl._create_unverified_context()

    def _auth_header(self) -> dict:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        if self._basic_auth:
            user, password = self._basic_auth
            creds = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            return {"Authorization": f"Basic {creds}"}
        return {}

    def _request(self, method: str, path: str, params: dict | None = None, json_body=None) -> Response:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)

        headers = {"Accept": "application/json", **self._auth_header()}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=self._ssl_context) as resp:
                return Response(resp.status, resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach {self.base_url}: {e.reason}") from e

    def get(self, path: str, params: dict | None = None) -> Response:
        return self._request("GET", path, params=params)

    def post(self, path: str, json=None) -> Response:
        return self._request("POST", path, json_body=json)


def _base_url(host: str, port: int) -> str:
    if port in (0, 443):
        return f"https://{host}"
    if port == 80:
        return f"http://{host}"
    return f"https://{host}:{port}"


def _fetch_oauth_token(base_url: str, client_id: str, client_secret: str, ssl_context) -> str:
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ssl_context) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"OAuth token response missing access_token: {body}")
    return token


def build_platform_client() -> Platform:
    """
    Prefers OAuth2 client-credentials (service account) if client_id/secret
    are present; falls back to basic auth otherwise.

    Raises RuntimeError (a fatal setup failure, not a business-logic error)
    if no usable credentials are found, or if the OAuth2 token request fails.
    """
    host = os.environ.get("ITENTIAL_HOST", "localhost")
    port = int(os.environ.get("ITENTIAL_PORT", "0"))
    verify = os.environ.get("ITENTIAL_VERIFY_TLS", "true").lower() != "false"
    client_id = os.environ.get("ITENTIAL_CLIENT_ID")
    client_secret = os.environ.get("ITENTIAL_CLIENT_SECRET")
    user = os.environ.get("ITENTIAL_USER")
    password = os.environ.get("ITENTIAL_PASSWORD")

    base_url = _base_url(host, port)
    ssl_context = None if verify else ssl._create_unverified_context()

    if client_id and client_secret:
        log.info("Authenticating to %s via OAuth2 client-credentials", host)
        try:
            token = _fetch_oauth_token(base_url, client_id, client_secret, ssl_context)
        except (urllib.error.URLError, ValueError) as e:
            raise RuntimeError(f"OAuth2 client-credentials authentication failed: {e}") from e
        return Platform(base_url, token=token, verify=verify)

    if user and password:
        log.warning(
            "Authenticating to %s via basic auth -- switch to a service "
            "account (ITENTIAL_CLIENT_ID/ITENTIAL_CLIENT_SECRET) for "
            "production/scheduled runs.",
            host,
        )
        return Platform(base_url, basic_auth=(user, password), verify=verify)

    raise RuntimeError(
        "No credentials found. Set ITENTIAL_CLIENT_ID/ITENTIAL_CLIENT_SECRET "
        "(preferred) or ITENTIAL_USER/ITENTIAL_PASSWORD."
    )


def create_service_account(platform: Platform, name: str, description: str = "") -> dict:
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
