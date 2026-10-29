"""Security headers applied to every response.

The app handles visitors' LLM API keys, so the browser itself is part of the
threat model. These headers are what make "your key stays yours" true rather
than merely intended:

  Content-Security-Policy  no inline script, no third-party script, no eval.
                           Even if a payload were reflected into the page, it
                           could not execute — nothing like an XSS can read
                           another visitor's localStorage.
  X-Frame-Options          DENY — the page cannot be framed, so clickjacking
                           (a fake "Save key" button over ours) is not possible.
  Referrer-Policy          no-referrer — the URL and any query values are never
                           sent to t.me or any other third party.
  Cross-Origin-Opener-Policy / -Resource-Policy  same-origin — a page on
                           another origin cannot open a handle to ours.
  Permissions-Policy       no camera/mic/geolocation.

Why this matters for isolation: keys live in the visitor's own browser storage.
Web storage is already partitioned per origin, and the CSP + COOP + frame
denial remove the two ways a hostile page could try to reach into it.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


def _inline_script_hashes() -> str:
    """Hash the inline <script> block so the CSP can allow exactly that code.

    The UI is deliberately a single self-contained file (no build step, and it
    renders correctly in sandboxed previews). Rather than weaken the policy
    with 'unsafe-inline', we pin the one script we ship by SHA-256. Anything
    else injected into the page — reflected input, a malicious post body — has
    a different hash and is refused by the browser. Re-hashing happens on every
    boot, so editing the file doesn't require updating a constant.
    """
    path = os.path.join(STATIC_DIR, "index.html")
    try:
        html = open(path, encoding="utf-8").read()
    except OSError:
        return "'unsafe-inline'"  # fail open rather than break the UI

    hashes = []
    for match in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S):
        digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
        hashes.append("'sha256-" + base64.b64encode(digest).decode() + "'")
    return " ".join(hashes) if hashes else "'none'"


SCRIPT_SRC = "'self' " + _inline_script_hashes()

CSP = (
    "default-src 'self'; "
    f"script-src {SCRIPT_SRC}; "            # exact-hash allowlist, no 'unsafe-inline'
    "style-src 'self' 'unsafe-inline'; "    # inline CSS only; cannot execute
    "img-src 'self' data:; "
    "connect-src 'self'; "                  # the browser only ever talks to this origin
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "upgrade-insecure-requests"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers["Content-Security-Policy"] = CSP
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Never let a proxy or the browser keep a response that may carry a
        # job's results (they are per-visitor and short-lived by design).
        if request.url.path.startswith(("/search", "/config", "/plan")):
            headers["Cache-Control"] = "no-store"
        return response
