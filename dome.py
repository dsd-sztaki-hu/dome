#!/usr/bin/env python3
"""Configurable DOME MCP server for an OpenAPI specification."""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import logging
import logging.config
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import fastmcp
import httpx

try:
    import httpx2
except ImportError:  # FastMCP 3.x does not use httpx2.
    httpx2 = None

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.utilities.logging import get_logger
from rich.align import Align
from rich.console import Console, Group
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


logger = get_logger(__name__)


DEFAULT_OPENAPI_PATH = Path(__file__).with_name("openapi.json")
DEFAULT_ENV_PATH = Path(__file__).with_name(".env")
DEFAULT_SERVER_NAME = "Dataverse DOME MCP"
DEFAULT_API_BASE_URL = "http://127.0.0.1:8080/api/"
DEFAULT_API_KEY_HEADER = "X-Dataverse-key"
DEFAULT_API_KEY_ENV = "DATAVERSE_API_TOKEN"
DEFAULT_API_KEY_SECURITY_SCHEME = "DataverseApiKey"
DEFAULT_MCP_INSTRUCTIONS = """
You are using Dataverse through the DOME MCP server.

Use the available tools according to their descriptions and input schemas. Do not
put API credentials in tool arguments or disclose them in responses.

Dataset safety rules:
- Treat creating, editing, and saving a dataset as draft work.
- Never publish, release, or otherwise make a dataset public as part of a create
  or edit request.
- Publish only when the user explicitly asks for publication, or after you have
  explained that publication is the next step and the user explicitly confirms.
- Do not infer publication permission from a general request to create, edit,
  save, or prepare a dataset, and do not infer confirmation from an earlier
  message.
- Before a publication action, clearly state the effect and ask for confirmation
  if the user's intent is not explicit.

Apply the same caution to other irreversible or high-impact operations: explain
what will happen and ask for confirmation when the user's intent is ambiguous.
""".strip()
DOME_LOGO = (
    "██████╗  ██████╗ ███╗   ███╗███████╗\n"
    "██╔══██╗██╔═══██╗████╗ ████║██╔════╝\n"
    "██║  ██║██║   ██║██╔████╔██║█████╗  \n"
    "██║  ██║██║   ██║██║╚██╔╝██║██╔══╝  \n"
    "██████╔╝╚██████╔╝██║ ╚═╝ ██║███████╗\n"
    "╚═════╝  ╚═════╝ ╚═╝     ╚═╝╚══════╝"
)
HTTP_METHODS = {
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
}


def _fastmcp_major_version() -> int:
    return int(fastmcp.__version__.split(".", 1)[0])


def _select_http_client_module():
    if _fastmcp_major_version() >= 4:
        if httpx2 is None:
            raise RuntimeError(
                "FastMCP 4 or newer requires the httpx2 package for OpenAPI clients."
            )
        return httpx2
    return httpx


# FastMCP 4 changed OpenAPIProvider from the legacy httpx package to httpx2.
# Keep the auth implementation on the same client family as the provider.
http_client = _select_http_client_module()
HttpRequest = http_client.Request
HttpAuth = http_client.Auth


def dome_log_time_format(log_time: datetime) -> Text:
    """Format log timestamps with millisecond precision."""

    return Text(f"{log_time.strftime('[%x %X')}.{log_time.microsecond // 1000:03d}]")


class DomeLogHandler(RichHandler):
    """Render DOME, FastMCP, and Uvicorn records in one Rich format."""

    def __init__(self, stream=None, *, show_path=False, console=None, **rich_kwargs) -> None:
        if console is None:
            console = Console(
                file=stream if stream is not None else sys.stderr,
                soft_wrap=True,
            )
        rich_kwargs.setdefault("log_time_format", dome_log_time_format)
        super().__init__(
            console=console,
            show_path=show_path,
            omit_repeated_times=False,
            **rich_kwargs,
        )
        self.setFormatter(logging.Formatter("%(message)s"))

    def render(self, *, record, traceback, message_renderable):
        if traceback is not None:
            return super().render(
                record=record,
                traceback=traceback,
                message_renderable=message_renderable,
            )

        path = Path(record.pathname).name
        level = self.get_level_text(record)
        time_format = None if self.formatter is None else self.formatter.datefmt
        log_time = datetime.fromtimestamp(record.created)
        line = Text()

        if self._log_render.show_time:
            time_format = time_format or self._log_render.time_format
            if callable(time_format):
                log_time_display = time_format(log_time)
            else:
                log_time_display = Text(log_time.strftime(time_format))
            log_time_display.stylize("log.time")
            if (
                log_time_display == self._log_render._last_time
                and self._log_render.omit_repeated_times
            ):
                line.append(" " * len(log_time_display))
            else:
                line.append_text(log_time_display)
                self._log_render._last_time = log_time_display
            line.append(" ")

        if self._log_render.show_level:
            line.append_text(level)
            line.append(" ")

        line.append_text(message_renderable)

        if self._log_render.show_path and path:
            path_text = Text()
            link_path = record.pathname if self.enable_link_path else None
            path_text.append(
                path,
                style=f"link file://{link_path}" if link_path else "log.path",
            )
            if record.lineno:
                path_text.append(":", style="log.path")
                path_text.append(
                    str(record.lineno),
                    style=(
                        f"link file://{link_path}#{record.lineno}"
                        if link_path
                        else "log.path"
                    ),
                )
            line.append(" ")
            line.append_text(path_text)

        line.no_wrap = True
        return line


@dataclass(frozen=True)
class ServerConfig:
    name: str
    openapi_source: str
    openapi_patch_path: str | None
    instructions_file: str | None
    api_base_url: str
    api_key_header: str
    api_key_env: str
    api_key_mode: str
    transport: str
    host: str
    port: int
    path: str
    show_banner: bool
    show_tools: bool
    log_dataverse_requests: bool
    ignore_ssl_errors: bool
    log_level: str | None
    timeout: float
    include_tags: tuple[str, ...]
    exclude_tags: tuple[str, ...]
    include_tools: tuple[str, ...]
    exclude_tools: tuple[str, ...]


@dataclass(frozen=True)
class OperationAuthRule:
    method: str
    path_template: str
    requires_api_key: bool


@dataclass(frozen=True)
class OperationAuthRules:
    base_path: str
    rules: tuple[OperationAuthRule, ...]

    def requires_api_key(self, request: HttpRequest) -> bool:
        method = request.method.lower()
        path = self.relative_request_path(request.url.path)
        for rule in self.rules:
            if rule.method == method and path_matches_template(rule.path_template, path):
                return rule.requires_api_key
        return False

    def relative_request_path(self, request_path: str) -> str:
        path = normalize_url_path(request_path)
        if self.base_path == "/":
            return path
        if path == self.base_path:
            return "/"
        prefix = f"{self.base_path}/"
        if path.startswith(prefix):
            return normalize_url_path(path[len(self.base_path):])
        return path


class DataverseApiKeyAuth(HttpAuth):
    def __init__(self, config: ServerConfig, operation_auth: OperationAuthRules) -> None:
        self.config = config
        self.operation_auth = operation_auth

    async def async_auth_flow(self, request: HttpRequest):
        requires_api_key = self.operation_auth.requires_api_key(request)
        request.headers.pop(self.config.api_key_header, None)
        if requires_api_key:
            api_key = api_key_for_current_request(
                self.config, require_http_context=requires_api_key
            )
            if api_key:
                request.headers[self.config.api_key_header] = api_key
            elif self.config.api_key_mode != "none":
                raise RuntimeError(api_key_missing_message(self.config))

        if self.config.log_dataverse_requests:
            log_dataverse_request(
                self.config,
                request,
                authenticated=request_has_authentication(self.config, request),
            )
        yield request


def parse_args() -> argparse.Namespace:
    load_env_file(DEFAULT_ENV_PATH)
    parser = argparse.ArgumentParser(description=f"Run {DEFAULT_SERVER_NAME}.")
    parser.add_argument(
        "--name",
        default=os.getenv("MCP_SERVER_NAME", DEFAULT_SERVER_NAME),
        help="MCP server name.",
    )
    parser.add_argument(
        "--openapi",
        default=os.getenv("OPENAPI_PATH", str(DEFAULT_OPENAPI_PATH)),
        help="Path or HTTP(S) URL to the OpenAPI JSON file.",
    )
    parser.add_argument(
        "--openapi-patch",
        default=os.getenv("MCP_OPENAPI_PATCH") or None,
        help=(
            "Path to a local OpenAPI correction file (operation overrides or "
            "JSON Patch) applied before DOME creates MCP tools."
        ),
    )
    parser.add_argument(
        "--instructions-file",
        default=os.getenv("MCP_INSTRUCTIONS_FILE") or None,
        help=(
            "Optional Markdown/text file with additional instructions sent to the "
            "MCP client during initialization."
        ),
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
            "from each incoming HTTP MCP request and falls back to --api-key-env, "
            "and 'none' sends no key."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http", "sse"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="MCP transport to use.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "127.0.0.1"),
        help="Host for HTTP/SSE transports.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT", "8000")),
        help="Port for HTTP/SSE transports.",
    )
    parser.add_argument(
        "--path",
        default=os.getenv("MCP_PATH", "/mcp"),
        help="Path for HTTP/SSE transports.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("MCP_LOG_LEVEL") or None,
        help="Optional MCP log level.",
    )
    parser.add_argument(
        "--show-tools",
        action=argparse.BooleanOptionalAction,
        default=parse_bool_env("MCP_SHOW_TOOLS", False),
        help="Print individual MCP tools at startup (default: false).",
    )
    parser.add_argument(
        "--show-banner",
        action=argparse.BooleanOptionalAction,
        default=parse_bool_env("MCP_SHOW_BANNER", True),
        help="Print the DOME startup banner (default: true).",
    )
    parser.add_argument(
        "--log-dataverse-requests",
        action=argparse.BooleanOptionalAction,
        default=parse_bool_env("MCP_LOG_DATAVERSE_REQUESTS", False),
        help=(
            "Log each MCP-to-Dataverse request with its authentication status; "
            "never logs credentials (default: false)."
        ),
    )
    parser.add_argument(
        "--ignore-ssl-errors",
        action=argparse.BooleanOptionalAction,
        default=parse_bool_env("MCP_IGNORE_SSL_ERRORS", False),
        help=(
            "Disable SSL certificate verification for OpenAPI and Dataverse "
            "requests (insecure; default: false)."
        ),
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
    parser.add_argument(
        "--include-tool",
        action="append",
        default=[],
        help=(
            "Only expose OpenAPI operations with this operationId. Can be repeated. "
            "Alternatively set INCLUDE_TOOLS as a comma-separated list."
        ),
    )
    parser.add_argument(
        "--exclude-tool",
        action="append",
        default=[],
        help=(
            "Hide OpenAPI operations with this operationId. Can be repeated. "
            "Alternatively set EXCLUDE_TOOLS as a comma-separated list."
        ),
    )
    return parser.parse_args()


def parse_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(
        f"{name} must be one of: 1, 0, true, false, yes, no, on, off."
    )


def build_config(args: argparse.Namespace) -> ServerConfig:
    openapi_source = normalize_openapi_source(args.openapi)
    openapi_patch_path = normalize_openapi_patch_path(
        getattr(args, "openapi_patch", None)
    )
    instructions_file = normalize_mcp_instructions_path(
        getattr(args, "instructions_file", None)
    )

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
        openapi_patch_path=openapi_patch_path,
        instructions_file=instructions_file,
        api_base_url=args.api_base_url,
        api_key_header=args.api_key_header,
        api_key_env=args.api_key_env,
        api_key_mode=args.api_key_mode,
        transport=args.transport,
        host=args.host,
        port=args.port,
        path=args.path,
        show_banner=args.show_banner,
        show_tools=args.show_tools,
        log_dataverse_requests=args.log_dataverse_requests,
        ignore_ssl_errors=args.ignore_ssl_errors,
        log_level=args.log_level,
        timeout=args.timeout,
        include_tags=tuple(combine_list_options(args.include_tag, "INCLUDE_TAGS")),
        exclude_tags=tuple(combine_list_options(args.exclude_tag, "EXCLUDE_TAGS")),
        include_tools=tuple(combine_list_options(args.include_tool, "INCLUDE_TOOLS")),
        exclude_tools=tuple(combine_list_options(args.exclude_tool, "EXCLUDE_TOOLS")),
    )


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    process_environment_keys = set(os.environ)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in process_environment_keys:
            continue

        os.environ[key] = unquote_env_value(value.strip())


def unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


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

    path = resolve_openapi_path(source)
    if path is None:
        raise SystemExit(f"OpenAPI file not found: {Path(source).expanduser().resolve()}")
    return str(path)


def normalize_openapi_patch_path(value: str | None) -> str | None:
    if value is None:
        return None

    source = str(value).strip()
    if not source:
        return None

    path = resolve_openapi_path(source)
    if path is None or not path.is_file():
        raise SystemExit(
            f"OpenAPI patch file not found: {Path(source).expanduser().resolve()}"
        )
    return str(path)


def normalize_mcp_instructions_path(value: str | None) -> str | None:
    if value is None:
        return None

    source = str(value).strip()
    if not source:
        return None

    path = resolve_openapi_path(source)
    if path is None or not path.is_file():
        raise SystemExit(
            f"MCP instructions file not found: {Path(source).expanduser().resolve()}"
        )
    return str(path)


def load_mcp_instructions(path: str | None) -> str:
    """Return built-in DOME guidance with optional deployment-specific additions."""

    if path is None:
        return DEFAULT_MCP_INSTRUCTIONS

    try:
        additional_instructions = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(
            f"Could not read MCP instructions file {path}: {exc}"
        ) from exc

    if not additional_instructions:
        return DEFAULT_MCP_INSTRUCTIONS
    return f"{DEFAULT_MCP_INSTRUCTIONS}\n\n{additional_instructions}"


def resolve_openapi_path(source: str) -> Path | None:
    path = Path(source).expanduser()
    candidates = [path]
    if not path.is_absolute():
        candidates.append(DEFAULT_ENV_PATH.parent / path)

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    return None


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def load_openapi_spec(source: str, timeout: float, verify_ssl: bool = True) -> dict:
    if is_http_url(source):
        spec = fetch_openapi_spec(source, timeout, verify_ssl=verify_ssl)
    else:
        with Path(source).open("r", encoding="utf-8") as handle:
            spec = json.load(handle)
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise SystemExit(f"OpenAPI document must contain a paths object: {source}")
    return spec


def apply_openapi_patch_file(spec: dict, path: str) -> dict:
    patch_path = Path(path)
    try:
        with patch_path.open("r", encoding="utf-8") as handle:
            patch_document = json.load(handle)
    except OSError as exc:
        raise SystemExit(f"Could not read OpenAPI patch file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"OpenAPI patch file is not valid JSON {path}: {exc}"
        ) from exc

    try:
        return apply_openapi_patches(spec, patch_document)
    except ValueError as exc:
        raise SystemExit(f"Invalid OpenAPI patch file {path}: {exc}") from exc


def apply_openapi_patches(spec: dict, patches: object) -> dict:
    """Apply an OpenAPI correction document to an OpenAPI document.

    The preferred format targets operations by operationId. An RFC 6902-style
    JSON Patch array remains supported for advanced or backwards-compatible
    corrections.
    """

    if isinstance(patches, dict):
        if "operations" in patches:
            return apply_openapi_operation_overrides(spec, patches["operations"])
        patches = patches.get("patches")
    if not isinstance(patches, list):
        raise ValueError("the patch document must be an array of patch operations")

    patched = deepcopy(spec)
    if not isinstance(patched, dict):
        raise ValueError("the OpenAPI document must be a JSON object")

    for operation_index, operation in enumerate(patches):
        try:
            patched = apply_openapi_patch_operation(patched, operation)
        except ValueError as exc:
            raise ValueError(f"operation {operation_index}: {exc}") from exc

    if not isinstance(patched, dict) or not isinstance(patched.get("paths"), dict):
        raise ValueError("patches must leave the OpenAPI document with a paths object")
    return patched


def apply_openapi_operation_overrides(spec: dict, operations: object) -> dict:
    """Apply human-readable operationId-targeted replacements."""

    if not isinstance(operations, dict):
        raise ValueError("'operations' must be an object keyed by operationId")

    patched = deepcopy(spec)
    if not isinstance(patched, dict) or not isinstance(patched.get("paths"), dict):
        raise ValueError("the OpenAPI document must contain a paths object")

    operation_matches = {}
    for path, path_item in patched["paths"].items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if (
                method.lower() in HTTP_METHODS
                and isinstance(operation, dict)
                and isinstance(operation.get("operationId"), str)
            ):
                operation_matches.setdefault(operation["operationId"], []).append(
                    (path, method, operation)
                )

    for operation_id, override in operations.items():
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation override keys must be non-empty operationId strings")

        matches = operation_matches.get(operation_id, [])
        if not matches:
            raise ValueError(f"operationId not found: {operation_id}")
        if len(matches) > 1:
            raise ValueError(f"operationId is not unique: {operation_id}")
        if not isinstance(override, dict):
            raise ValueError(f"override for {operation_id} must be an object")

        replacements = override.get("replace")
        if not isinstance(replacements, dict) or not replacements:
            raise ValueError(
                f"override for {operation_id} must contain a non-empty 'replace' object"
            )

        operation = matches[0][2]
        for relative_path, value in replacements.items():
            replace_operation_value(operation, relative_path, value, operation_id)

    return patched


def replace_operation_value(
    operation: dict,
    relative_path: object,
    value: object,
    operation_id: str,
) -> None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError(
            f"replacement paths for {operation_id} must be non-empty strings"
        )

    path_parts = relative_path.split(".")
    if any(not part for part in path_parts):
        raise ValueError(
            f"replacement path for {operation_id} contains an empty segment: "
            f"{relative_path}"
        )

    parent: object = operation
    for part in path_parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            raise ValueError(
                f"replacement target does not exist for {operation_id}: "
                f"{relative_path}"
            )
        parent = parent[part]

    final_part = path_parts[-1]
    if not isinstance(parent, dict) or final_part not in parent:
        raise ValueError(
            f"replacement target does not exist for {operation_id}: {relative_path}"
        )
    parent[final_part] = deepcopy(value)


def apply_openapi_patch_operation(document: object, operation: object) -> object:
    if not isinstance(operation, dict):
        raise ValueError("each patch operation must be an object")

    operation_name = operation.get("op")
    if not isinstance(operation_name, str):
        raise ValueError("patch operation is missing a string 'op'")
    operation_name = operation_name.casefold()

    path = operation.get("path")
    path_parts = json_pointer_parts(path)

    if operation_name == "add":
        require_patch_value(operation)
        return add_json_pointer_value(document, path_parts, operation["value"])

    if operation_name == "remove":
        if not path_parts:
            raise ValueError("remove cannot target the document root")
        return remove_json_pointer_value(document, path_parts)

    if operation_name == "replace":
        require_patch_value(operation)
        if not path_parts:
            return deepcopy(operation["value"])
        return replace_json_pointer_value(document, path_parts, operation["value"])

    if operation_name == "copy":
        source_parts = json_pointer_parts(operation.get("from"))
        return add_json_pointer_value(
            document,
            path_parts,
            deepcopy(get_json_pointer_value(document, source_parts)),
        )

    if operation_name == "move":
        source_parts = json_pointer_parts(operation.get("from"))
        if source_parts == path_parts:
            return document
        if path_parts[: len(source_parts)] == source_parts:
            raise ValueError("move cannot target a child of its source")
        value = deepcopy(get_json_pointer_value(document, source_parts))
        if not source_parts:
            raise ValueError("move cannot remove the document root")
        document = remove_json_pointer_value(document, source_parts)
        return add_json_pointer_value(document, path_parts, value)

    if operation_name == "test":
        require_patch_value(operation)
        actual = get_json_pointer_value(document, path_parts)
        if actual != operation["value"]:
            raise ValueError(f"test failed at {path or '<document root>'}")
        return document

    raise ValueError(f"unsupported patch operation '{operation_name}'")


def require_patch_value(operation: dict) -> None:
    if "value" not in operation:
        raise ValueError("patch operation is missing 'value'")


def json_pointer_parts(pointer: object) -> list[str]:
    if not isinstance(pointer, str):
        raise ValueError("JSON Pointer must be a string")
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or start with '/'")

    parts = []
    for raw_part in pointer[1:].split("/"):
        decoded = []
        index = 0
        while index < len(raw_part):
            character = raw_part[index]
            if character != "~":
                decoded.append(character)
                index += 1
                continue
            if index + 1 >= len(raw_part) or raw_part[index + 1] not in {"0", "1"}:
                raise ValueError(f"invalid JSON Pointer escape in '{pointer}'")
            decoded.append("~" if raw_part[index + 1] == "0" else "/")
            index += 2
        parts.append("".join(decoded))
    return parts


def get_json_pointer_value(document: object, parts: list[str]) -> object:
    current = document
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise ValueError(f"JSON Pointer target does not exist: /{'/'.join(parts)}")
            current = current[part]
        elif isinstance(current, list):
            index = json_pointer_array_index(part, len(current))
            current = current[index]
        else:
            raise ValueError("JSON Pointer traverses a scalar value")
    return current


def json_pointer_parent(document: object, parts: list[str]) -> tuple[object, str]:
    if not parts:
        raise ValueError("JSON Pointer must identify a child value")
    return get_json_pointer_value(document, parts[:-1]), parts[-1]


def json_pointer_array_index(part: str, length: int, *, allow_end: bool = False) -> int:
    if part == "-":
        if allow_end:
            return length
        raise ValueError("'-' is only valid for adding to an array")
    if not part or (len(part) > 1 and part.startswith("0")) or not all(
        character in "0123456789" for character in part
    ):
        raise ValueError(f"invalid JSON array index '{part}'")
    index = int(part)
    upper_bound = length if allow_end else length - 1
    if index < 0 or index > upper_bound:
        raise ValueError(f"JSON array index out of range: {part}")
    return index


def add_json_pointer_value(document: object, parts: list[str], value: object) -> object:
    if not parts:
        return deepcopy(value)
    parent, part = json_pointer_parent(document, parts)
    if isinstance(parent, dict):
        parent[part] = deepcopy(value)
        return document
    if isinstance(parent, list):
        parent.insert(
            json_pointer_array_index(part, len(parent), allow_end=True),
            deepcopy(value),
        )
        return document
    raise ValueError("JSON Pointer parent is not an object or array")


def remove_json_pointer_value(document: object, parts: list[str]) -> object:
    parent, part = json_pointer_parent(document, parts)
    if isinstance(parent, dict):
        if part not in parent:
            raise ValueError(f"JSON Pointer target does not exist: /{'/'.join(parts)}")
        del parent[part]
        return document
    if isinstance(parent, list):
        del parent[json_pointer_array_index(part, len(parent))]
        return document
    raise ValueError("JSON Pointer parent is not an object or array")


def replace_json_pointer_value(document: object, parts: list[str], value: object) -> object:
    parent, part = json_pointer_parent(document, parts)
    if isinstance(parent, dict):
        if part not in parent:
            raise ValueError(f"JSON Pointer target does not exist: /{'/'.join(parts)}")
        parent[part] = deepcopy(value)
        return document
    if isinstance(parent, list):
        parent[json_pointer_array_index(part, len(parent))] = deepcopy(value)
        return document
    raise ValueError("JSON Pointer parent is not an object or array")


def fetch_openapi_spec(url: str, timeout: float, verify_ssl: bool = True) -> dict:
    try:
        response = httpx.get(url, timeout=timeout, verify=verify_ssl)
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
    include_tools = normalized_tool_set(config.include_tools)
    exclude_tools = normalized_tool_set(config.exclude_tools)
    if not include_tags and not exclude_tags and not include_tools and not exclude_tools:
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
            if method.lower() not in HTTP_METHODS:
                continue
            operation_count += 1
            if operation_matches_filters(
                operation,
                include_tags,
                exclude_tags,
                include_tools,
                exclude_tools,
            ):
                filtered_path_item[method] = operation
                kept_count += 1
        if has_operation(filtered_path_item):
            filtered_paths[path] = filtered_path_item

    if operation_count and kept_count == 0:
        raise SystemExit(
            "OpenAPI filters removed every operation. "
            f"include-tags={list(config.include_tags)} "
            f"exclude-tags={list(config.exclude_tags)} "
            f"include-tools={list(config.include_tools)} "
            f"exclude-tools={list(config.exclude_tools)}"
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
    return operation_matches_filters(operation, include_tags, exclude_tags, set(), set())


def operation_matches_filters(
    operation: object,
    include_tags: set[str],
    exclude_tags: set[str],
    include_tools: set[str],
    exclude_tools: set[str],
) -> bool:
    if not isinstance(operation, dict):
        return False

    tags = normalized_tag_set(operation.get("tags") or [])
    tool_name = normalize_tool_name(operation.get("operationId"))

    if exclude_tags and tags.intersection(exclude_tags):
        return False
    if exclude_tools and tool_name in exclude_tools:
        return False

    include_match = (
        (bool(include_tags) and bool(tags.intersection(include_tags)))
        or (bool(include_tools) and tool_name in include_tools)
    )
    if (include_tags or include_tools) and not include_match:
        return False
    return True


def has_operation(path_item: dict) -> bool:
    return any(key.lower() in HTTP_METHODS for key in path_item)


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


def normalized_tool_set(values) -> set[str]:
    return {
        normalize_tool_name(value)
        for value in values
        if normalize_tool_name(value)
    }


def normalize_tool_name(value) -> str:
    return str(value).strip().casefold() if value is not None else ""


def build_operation_auth_rules(spec: dict, config: ServerConfig) -> OperationAuthRules:
    api_key_schemes = api_key_security_scheme_names(spec, config.api_key_header)
    root_security = spec.get("security") if "security" in spec else None
    rules: list[OperationAuthRule] = []

    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            method_name = method.lower()
            if method_name not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            security = (
                operation.get("security") if "security" in operation else root_security
            )
            rules.append(
                OperationAuthRule(
                    method=method_name,
                    path_template=normalize_url_path(path),
                    requires_api_key=security_requires_api_key(security, api_key_schemes),
                )
            )

    return OperationAuthRules(
        base_path=normalize_url_path(urlparse(config.api_base_url).path),
        rules=tuple(rules),
    )


def api_key_security_scheme_names(spec: dict, header_name: str) -> set[str]:
    schemes = ((spec.get("components") or {}).get("securitySchemes") or {})
    names = set()
    for scheme_name, scheme in schemes.items():
        if not isinstance(scheme, dict):
            continue
        if (
            str(scheme.get("type", "")).casefold() == "apikey"
            and str(scheme.get("in", "")).casefold() == "header"
            and str(scheme.get("name", "")).casefold() == header_name.casefold()
        ):
            names.add(str(scheme_name))

    if DEFAULT_API_KEY_SECURITY_SCHEME in schemes:
        names.add(DEFAULT_API_KEY_SECURITY_SCHEME)
    return names


def security_requires_api_key(security: object, api_key_schemes: set[str]) -> bool:
    if not security or not api_key_schemes:
        return False
    if not isinstance(security, list):
        return False

    has_requirement = False
    for requirement in security:
        if not isinstance(requirement, dict):
            continue
        has_requirement = True
        if not requirement:
            return False
        if not api_key_schemes.intersection(str(name) for name in requirement):
            return False
    return has_requirement


def path_matches_template(template: str, path: str) -> bool:
    template_parts = split_path(template)
    path_parts = split_path(path)
    if len(template_parts) != len(path_parts):
        return False

    return all(
        is_path_parameter(template_part) or template_part == path_part
        for template_part, path_part in zip(template_parts, path_parts)
    )


def split_path(path: str) -> list[str]:
    return [part for part in normalize_url_path(path).split("/") if part]


def is_path_parameter(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def normalize_url_path(path: str) -> str:
    value = "/" + str(path or "").strip("/")
    return value if value != "" else "/"


def api_key_for_current_request(
    config: ServerConfig, require_http_context: bool = True
) -> str | None:
    if config.api_key_mode == "none":
        return None

    if config.api_key_mode == "env":
        return os.getenv(config.api_key_env)

    if config.api_key_mode == "request-header":
        fallback_api_key = os.getenv(config.api_key_env)
        try:
            headers = get_http_headers()
        except RuntimeError as exc:
            if fallback_api_key:
                return fallback_api_key
            if not require_http_context:
                return None
            raise RuntimeError(
                f"{config.api_key_mode} mode requires an HTTP MCP transport so "
                f"callers can provide {config.api_key_header}."
            ) from exc
        return case_insensitive_header(headers, config.api_key_header) or fallback_api_key

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


def request_has_authentication(config: ServerConfig, request: HttpRequest) -> bool:
    return any(
        request.headers.get(header_name)
        for header_name in (config.api_key_header, "Authorization")
    )


def log_dataverse_request(
    config: ServerConfig, request: HttpRequest, authenticated: bool
) -> None:
    logger.info(
        "[DOME] Dataverse request %s %s (auth=%s)",
        request.method.upper(),
        single_line_text(request.url.path),
        "used" if authenticated else "not-used",
    )


def build_dome_log_config(config: ServerConfig) -> dict:
    log_level = (config.log_level or "INFO").upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "dome": {"format": "%(message)s"},
        },
        "handlers": {
            "dome": {
                "()": DomeLogHandler,
                "formatter": "dome",
                "stream": "ext://sys.stderr",
                "show_path": False,
                "rich_tracebacks": True,
                "tracebacks_max_frames": 3,
            },
        },
        "loggers": {
            "fastmcp": {
                "handlers": ["dome"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn": {
                "handlers": ["dome"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["dome"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["dome"],
                "level": log_level,
                "propagate": False,
            },
        },
    }


def configure_dome_logging(config: ServerConfig) -> None:
    logging.config.dictConfig(build_dome_log_config(config))


def single_line_text(value: object) -> str:
    return " ".join(str(value).splitlines())


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
    openapi_spec = load_openapi_spec(
        config.openapi_source,
        config.timeout,
        verify_ssl=not config.ignore_ssl_errors,
    )
    if config.openapi_patch_path:
        openapi_spec = apply_openapi_patch_file(
            openapi_spec,
            config.openapi_patch_path,
        )
    openapi_spec = filter_openapi_by_tags(openapi_spec, config)
    api_client = http_client.AsyncClient(
        base_url=config.api_base_url,
        headers=static_api_headers(),
        auth=DataverseApiKeyAuth(
            config, build_operation_auth_rules(openapi_spec, config)
        ),
        timeout=config.timeout,
        verify=not config.ignore_ssl_errors,
    )
    mcp = FastMCP.from_openapi(
        openapi_spec=openapi_spec,
        client=api_client,
        name=config.name,
    )
    mcp.instructions = load_mcp_instructions(config.instructions_file)
    return mcp


def print_served_tools(config: ServerConfig, mcp: FastMCP) -> None:
    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    print(
        f"{config.name} serving {len(tools)} MCP tool(s) from {config.openapi_source}",
        file=sys.stderr,
        flush=True,
    )
    if config.openapi_patch_path:
        print(
            f"OpenAPI patch: {config.openapi_patch_path}",
            file=sys.stderr,
            flush=True,
        )
    if config.include_tags or config.exclude_tags:
        print(
            "Tag filters: "
            f"include={list(config.include_tags) or '*'} "
            f"exclude={list(config.exclude_tags) or '[]'}",
            file=sys.stderr,
            flush=True,
        )
    if config.include_tools or config.exclude_tools:
        print(
            "Tool filters: "
            f"include={list(config.include_tools) or '*'} "
            f"exclude={list(config.exclude_tools) or '[]'}",
            file=sys.stderr,
            flush=True,
        )
    if not config.show_tools:
        return
    for tool in sorted(tools, key=lambda item: item.name):
        tags = sorted(str(tag) for tag in (tool.tags or []))
        tag_text = f" [{', '.join(tags)}]" if tags else ""
        description = first_line(tool.description)
        suffix = f" - {description}" if description else ""
        print(f"  - {tool.name}{tag_text}{suffix}", file=sys.stderr, flush=True)


def print_dome_banner(config: ServerConfig) -> None:
    console = Console(file=sys.stderr)
    endpoint = (
        "stdio"
        if config.transport == "stdio"
        else f"http://{config.host}:{config.port}{config.path}"
    )

    runtime = Table.grid(padding=(0, 1))
    runtime.add_column(style="bold cyan", justify="right")
    runtime.add_column(style="white")
    runtime.add_row("Server", config.name)
    runtime.add_row("Transport", config.transport)
    runtime.add_row("Endpoint", endpoint)
    runtime.add_row("OpenAPI", config.openapi_source)
    runtime.add_row("OpenAPI patch", config.openapi_patch_path or "none")
    runtime.add_row(
        "MCP instructions",
        (
            "built-in"
            if config.instructions_file is None
            else f"built-in + {config.instructions_file}"
        ),
    )
    runtime.add_row("Tool details", "enabled" if config.show_tools else "suppressed")
    runtime.add_row(
        "TLS verification",
        "disabled" if config.ignore_ssl_errors else "enabled",
    )

    content = Group(
        Align.center(Text(DOME_LOGO, style="bold cyan")),
        Align.center(Text("Dataverse OpenAPI MCP Engine", style="bold white")),
        Text(""),
        runtime,
    )
    console.print(
        Panel(
            content,
            border_style="cyan",
            title="DOME",
            title_align="center",
            padding=(1, 2),
        )
    )


def first_line(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().splitlines()[0].split())


def print_ssl_warning(config: ServerConfig) -> None:
    if not config.ignore_ssl_errors:
        return
    print(
        "WARNING: SSL certificate verification is disabled for OpenAPI and "
        "Dataverse API requests.",
        file=sys.stderr,
        flush=True,
    )


def run_server(config: ServerConfig, mcp: FastMCP) -> None:
    if config.transport == "stdio":
        mcp.run(show_banner=False)
        return

    kwargs = {
        "transport": config.transport,
        "host": config.host,
        "port": config.port,
        "path": config.path,
        "show_banner": False,
        "uvicorn_config": {"log_config": build_dome_log_config(config)},
    }
    mcp.run(**kwargs)


def main() -> None:
    config = build_config(parse_args())
    configure_dome_logging(config)
    print_ssl_warning(config)
    mcp = create_mcp_server(config)
    if config.show_banner:
        print_dome_banner(config)
    print_served_tools(config, mcp)
    run_server(config, mcp)


def run() -> None:
    try:
        main()
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    run()
