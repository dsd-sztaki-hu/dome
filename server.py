#!/usr/bin/env python3
"""Configurable FastMCP server for an OpenAPI specification."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers


DEFAULT_OPENAPI_PATH = Path(__file__).with_name("openapi.json")
DEFAULT_SERVER_NAME = "Dataverse FastMCP"
DEFAULT_API_BASE_URL = "http://127.0.0.1:8080/api/"
DEFAULT_API_KEY_HEADER = "X-Dataverse-key"
DEFAULT_API_KEY_ENV = "DATAVERSE_API_TOKEN"


@dataclass(frozen=True)
class ServerConfig:
    name: str
    openapi_path: Path
    api_base_url: str
    api_key_header: str
    api_key_env: str
    api_key_mode: str
    transport: str
    host: str
    port: int
    path: str
    log_level: str | None
    timeout: float


class DataverseApiKeyAuth(httpx.Auth):
    def __init__(self, config: ServerConfig) -> None:
        self.config = config

    async def async_auth_flow(self, request: httpx.Request):
        api_key = api_key_for_current_request(self.config)
        if api_key:
            request.headers[self.config.api_key_header] = api_key
        elif self.config.api_key_mode != "none":
            raise RuntimeError(api_key_missing_message(self.config))
        yield request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Run {DEFAULT_SERVER_NAME}.")
    parser.add_argument(
        "--name",
        default=os.getenv("FASTMCP_SERVER_NAME", DEFAULT_SERVER_NAME),
        help="FastMCP server name.",
    )
    parser.add_argument(
        "--openapi",
        type=Path,
        default=Path(os.getenv("OPENAPI_PATH", str(DEFAULT_OPENAPI_PATH))),
        help="Path to the OpenAPI JSON file.",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("API_BASE_URL", DEFAULT_API_BASE_URL),
        help="Target API base URL used for upstream OpenAPI calls.",
    )
    parser.add_argument(
        "--api-key-header",
        default=os.getenv("API_KEY_HEADER", DEFAULT_API_KEY_HEADER),
        help="HTTP header used for the Dataverse API key.",
    )
    parser.add_argument(
        "--api-key-env",
        default=os.getenv("API_KEY_ENV", DEFAULT_API_KEY_ENV),
        help="Environment variable used for the API key in env mode.",
    )
    parser.add_argument(
        "--api-key-mode",
        choices=["env", "request-header", "none"],
        default=os.getenv("API_KEY_MODE", "request-header"),
        help=(
            "How to supply the Dataverse API key: 'env' reads --api-key-env "
            "from this process, 'request-header' forwards --api-key-header "
            "from each incoming HTTP MCP request, and 'none' sends no key."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http", "sse"],
        default=os.getenv("FASTMCP_TRANSPORT", "stdio"),
        help="MCP transport to use.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("FASTMCP_HOST", "127.0.0.1"),
        help="Host for HTTP/SSE transports.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FASTMCP_PORT", "8000")),
        help="Port for HTTP/SSE transports.",
    )
    parser.add_argument(
        "--path",
        default=os.getenv("FASTMCP_PATH", "/mcp"),
        help="Path for HTTP/SSE transports.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("FASTMCP_LOG_LEVEL") or None,
        help="Optional FastMCP log level.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("API_TIMEOUT", "30")),
        help="Upstream API request timeout in seconds.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> ServerConfig:
    openapi_path = args.openapi.expanduser().resolve()
    if not openapi_path.exists():
        raise SystemExit(f"OpenAPI file not found: {openapi_path}")

    if not args.api_base_url:
        raise SystemExit(
            "API base URL is required. Pass --api-base-url or set API_BASE_URL."
        )

    if args.api_key_mode == "request-header" and args.transport == "stdio":
        raise SystemExit(
            "API key mode 'request-header' requires an HTTP transport. "
            "Run with --transport streamable-http, http, or sse."
        )

    return ServerConfig(
        name=args.name,
        openapi_path=openapi_path,
        api_base_url=args.api_base_url,
        api_key_header=args.api_key_header,
        api_key_env=args.api_key_env,
        api_key_mode=args.api_key_mode,
        transport=args.transport,
        host=args.host,
        port=args.port,
        path=args.path,
        log_level=args.log_level,
        timeout=args.timeout,
    )


def load_openapi_spec(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        spec = json.load(handle)
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise SystemExit(f"OpenAPI document must contain a paths object: {path}")
    return spec


def api_key_for_current_request(config: ServerConfig) -> str | None:
    if config.api_key_mode == "none":
        return None

    if config.api_key_mode == "env":
        return os.getenv(config.api_key_env)

    if config.api_key_mode == "request-header":
        try:
            headers = get_http_headers()
        except RuntimeError as exc:
            raise RuntimeError(
                f"{config.api_key_mode} mode requires an HTTP MCP transport so "
                f"callers can provide {config.api_key_header}."
            ) from exc
        return case_insensitive_header(headers, config.api_key_header)

    raise RuntimeError(f"Unsupported API key mode: {config.api_key_mode}")


def case_insensitive_header(headers, name: str) -> str | None:
    value = headers.get(name)
    if value:
        return value

    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted and value:
            return value
    return None


def api_key_missing_message(config: ServerConfig) -> str:
    if config.api_key_mode == "env":
        return f"Missing {config.api_key_env}; cannot set {config.api_key_header}."
    if config.api_key_mode == "request-header":
        return f"Missing incoming {config.api_key_header} header."
    return f"Missing API key for mode {config.api_key_mode}."


def static_api_headers() -> dict[str, str]:
    headers: dict[str, str] = {}

    bearer_token = os.getenv("API_BEARER_TOKEN")
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    raw_extra_headers = os.getenv("API_HEADERS_JSON")
    if raw_extra_headers:
        try:
            extra_headers = json.loads(raw_extra_headers)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"API_HEADERS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(extra_headers, dict):
            raise SystemExit("API_HEADERS_JSON must be a JSON object.")
        headers.update({str(key): str(value) for key, value in extra_headers.items()})

    return headers


def create_mcp_server(config: ServerConfig) -> FastMCP:
    api_client = httpx.AsyncClient(
        base_url=config.api_base_url,
        headers=static_api_headers(),
        auth=DataverseApiKeyAuth(config),
        timeout=config.timeout,
    )
    return FastMCP.from_openapi(
        openapi_spec=load_openapi_spec(config.openapi_path),
        client=api_client,
        name=config.name,
    )


def run_server(config: ServerConfig, mcp: FastMCP) -> None:
    if config.transport == "stdio":
        kwargs = {}
        if config.log_level:
            kwargs["log_level"] = config.log_level
        mcp.run(**kwargs)
        return

    kwargs = {
        "transport": config.transport,
        "host": config.host,
        "port": config.port,
        "path": config.path,
    }
    if config.log_level:
        kwargs["log_level"] = config.log_level
    mcp.run(**kwargs)


def main() -> None:
    config = build_config(parse_args())
    run_server(config, create_mcp_server(config))


if __name__ == "__main__":
    main()
