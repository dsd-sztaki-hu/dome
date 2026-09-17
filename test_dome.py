import asyncio
import io
import logging
import os
import re
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
    DomeLogHandler,
    OperationAuthRule,
    OperationAuthRules,
    ServerConfig,
    apply_openapi_patch_file,
    apply_openapi_patches,
    build_config,
    build_dome_log_config,
    create_mcp_server,
    fetch_openapi_spec,
    filter_openapi_by_tags,
    load_mcp_instructions,
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
        openapi_patch_path=None,
        instructions_file=None,
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
        self.assertIn("[DOME] Dataverse request GET /api/private (auth=used)", audit_log)
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


class DomeLoggingTests(unittest.TestCase):
    def test_handler_uses_fastmcp_style_format_without_newlines(self) -> None:
        output = io.StringIO()
        handler = DomeLogHandler(output)
        self.assertIsInstance(handler, RichHandler)
        self.assertTrue(handler.console.soft_wrap)
        record = logging.LogRecord(
            name="fastmcp.dome",
            level=logging.INFO,
            pathname="/tmp/dome.py",
            lineno=730,
            msg=(
                "[DOME] Dataverse request GET "
                "/api/mydata/retrieve/collectionList (auth=used)"
            ),
            args=(),
            exc_info=None,
        )

        handler.emit(record)

        rendered = output.getvalue()
        self.assertEqual(rendered.count("\n"), 1)
        self.assertIn(
            "INFO     [DOME] Dataverse request GET "
            "/api/mydata/retrieve/collectionList (auth=used)",
            rendered,
        )
        self.assertNotIn("dome.py:730", rendered)

    def test_shared_log_config_uses_dome_handler_without_source_paths(self) -> None:
        log_config = build_dome_log_config(make_config())

        self.assertIs(log_config["handlers"]["dome"]["()"], DomeLogHandler)
        self.assertFalse(log_config["handlers"]["dome"]["show_path"])
        for logger_name in ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access"):
            self.assertEqual(
                log_config["loggers"][logger_name]["handlers"],
                ["dome"],
            )

    def test_handler_shows_timestamp_on_every_line(self) -> None:
        output = io.StringIO()
        handler = DomeLogHandler(output)
        records = [
            logging.LogRecord(
                name="uvicorn.error",
                level=logging.INFO,
                pathname="/tmp/uvicorn/server.py",
                lineno=100,
                msg="First message",
                args=(),
                exc_info=None,
            ),
            logging.LogRecord(
                name="uvicorn.error",
                level=logging.INFO,
                pathname="/tmp/uvicorn/server.py",
                lineno=101,
                msg="Second message",
                args=(),
                exc_info=None,
            ),
        ]
        records[1].created = records[0].created

        for record in records:
            handler.emit(record)

        rendered = output.getvalue()
        timestamps = re.findall(
            r"\[\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\]",
            rendered,
        )
        self.assertEqual(len(timestamps), 2)


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


class OpenApiPatchTests(unittest.TestCase):
    def test_patch_can_replace_an_operation_request_body(self) -> None:
        spec = {
            "paths": {
                "/dataverses/{identifier}/datasets": {
                    "post": {
                        "operationId": "Dataverses_createDataset",
                        "requestBody": {
                            "content": {
                                "application/ld+json": {
                                    "schema": {"type": "string"}
                                },
                                "application/json": {
                                    "schema": {"type": "string"}
                                },
                            }
                        },
                    }
                }
            }
        }
        patches = {
            "operations": {
                "Dataverses_createDataset": {
                    "replace": {
                        "requestBody.content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": True,
                                }
                            }
                        }
                    }
                }
            }
        }

        patched = apply_openapi_patches(spec, patches)

        self.assertEqual(
            patched["paths"]["/dataverses/{identifier}/datasets"]["post"][
                "requestBody"
            ]["content"],
            patches["operations"]["Dataverses_createDataset"]["replace"][
                "requestBody.content"
            ],
        )
        self.assertEqual(
            list(
                spec["paths"]["/dataverses/{identifier}/datasets"]["post"][
                    "requestBody"
                ]["content"]
            ),
            ["application/ld+json", "application/json"],
        )

    def test_operation_override_can_replace_a_nested_media_type_schema(self) -> None:
        spec = {
            "paths": {
                "/dataverses/{identifier}/validateDatasetJson": {
                    "post": {
                        "operationId": "Dataverses_validateDatasetJson",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "string"}
                                }
                            }
                        },
                    }
                }
            }
        }

        patched = apply_openapi_patches(
            spec,
            {
                "operations": {
                    "Dataverses_validateDatasetJson": {
                        "replace": {
                            "requestBody.content.application/json.schema": {
                                "type": "object",
                                "additionalProperties": True,
                            }
                        }
                    }
                }
            },
        )

        self.assertEqual(
            patched["paths"]["/dataverses/{identifier}/validateDatasetJson"]["post"][
                "requestBody"
            ]["content"]["application/json"]["schema"],
            {"type": "object", "additionalProperties": True},
        )

    def test_patch_file_can_use_a_wrapped_patch_document(self) -> None:
        spec = {"paths": {}, "x-dome": {"enabled": False}}

        with TemporaryDirectory() as directory:
            patch_path = Path(directory) / "openapi-patches.json"
            patch_path.write_text(
                '{"patches": [{"op": "replace", '
                '"path": "/x-dome/enabled", "value": true}]}',
                encoding="utf-8",
            )

            patched = apply_openapi_patch_file(spec, str(patch_path))

        self.assertTrue(patched["x-dome"]["enabled"])

    def test_patch_supports_add_remove_and_copy_with_escaped_keys(self) -> None:
        spec = {"paths": {}, "x-dome": {"a/b": "value", "items": ["one"]}}

        patched = apply_openapi_patches(
            spec,
            [
                {"op": "add", "path": "/x-dome/items/-", "value": "two"},
                {"op": "copy", "from": "/x-dome/a~1b", "path": "/x-dome/copied"},
                {"op": "remove", "path": "/x-dome/a~1b"},
            ],
        )

        self.assertEqual(patched["x-dome"], {"items": ["one", "two"], "copied": "value"})

    def test_create_mcp_server_applies_patch_before_filtering(self) -> None:
        spec = {
            "paths": {
                "/datasets": {
                    "post": {
                        "operationId": "Datasets_create",
                        "tags": ["Datasets"],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "string"}
                                }
                            }
                        },
                    }
                }
            }
        }

        with TemporaryDirectory() as directory:
            patch_path = Path(directory) / "openapi-patches.json"
            patch_path.write_text(
                '[{"op": "replace", "path": '
                '"/paths/~1datasets/post/requestBody/content/application~1json/schema/type", '
                '"value": "object"}]',
                encoding="utf-8",
            )
            config = replace(make_config(), openapi_patch_path=str(patch_path))
            mcp = Mock()

            with patch("dome.load_openapi_spec", return_value=spec):
                with patch("dome.http_client.AsyncClient"):
                    with patch("dome.FastMCP.from_openapi", return_value=mcp) as factory:
                        self.assertIs(create_mcp_server(config), mcp)

        patched_spec = factory.call_args.kwargs["openapi_spec"]
        self.assertEqual(
            patched_spec["paths"]["/datasets"]["post"]["requestBody"]["content"][
                "application/json"
            ]["schema"]["type"],
            "object",
        )


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
            uvicorn_config={"log_config": build_dome_log_config(config)},
        )


class McpInstructionsTests(unittest.TestCase):
    def test_default_instructions_include_dataset_publication_safety_rule(self) -> None:
        instructions = load_mcp_instructions(None)

        self.assertIn(
            "Treat creating, editing, and saving a dataset as draft work",
            instructions,
        )
        self.assertIn(
            "Never publish, release, or otherwise make a dataset public",
            instructions,
        )

    def test_custom_instructions_are_appended_to_built_in_guidance(self) -> None:
        with TemporaryDirectory() as directory:
            instructions_path = Path(directory) / "instructions.md"
            instructions_path.write_text(
                "Use the institution's preferred dataverse for new datasets.",
                encoding="utf-8",
            )

            instructions = load_mcp_instructions(str(instructions_path))

        self.assertIn(
            "Treat creating, editing, and saving a dataset as draft work",
            instructions,
        )
        self.assertTrue(
            instructions.endswith(
                "Use the institution's preferred dataverse for new datasets."
            )
        )

    def test_create_mcp_server_attaches_instructions_to_fastmcp_server(self) -> None:
        spec = {"paths": {}}
        mcp = Mock()

        with TemporaryDirectory() as directory:
            instructions_path = Path(directory) / "instructions.md"
            instructions_path.write_text(
                "Ask for the collection alias when it is ambiguous.",
                encoding="utf-8",
            )
            config = replace(make_config(), instructions_file=str(instructions_path))

            with patch("dome.load_openapi_spec", return_value=spec):
                with patch("dome.http_client.AsyncClient"):
                    with patch("dome.FastMCP.from_openapi", return_value=mcp):
                        self.assertIs(create_mcp_server(config), mcp)

        self.assertIn(
            "Never publish, release, or otherwise make a dataset public",
            mcp.instructions,
        )
        self.assertIn("Ask for the collection alias when it is ambiguous.", mcp.instructions)

    def test_empty_custom_instructions_file_keeps_built_in_guidance(self) -> None:
        with TemporaryDirectory() as directory:
            instructions_path = Path(directory) / "empty.md"
            instructions_path.write_text("\n", encoding="utf-8")

            self.assertEqual(
                load_mcp_instructions(str(instructions_path)),
                dome.DEFAULT_MCP_INSTRUCTIONS,
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
                "MCP_INSTRUCTIONS_FILE": "",
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

    def test_mcp_openapi_patch_environment_setting_is_read(self) -> None:
        with patch.dict(
            os.environ,
            {"MCP_OPENAPI_PATCH": "./openapi-patches.json"},
            clear=False,
        ):
            with patch.object(sys, "argv", ["dome.py"]):
                args = parse_args()

        self.assertEqual(args.openapi_patch, "./openapi-patches.json")

    def test_mcp_instructions_file_environment_setting_is_read(self) -> None:
        with patch.dict(
            os.environ,
            {"MCP_INSTRUCTIONS_FILE": "./mcp-instructions.example.md"},
            clear=False,
        ):
            with patch.object(sys, "argv", ["dome.py"]):
                config = build_config(parse_args())

        self.assertEqual(
            config.instructions_file,
            str((dome.DEFAULT_ENV_PATH.parent / "mcp-instructions.example.md").resolve()),
        )

    def test_mcp_instructions_file_cli_setting_is_read(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["dome.py", "--instructions-file", "./mcp-instructions.example.md"],
        ):
            config = build_config(parse_args())

        self.assertEqual(
            config.instructions_file,
            str((dome.DEFAULT_ENV_PATH.parent / "mcp-instructions.example.md").resolve()),
        )


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
