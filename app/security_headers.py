"""Security response headers. Covers most of what Mozilla Observatory
grades: framing, MIME sniffing, referrer leakage, permission grants, CSP
for XSS containment, and (in prod) HTTPS enforcement."""
from __future__ import annotations

import secrets

from flask import g


def register_security_headers(app, is_production: bool) -> None:
    @app.before_request
    def _mint_csp_nonce():
        # Fresh per-request nonce; each inline <script nonce="..."> in the
        # templates picks this up via the csp_nonce() context helper.
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.context_processor
    def _inject_csp_nonce():
        return {'csp_nonce': lambda: getattr(g, 'csp_nonce', '')}

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault(
            'Permissions-Policy',
            'geolocation=(), microphone=(), camera=(), payment=()',
        )
        # CSP:
        #   script-src whitelists the two CDNs we load (Bootstrap, Socket.IO)
        #     and requires a per-request nonce on any inline <script>.
        #   style-src still allows 'unsafe-inline' because Bootstrap and
        #     inline style="display:none" toggles need it — the real XSS
        #     surface is scripts, not styles.
        #   connect-src allows same-origin WebSockets for Flask-SocketIO.
        #   frame-ancestors 'none' subsumes X-Frame-Options for modern browsers.
        nonce = getattr(g, 'csp_nonce', '')
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; "
            "form-action 'self'; "
            "base-uri 'self'; "
            "object-src 'none'"
        )
        response.headers.setdefault('Content-Security-Policy', csp)
        if is_production:
            # 1 year; keeps browsers on HTTPS after the first visit.
            response.headers.setdefault(
                'Strict-Transport-Security',
                'max-age=31536000; includeSubDomains',
            )
        return response
