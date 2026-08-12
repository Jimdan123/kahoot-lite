"""SSRF guard for custom BYOK provider base_urls (app/models.py's
UserApiKey.base_url, app/settings/forms.py's CustomProviderForm).

A logged-in user can point a custom provider's base_url at any host — the
SERVER then makes outbound HTTPS requests there on every pipeline run (see
app/ai/langgraph_flow/llm_utils.py's _build_custom_client). Without
validation this is a classic SSRF vector: an attacker could target
internal-only services, cloud metadata endpoints, or anything else
reachable from the server's own network egress that isn't reachable from
the public internet.

Checked in TWO places, not one, because a save-time-only check has a
DNS-rebinding gap: an attacker could point the hostname at a public IP
when saving (passing validation), then repoint its DNS record at an
internal IP before the pipeline actually uses it.
  1. CustomProviderForm.validate_base_url — save time, rejects the
     obvious/common case immediately with a clear form error.
  2. graph.py's _resolve_custom_providers — re-checked immediately before
     each pipeline run actually uses a saved URL, shrinking the window an
     attacker has to rebind DNS from "any time after saving" to "between
     this check and the HTTP call microseconds later."

This does NOT fully close that remaining microsecond-scale TOCTOU window —
doing so needs a custom HTTP transport that connects directly to a
pre-validated IP while still presenting the original hostname for TLS SNI/
certificate validation, which risks subtly breaking cert verification if
implemented wrong. Given this app's threat model (authenticated users
attacking their own server instance, not a high-value multi-tenant target),
double-checking with stdlib DNS resolution is judged sufficient. Documented
here, not silently decided.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class SSRFError(Exception):
    """Raised when a base_url fails the checks below — always has a
    message safe to show directly to the user who submitted it."""


def validate_public_https_url(url: str) -> None:
    """Raises SSRFError if `url` isn't a safe custom-provider base_url:
    not https, embeds credentials, uses a non-standard port, or resolves
    (any of its addresses, if the hostname has multiple DNS records) to a
    private/loopback/link-local/reserved/multicast/unspecified address."""
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise SSRFError('URL must use https://')
    if parsed.username or parsed.password:
        raise SSRFError('URL must not contain embedded credentials')
    hostname = (parsed.hostname or '').rstrip('.').lower()
    if not hostname:
        raise SSRFError('URL is missing a hostname')
    port = parsed.port or 443
    if port != 443:
        # Every real provider we've configured (OpenAI, Together, etc.) uses
        # standard 443 — a non-standard port is far more often an internal
        # service (a database, an admin panel, a k8s sidecar) than a
        # legitimate OpenAI-compatible API, so this isn't just an SSRF
        # guard, it's also a reasonable product default.
        raise SSRFError('URL must use the standard HTTPS port (443)')
    try:
        addrinfo = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFError(f'Could not resolve hostname {hostname!r}: {exc}') from exc
    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise SSRFError(
                f'That host resolves to a private/internal address ({ip}) — '
                f'custom providers must be publicly reachable services.'
            )
