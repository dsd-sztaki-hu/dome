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


class RunServerTests(unittest.TestCase):
    def test_stdio_startup_suppresses_framework_banner(self) -> None:
        mcp = Mock()

        run_server(mcp=mcp, config=make_config())

        mcp.run.assert_called_once_with(show_banner=False)

    def test_http_startup_suppresses_framework_banner(self) -> None:
        mcp = Mock()
        config = replace(make_config(), transport="streamable-http")

        run_server(mcp=mcp, config=config)

        mcp.run.assert_called_once_with(
            transport="streamable-http",
            host="127.0.0.1",
            port=8000,
            path="/mcp",
            show_banner=False,
        )


class EntrypointTests(unittest.TestCase):
    def test_keyboard_interrupt_is_clean_at_cli_boundary(self) -> None:
        with patch("dome.main", side_effect=KeyboardInterrupt):
            run()


class ClientCompatibilityTests(unittest.TestCase):
    def test_openapi_client_matches_fastmcp_runtime(self) -> None:
        major_version = int(fastmcp.__version__.split(".", 1)[0])
        expected_module = "httpx2" if major_version >= 4 else "httpx"

        self.assertEqual(dome.http_client.__name__, expected_module)


class SslConfigurationTests(unittest.TestCase):
    def test_openapi_fetch_can_disable_certificate_verification(self) -> None:
        response = Mock()
        response.json.return_value = {"paths": {}}

        with patch("dome.httpx.get", return_value=response) as http_get:
            spec = fetch_openapi_spec(
                "https://example.test/openapi.json",
                timeout=30,
                verify_ssl=False,
            )

        self.assertEqual(spec, {"paths": {}})
        http_get.assert_called_once_with(
            "https://example.test/openapi.json",
            timeout=30,
            verify=False,
        )

    def test_upstream_client_can_disable_certificate_verification(self) -> None:
        config = replace(
            make_config(),
            openapi_source="https://example.test/openapi.json",
            ignore_ssl_errors=True,
        )
        mcp = Mock()

        with patch("dome.load_openapi_spec", return_value={"paths": {}}):
            with patch("dome.http_client.AsyncClient") as async_client:
                with patch("dome.FastMCP.from_openapi", return_value=mcp):
                    self.assertIs(create_mcp_server(config), mcp)

        self.assertFalse(async_client.call_args.kwargs["verify"])


class BannerTests(unittest.TestCase):
    def test_dome_banner_contains_runtime_identity(self) -> None:
        output = io.StringIO()

        with redirect_stderr(output):
            print_dome_banner(make_config())

        banner = output.getvalue()
        self.assertIn("DOME", banner)
        self.assertIn("Dataverse OpenAPI MCP Engine", banner)
        self.assertIn("stdio", banner)
        self.assertIn("openapi.json", banner)


class ToolListingTests(unittest.TestCase):
    def test_tool_details_can_be_suppressed_while_summary_remains(self) -> None:
        mcp = Mock()
        mcp.list_tools = AsyncMock(
            return_value=[
                SimpleNamespace(
                    name="private-tool",
                    tags=[],
                    description="A private tool",
                ),
            ]
        )
        config = replace(make_config(), show_tools=False)
        output = io.StringIO()

        with redirect_stderr(output):
            print_served_tools(config, mcp)

        self.assertIn("serving 1 MCP tool(s)", output.getvalue())
        self.assertNotIn("private-tool", output.getvalue())


class ArgumentParsingTests(unittest.TestCase):
    def test_mcp_ignore_ssl_errors_environment_setting_is_read(self) -> None:
        with patch.dict(os.environ, {"MCP_IGNORE_SSL_ERRORS": "true"}, clear=False):
            with patch.object(sys, "argv", ["dome.py"]):
                args = parse_args()

        self.assertTrue(args.ignore_ssl_errors)

    def test_no_ignore_ssl_errors_cli_flag_overrides_environment(self) -> None:
        with patch.dict(os.environ, {"MCP_IGNORE_SSL_ERRORS": "true"}, clear=False):
            with patch.object(sys, "argv", ["dome.py", "--no-ignore-ssl-errors"]):
                args = parse_args()

        self.assertFalse(args.ignore_ssl_errors)

    def test_mcp_log_api_key_usage_environment_setting_is_read(self) -> None:
        with patch.dict(os.environ, {"MCP_LOG_API_KEY_USAGE": "true"}, clear=False):
            with patch.object(sys, "argv", ["dome.py"]):
                args = parse_args()

        self.assertTrue(args.log_api_key_usage)

    def test_no_log_api_key_usage_cli_flag_overrides_environment(self) -> None:
        with patch.dict(os.environ, {"MCP_LOG_API_KEY_USAGE": "true"}, clear=False):
            with patch.object(sys, "argv", ["dome.py", "--no-log-api-key-usage"]):
                args = parse_args()

        self.assertFalse(args.log_api_key_usage)

    def test_mcp_show_tools_environment_setting_is_read(self) -> None:
        with patch.dict(os.environ, {"MCP_SHOW_TOOLS": "false"}, clear=False):
            with patch.object(sys, "argv", ["dome.py"]):
                args = parse_args()

        self.assertFalse(args.show_tools)

    def test_no_show_tools_cli_flag_overrides_environment(self) -> None:
        with patch.dict(os.environ, {"MCP_SHOW_TOOLS": "true"}, clear=False):
            with patch.object(sys, "argv", ["dome.py", "--no-show-tools"]):
                args = parse_args()

        self.assertFalse(args.show_tools)

    def test_show_tools_cli_flag_enables_listing(self) -> None:
        with patch.dict(os.environ, {"MCP_SHOW_TOOLS": "false"}, clear=False):
            with patch.object(sys, "argv", ["dome.py", "--show-tools"]):
                args = parse_args()

        self.assertTrue(args.show_tools)

    def test_mcp_show_banner_environment_setting_is_read(self) -> None:
        with patch.dict(os.environ, {"MCP_SHOW_BANNER": "false"}, clear=False):
            with patch.object(sys, "argv", ["dome.py"]):
                args = parse_args()

        self.assertFalse(args.show_banner)

    def test_no_show_banner_cli_flag_overrides_environment(self) -> None:
        with patch.dict(os.environ, {"MCP_SHOW_BANNER": "true"}, clear=False):
            with patch.object(sys, "argv", ["dome.py", "--no-show-banner"]):
                args = parse_args()

        self.assertFalse(args.show_banner)


class EnvironmentLoadingTests(unittest.TestCase):
    def test_last_duplicate_env_entry_wins_within_env_file(self) -> None:
        variable_name = "DOME_TEST_DUPLICATE_ENV_VALUE"

        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                f"{variable_name}=first\n{variable_name}=last\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(variable_name, None)
                load_env_file(env_path)

                self.assertEqual(os.environ[variable_name], "last")


if __name__ == "__main__":
    unittest.main()
