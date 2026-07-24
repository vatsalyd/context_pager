from __future__ import annotations

import json

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from context_pager.governance.pii_middleware import redact_json_envelope


class PIIRedactionMiddleware(BaseHTTPMiddleware):
    """Starlette HTTP middleware that redacts PII from MCP tool call JSON responses.

    MCP responses from FastMCP come back as text/event-stream with `event: message`
    lines carrying JSON-RPC payloads. We intercept the body, walk TextContent items,
    redact PII, and rewrite the body in the same SSE format.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Only post-process MCP tool-call responses
        if not request.url.path.startswith("/mcp"):
            return response
        if response.status_code != 200:
            return response

        # Collect streaming body
        body_chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            if chunk:
                body_chunks.append(chunk)
        body_bytes = b"".join(body_chunks)
        if not body_bytes:
            return response

        content_type = response.media_type or ""
        try:
            text = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return response

        if "event-stream" in content_type:
            new_body, modified = await self._redact_sse(text)
        else:
            new_body, modified = await self._redact_plain_json(text)

        if not modified:
            # Body iterator was consumed — rebuild response from original bytes
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        new_headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(
            content=new_body.encode("utf-8"),
            status_code=response.status_code,
            headers=new_headers,
            media_type=response.media_type,
        )

    async def _redact_sse(self, text: str) -> tuple[str, bool]:
        """Walk SSE-format MCP response, redact TextContent in data lines."""
        modified = False
        new_lines: list[str] = []
        for line in text.splitlines(keepends=True):
            if line.startswith("data:"):
                payload_str = line[5:].strip()
                try:
                    payload = json.loads(payload_str)
                    if await self._redact_payload(payload):
                        modified = True
                        new_lines.append(f"data: {json.dumps(payload)}\n")
                        continue
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass
            new_lines.append(line)
        return "".join(new_lines), modified

    async def _redact_plain_json(self, text: str) -> tuple[str, bool]:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return text, False
        modified = await self._redact_payload(payload)
        if modified:
            return json.dumps(payload), True
        return text, False

    @staticmethod
    async def _redact_payload(payload: dict) -> bool:
        """Walk JSON-RPC payload, redact PII in tool-result TextContent. Returns True if modified."""
        result = payload.get("result")
        if not isinstance(result, dict):
            return False
        content = result.get("content")
        if not isinstance(content, list):
            return False

        modified = False
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            try:
                envelope = json.loads(item["text"])
                if isinstance(envelope, dict):
                    redacted_envelope, counts = await redact_json_envelope(envelope)
                    if counts:
                        redacted_envelope.setdefault("metadata", {})["pii_redacted"] = counts
                        item["text"] = json.dumps(redacted_envelope)
                        modified = True
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
        return modified
