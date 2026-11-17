import json
import os
from typing import Any, Iterable
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse


UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://aihubmix.com").rstrip("/")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "").strip()
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "").strip()
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "600"))

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def parse_origins(raw: str) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or ["*"]


def filtered_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


# --- DeepSeek-V4-Flash compatibility (affects ONLY this model) -------------
# Some clients (e.g. LiveCaptions Translator) send max_tokens=128 together with
# several conflicting "disable thinking" fields. deepseek-v4-flash may then spend
# the whole budget on reasoning and return message.content = null, which makes
# those clients crash. Every other model is proxied byte-for-byte as before.
DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_V4_FLASH_MIN_MAX_TOKENS = 1024
_CONFLICTING_THINKING_FIELDS = ("reasoning_effort", "reasoning", "enable_thinking", "think")


def parse_deepseek_v4_flash_body(path: str, method: str, body: bytes) -> dict[str, Any] | None:
    """Return the parsed JSON body if this is a deepseek-v4-flash chat request, else None."""
    if method != "POST" or not path.rstrip("/").endswith("chat/completions") or not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if not isinstance(model, str) or model.strip().lower() != DEEPSEEK_V4_FLASH_MODEL:
        return None
    return payload


def adjust_deepseek_v4_flash_request(payload: dict[str, Any]) -> dict[str, Any]:
    for field in _CONFLICTING_THINKING_FIELDS:
        payload.pop(field, None)
    payload["thinking"] = {"type": "disabled"}
    for key in ("max_tokens", "max_completion_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and value < DEEPSEEK_V4_FLASH_MIN_MAX_TOKENS:
            payload[key] = DEEPSEEK_V4_FLASH_MIN_MAX_TOKENS
    return payload


def fix_deepseek_v4_flash_response(raw: bytes) -> bytes:
    """Make sure every choice has a string message.content."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        return raw
    changed = False
    for choice in data["choices"]:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and message.get("content") is None:
            message["content"] = ""
            changed = True
    return json.dumps(data, ensure_ascii=False).encode("utf-8") if changed else raw


app = FastAPI(title="AIHubMix Render Proxy", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_origins(os.getenv("ALLOWED_ORIGINS", "*")),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "upstream": UPSTREAM_BASE_URL,
            "usage": "Set your OpenAI-compatible base URL to this service URL plus /v1",
        }
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def verify_proxy_key(authorization: str | None) -> None:
    if not PROXY_API_KEY:
        return
    expected = f"Bearer {PROXY_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid proxy API key")


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request, authorization: str | None = Header(None)):
    if path in {"", "healthz"}:
        return Response(status_code=404)

    verify_proxy_key(authorization)

    upstream_url = urljoin(f"{UPSTREAM_BASE_URL}/", path)
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    headers = filtered_headers(request.headers.items())
    if UPSTREAM_API_KEY:
        headers["authorization"] = f"Bearer {UPSTREAM_API_KEY}"

    body = await request.body()
    deepseek_payload = parse_deepseek_v4_flash_body(path, request.method, body)
    if deepseek_payload is not None:
        body = json.dumps(adjust_deepseek_v4_flash_request(deepseek_payload)).encode("utf-8")

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=30.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    upstream_request = client.build_request(
        request.method,
        upstream_url,
        headers=headers,
        content=body,
    )
    upstream_response = await client.send(upstream_request, stream=True)

    is_event_stream = "text/event-stream" in upstream_response.headers.get("content-type", "")
    if deepseek_payload is not None and not deepseek_payload.get("stream") and not is_event_stream:
        try:
            raw = await upstream_response.aread()  # decoded (gzip etc. removed)
        finally:
            await upstream_response.aclose()
            await client.aclose()
        if upstream_response.is_success:
            raw = fix_deepseek_v4_flash_response(raw)
        fixed_headers = {
            key: value
            for key, value in filtered_headers(upstream_response.headers.items()).items()
            if key.lower() != "content-encoding"
        }
        return Response(
            content=raw,
            status_code=upstream_response.status_code,
            headers=fixed_headers,
            media_type=upstream_response.headers.get("content-type"),
        )

    async def close_upstream() -> None:
        await upstream_response.aclose()
        await client.aclose()

    response_headers = filtered_headers(upstream_response.headers.items())
    background = BackgroundTask(close_upstream)

    if request.method == "HEAD":
        return Response(
            status_code=upstream_response.status_code,
            headers=response_headers,
            background=background,
        )

    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=background,
        media_type=upstream_response.headers.get("content-type"),
    )
