import os
from typing import Iterable
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

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=30.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    upstream_request = client.build_request(
        request.method,
        upstream_url,
        headers=headers,
        content=await request.body(),
    )
    upstream_response = await client.send(upstream_request, stream=True)

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
