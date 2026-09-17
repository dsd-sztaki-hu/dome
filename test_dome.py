import asyncio
import io
import logging
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
from rich.logging import RichHandler

from dome import (
    DataverseApiKeyAuth,
    DataverseRequestLogHandler,
    OperationAuthRule,
    OperationAuthRules,
    ServerConfig,
    build_config,
    create_mcp_server,
    fetch_openapi_spec,
    filter_openapi_by_tags,
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
        include_tools=(),
        exclude_tools=(),
        ignore_ssl_errors=False,
        log_dataverse_requests=False,
        show_tools=True,
    )


def apply_auth(auth: DataverseApiKeyAuth, request: httpx.Request) -> httpx.Request:
    async def run() -> httpx.Request:
        async for authenticated_request in auth.async_auth_flow(request):
            return authenticated_request
        raise AssertionError("auth flow yielded no request")

    return asyncio.run(run())


class DataverseApiKeyAuthTests(unittest.TestCase):
    def test_authenticated_operation_logging_includes_auth_status_without_secret(self) -> None:
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
            replace(make_config(), log_dataverse_requests=True), operation_auth
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
        self.assertIn("Dataverse request GET /api/private (auth=used)", audit_log)
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
        auth = DataverseApiKeyAuth(make_config(), operation_auth)
        request = httpx.Request(
            "GET",
            "https://beta.dataverse.org/api/public",
            headers={"x-dataverse-key": "stale-secret"},
        )

        with patch.dict(os.environ, {"DATAVERSE_API_TOKEN": "secret"}, clear=False):
            with self.assertNoLogs("fastmcp.dome", level="INFO"):
                authenticated_request = apply_auth(auth, request)

        self.assertNotIn("X-Dataverse-key", authenticated_request.headers)

    def test_public_operation_logging_includes_missing_auth_status(self) -> None:
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
            replace(make_config(), log_dataverse_requests=True), operation_auth
        )
        request = httpx.Request(
            "GET",
            "https://beta.dataverse.org/api/public",
            headers={"x-dataverse-key": "stale-secret"},
        )

        with patch.dict(os.environ, {"DATAVERSE_API_TOKEN": "secret"}, clear=False):
            with self.assertLogs("fastmcp.dome", level="INFO") as logs:
                authenticated_request = apply_auth(auth, request)

        self.assertNotIn("X-Dataverse-key", authenticated_request.headers)
        audit_log = "\n".join(logs.output)
        self.assertIn("Dataverse request GET /api/public (auth=not-used)", audit_log)
        self.assertNotIn("stale-secret", audit_log)
        self.assertNotIn("secret", audit_log)


class DataverseRequestLoggingTests(unittest.TestCase):
    def test_handler_uses_fastmcp_style_format_without_newlines(self) -> None:
        output = io.StringIO()
        handler = DataverseRequestLogHandler(output)
        self.assertIsInstance(handler, RichHandler)
        self.assertTrue(handler.console.soft_wrap)
        record = logging.LogRecord(
            name="fastmcp.dome",
            level=logging.INFO,
            pathname="/tmp/dome.py",
            lineno=730,
            msg=(
                "[Dataverse DOME MCP] Dataverse request GET "
                "/api/mydata/retrieve/collectionList (auth=used)"
            ),
            args=(),
            exc_info=None,
        )

        handler.emit(record)

        rendered = output.getvalue()
        self.assertEqual(rendered.count("\n"), 1)
        self.assertIn(
            "INFO     [Dataverse DOME MCP] Dataverse request GET "
            "/api/mydata/retrieve/collectionList (auth=used) dome.py:730",
            rendered,
        )


class OpenApiFilterTests(unittest.TestCase):
    def test_include_tags_work_without_exclude_tags(self) -> None:
        spec = {
            "paths": {
                "/datasets": {
                    "get": {
                        "operationId": "Datasets_list",
                        "tags": ["Datasets"],
                    }
                },
                "/users": {
                    "get": {
                        "operationId": "Users_list",
                        "tags": ["Users"],
                    }
                },
            }
        }

        filtered = filter_openapi_by_tags(
            spec,
            replace(make_config(), include_tags=("Datasets",)),
        )

        self.assertEqual(set(filtered["paths"]), {"/datasets"})

    def test_exclude_tags_work_without_include_tags(self) -> None:
        spec = {
            "paths": {
                "/datasets": {
                    "get": {
                        "operationId": "Datasets_list",
                        "tags": ["Datasets"],
                    }
                },
                "/users": {
                    "get": {
                        "operationId": "Users_list",
                        "tags": ["Users"],
                    }
                },
            }
        }

        filtered = filter_openapi_by_tags(
            spec,
            replace(make_config(), exclude_tags=("Users",)),
        )

        self.assertEqual(set(filtered["paths"]), {"/datasets"})

    def test_include_and_exclude_tags_can_be_combined(self) -> None:
        spec = {
            "paths": {
                "/datasets": {
                    "get": {
                        "operationId": "Datasets_list",
                        "tags": ["Datasets"],
                    }
                },
                "/users": {
                    "get": {
                        "operationId": "Users_list",
                        "tags": ["Users"],
                    }
                },
            }
        }

        filtered = filter_openapi_by_tags(
            spec,
            replace(
                make_config(),
                include_tags=("Datasets", "Users"),
                exclude_tags=("Users",),
            ),
        )

        self.assertEqual(set(filtered["paths"]), {"/datasets"})

    def test_include_tools_extend_included_tag_operations(self) -> None:
        spec = {
            "paths": {
                "/datasets": {
                    "get": {
                        "operationId": "Datasets_list",
                        "tags": ["Datasets"],
                    }
                },
                "/mydata/retrieve": {
                    "get": {
                        "operationId": "DataRetrieverAPI_retrieveMyDataAsJsonString",
                        "tags": ["Users"],
                    }
                },
                "/users/other": {
                    "get": {
                        "operationId": "Users_otherOperation",
                        "tags": ["Users"],
                    }
                },
            }
        }
        config = replace(
            make_config(),
            include_tags=("Datasets",),
            include_tools=("DataRetrieverAPI_retrieveMyDataAsJsonString",),
        )

        filtered = filter_openapi_by_tags(spec, config)

        operation_ids = {
            operation["operationId"]
            for path_item in filtered["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and "operationId" in operation
        }
        self.assertEqual(
            operation_ids,
            {
                "Datasets_list",
                "DataRetrieverAPI_retrieveMyDataAsJsonString",
            },
        )

    def test_include_tools_can_select_specific_operations(self) -> None:
        spec = {
            "tags": [{"name": "Users"}, {"name": "Datasets"}],
            "paths": {
                "/mydata/retrieve": {
                    "get": {
                        "operationId": "DataRetrieverAPI_retrieveMyDataAsJsonString",
                        "tags": ["Users"],
                    }
                },
                "/mydata/retrieve/collectionList": {
                    "get": {
                        "operationId": "DataRetrieverAPI_retrieveMyCollectionList",
                        "tags": ["Users"],
                    }
                },
                "/users/other": {
                    "get": {
                        "operationId": "Users_otherOperation",
                        "tags": ["Users"],
                    }
                },
                "/datasets": {
                    "get": {
                        "operationId": "Datasets_list",
                        "tags": ["Datasets"],
                    }
                },
            },
        }
        config = replace(
            make_config(),
            include_tools=(
                "DataRetrieverAPI_retrieveMyCollectionList",
                "DataRetrieverAPI_retrieveMyDataAsJsonString",
            ),
        )

        filtered = filter_openapi_by_tags(spec, config)

        operation_ids = {
            operation["operationId"]
            for path_item in filtered["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and "operationId" in operation
        }
        self.assertEqual(
            operation_ids,
            {
                "DataRetrieverAPI_retrieveMyCollectionList",
                "DataRetrieverAPI_retrieveMyDataAsJsonString",
            },
        )
        self.assertEqual(filtered["tags"], [{"name": "Users"}])

    def test_exclude_tool_removes_one_operation_from_an_included_tag(self) -> None:
        spec = {
            "paths": {
                "/users/one": {
                    "get": {
                        "operationId": "Users_oneOperation",
                        "tags": ["Users"],
                    }
                },
                "/users/two": {
                    "get": {
                        "operationId": "Users_twoOperation",
                        "tags": ["Users"],
                    }
                },
            }
        }
        config = replace(
            make_config(),
            include_tags=("Users",),
            exclude_tools=("Users_twoOperation",),
        )

        filtered = filter_openapi_by_tags(spec, config)

        self.assertIn("/users/one", filtered["paths"])
        self.assertNotIn("/users/two", filtered["paths"])

    def test_exclude_tools_work_without_include_filters(self) -> None:
        spec = {
            "paths": {
                "/users/one": {
                    "get": {
                        "operationId": "Users_oneOperation",
                        "tags": ["Users"],
                    }
                },
                "/users/two": {
                    "get": {
                        "operationId": "Users_twoOperation",
                        "tags": ["Users"],
                    }
                },
            }
        }

        filtered = filter_openapi_by_tags(
            spec,
            replace(make_config(), exclude_tools=("Users_twoOperation",)),
        )

        self.assertEqual(set(filtered["paths"]), {"/users/one"})


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
    def test_tool_filters_accept_cli_and_environment_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "INCLUDE_TOOLS": "env-tool,shared-tool",
                "EXCLUDE_TOOLS": "env-hidden",
            },
            clear=False,
        ):
            with patch.object(
                sys,
                "argv",
                [
                    "dome.py",
                    "--include-tool",
                    "cli-tool",
                    "--exclude-tool",
                    "cli-hidden",
                    "--api-key-mode",
                    "none",
                ],
            ):
                config = build_config(parse_args())

        self.assertEqual(
            config.include_tools,
            ("cli-tool", "env-tool", "shared-tool"),
        )
        self.assertEqual(config.exclude_tools, ("cli-hidden", "env-hidden"))

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

    def test_mcp_log_dataverse_requests_environment_setting_is_read(self) -> None:
        with patch.dict(
            os.environ, {"MCP_LOG_DATAVERSE_REQUESTS": "true"}, clear=False
        ):
            with patch.object(sys, "argv", ["dome.py"]):
                args = parse_args()

        self.assertTrue(args.log_dataverse_requests)

    def test_no_log_dataverse_requests_cli_flag_overrides_environment(self) -> None:
        with patch.dict(
            os.environ, {"MCP_LOG_DATAVERSE_REQUESTS": "true"}, clear=False
        ):
            with patch.object(
                sys, "argv", ["dome.py", "--no-log-dataverse-requests"]
            ):
                args = parse_args()

        self.assertFalse(args.log_dataverse_requests)

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
