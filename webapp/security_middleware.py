from __future__ import annotations

import os
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class ProductionSecurityMiddleware(BaseHTTPMiddleware):
    """Small production guardrail layer for the single-process deployment."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id", "").strip()[:80] or secrets.token_hex(12)
        max_body = max(1024 * 1024, int(os.getenv("VBHC_MAX_REQUEST_BYTES", str(18 * 1024 * 1024))))
        raw_length = request.headers.get("content-length", "").strip()
        if raw_length:
            try:
                if int(raw_length) > max_body:
                    return JSONResponse(
                        {"ok": False, "error": f"Yêu cầu vượt quá giới hạn {max_body // (1024 * 1024)} MB"},
                        status_code=413,
                        headers={"X-Request-ID": request_id},
                    )
            except ValueError:
                pass

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "img-src 'self' data:; connect-src 'self'; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "form-action 'self'"
        )
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/") or request.url.path in {"/", "/reply", "/usage", "/billing", "/admin-control"}:
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response
