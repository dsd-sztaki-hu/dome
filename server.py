#!/usr/bin/env python3
"""Configurable FastMCP server for an OpenAPI specification."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
    openapi_source: str
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
    include_tags: tuple[str, ...]
    exclude_tags: tuple[str, ...]


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
        default=os.getenv("OPENAPI_PATH", str(DEFAULT_OPENAPI_PATH)),
        help="Path or HTTP(S) URL to the OpenAPI JSON file.",
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
    parser.add_argument(
        "--include-tag",
        action="append",
        default=[],
        help=(
            "Only expose OpenAPI operations with this tag. Can be repeated. "
            "Alternatively set INCLUDE_TAGS as a comma-separated list."
        ),
    )
    parser.add_argument(
        "--exclude-tag",
        action="append",
        default=[],
        help=(
            "Hide OpenAPI operations with this tag. Can be repeated. "
            "Alternatively set EXCLUDE_TAGS as a comma-separated list."
        ),
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> ServerConfig:
    openapi_source = normalize_openapi_source(args.openapi)

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
        openapi_source=openapi_source,
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
        include_tags=tuple(combine_list_options(args.include_tag, "INCLUDE_TAGS")),
        exclude_tags=tuple(combine_list_options(args.exclude_tag, "EXCLUDE_TAGS")),
    )


def combine_list_options(values: list[str], env_name: str) -> list[str]:
    combined = []
    for value in values:
        combined.extend(split_csv(value))
    combined.extend(split_csv(os.getenv(env_name, "")))
    return unique_ordered([value.strip() for value in combined if value.strip()])


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def unique_ordered(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize_openapi_source(value: str) -> str:
    source = str(value).strip()
    if not source:
        raise SystemExit("OpenAPI source is required. Pass --openapi or set OPENAPI_PATH.")

    if is_http_url(source):
        return source

    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"OpenAPI file not found: {path}")
    return str(path)


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def load_openapi_spec(source: str, timeout: float) -> dict:
    if is_http_url(source):
        spec = fetch_openapi_spec(source, timeout)
    else:
        with Path(source).open("r", encoding="utf-8") as handle:
            spec = json.load(handle)
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise SystemExit(f"OpenAPI document must contain a paths object: {source}")
    return spec


def fetch_openapi_spec(url: str, timeout: float) -> dict:
    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise SystemExit(
            f"OpenAPI URL returned HTTP {exc.response.status_code}: {url}"
        ) from exc
    except httpx.RequestError as exc:
        raise SystemExit(f"Could not fetch OpenAPI URL {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"OpenAPI URL did not return valid JSON: {url}") from exc


def filter_openapi_by_tags(spec: dict, config: ServerConfig) -> dict:
    include_tags = normalized_tag_set(config.include_tags)
    exclude_tags = normalized_tag_set(config.exclude_tags)
    if not include_tags and not exclude_tags:
        return spec

    filtered = dict(spec)
    filtered_paths = {}
    operation_count = 0
    kept_count = 0

    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        filtered_path_item = {
            key: value
            for key, value in path_item.items()
            if key.startswith("x-") or key in {"parameters", "summary", "description"}
        }
        for method, operation in path_item.items():
            if method.lower() not in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}:
                continue
            operation_count += 1
            if operation_matches_tag_filter(operation, include_tags, exclude_tags):
                filtered_path_item[method] = operation
                kept_count += 1
        if has_operation(filtered_path_item):
            filtered_paths[path] = filtered_path_item

    if operation_count and kept_count == 0:
        raise SystemExit(
            "Tag filters removed every OpenAPI operation. "
            f"include={list(config.include_tags)} exclude={list(config.exclude_tags)}"
        )

    filtered["paths"] = filtered_paths
    filtered["tags"] = [
        tag
        for tag in spec.get("tags") or []
        if not isinstance(tag, dict)
        or normalize_tag(tag.get("name")) in tags_used_by_paths(filtered_paths)
    ]
    return filtered


def operation_matches_tag_filter(operation: object, include_tags: set[str], exclude_tags: set[str]) -> bool:
    if not isinstance(operation, dict):
        return False
    tags = normalized_tag_set(operation.get("tags") or [])
    if include_tags and not tags.intersection(include_tags):
        return False
    if exclude_tags and tags.intersection(exclude_tags):
        return False
    return True


def has_operation(path_item: dict) -> bool:
    return any(
        key.lower() in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
        for key in path_item
    )


def tags_used_by_paths(paths: dict) -> set[str]:
    tags = set()
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if isinstance(operation, dict):
                tags.update(normalized_tag_set(operation.get("tags") or []))
    return tags


def normalized_tag_set(values) -> set[str]:
    return {normalize_tag(value) for value in values if normalize_tag(value)}


def normalize_tag(value) -> str:
    return str(value).strip().casefold() if value is not None else ""


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
        openapi_spec=filter_openapi_by_tags(
            load_openapi_spec(config.openapi_source, config.timeout), config
        ),
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
