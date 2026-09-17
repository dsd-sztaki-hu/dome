import asyncio
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import dome
import fastmcp
import httpx

from dome import (
    DataverseApiKeyAuth,
    OperationAuthRule,
    OperationAuthRules,
    ServerConfig,
    create_mcp_server,
    fetch_openapi_spec,
    load_env_file,
    parse_args,
    print_dome_banner,
    print_served_tools,
    run,
    run_server,
)


def make_config() -> ServerConfig:
    return ServerConfig(
        name="test",
        openapi_source="openapi.json",
        api_base_url="https://beta.dataverse.org/api/",
        api_key_header="X-Dataverse-key",
        api_key_env="DATAVERSE_API_TOKEN",
        api_key_mode="env",
        transport="stdio",
        host="127.0.0.1",
        port=8000,
        path="/mcp",
        show_banner=True,
        log_level=None,
        timeout=30,
        include_tags=(),
        exclude_tags=(),
        ignore_ssl_errors=False,
        log_api_key_usage=False,
        show_tools=True,
    )


def apply_auth(auth: DataverseApiKeyAuth, request: httpx.Request) -> httpx.Request:
    async def run() -> httpx.Request:
        async for authenticated_request in auth.async_auth_flow(request):
            return authenticated_request
        raise AssertionError("auth flow yielded no request")

    return asyncio.run(run())


class DataverseApiKeyAuthTests(unittest.TestCase):
    def test_authenticated_operation_can_log_key_forwarding_without_secret(self) -> None:
        operation_auth = OperationAuthRules(
            base_path="/api",
            rules=(
                OperationAuthRule(
                    method="get",
                    path_template="/private",
                    requires_api_key=True,
                ),
            ),
        )
        auth = DataverseApiKeyAuth(
            replace(make_config(), log_api_key_usage=True), operation_auth
        )
        request = httpx.Request("GET", "https://beta.dataverse.org/api/private")

        with patch.dict(os.environ, {"DATAVERSE_API_TOKEN": "secret"}, clear=False):
            with self.assertLogs("fastmcp.dome", level="INFO") as logs:
                authenticated_request = apply_auth(auth, request)

        self.assertEqual(
            authenticated_request.headers["X-Dataverse-key"],
            "secret",
        )
        audit_log = "\n".join(logs.output)
        self.assertIn("GET /api/private", audit_log)
        self.assertIn("Dataverse API key forwarded", audit_log)
        self.assertNotIn("secret", audit_log)

    def test_authenticated_operation_receives_configured_api_key(self) -> None:
        operation_auth = OperationAuthRules(
            base_path="/api",
            rules=(
                OperationAuthRule(
                    method="get",
                    path_template="/private",
                    requires_api_key=True,
                ),
            ),
        )
        auth = DataverseApiKeyAuth(make_config(), operation_auth)
        request = httpx.Request("GET", "https://beta.dataverse.org/api/private")

        with patch.dict(os.environ, {"DATAVERSE_API_TOKEN": "secret"}, clear=False):
            authenticated_request = apply_auth(auth, request)

        self.assertEqual(
            authenticated_request.headers["X-Dataverse-key"],
            "secret",
        )

    def test_public_operation_does_not_receive_configured_api_key(self) -> None:
        operation_auth = OperationAuthRules(
            base_path="/api",
            rules=(
                OperationAuthRule(
                    method="get",
                    path_template="/public",
                    requires_api_key=False,
                ),
            ),
        )
        auth = DataverseApiKeyAuth(
            replace(make_config(), log_api_key_usage=True), operation_auth
        )
        request = httpx.Request(
            "GET",
            "https://beta.dataverse.org/api/public",
            headers={"x-dataverse-key": "stale-secret"},
        )

        with patch.dict(os.environ, {"DATAVERSE_API_TOKEN": "secret"}, clear=False):
            with self.assertNoLogs("fastmcp.dome", level="INFO"):
                authenticated_request = apply_auth(auth, request)

        self.assertNotIn("X-Dataverse-key", authenticated_request.headers)


if __name__ == "__main__":
    unittest.main()
