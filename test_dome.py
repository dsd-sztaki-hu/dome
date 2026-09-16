import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

from dome import (
    DataverseApiKeyAuth,
    OperationAuthRule,
    OperationAuthRules,
    ServerConfig,
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
        log_level=None,
        timeout=30,
        include_tags=(),
        exclude_tags=(),
    )


def apply_auth(auth: DataverseApiKeyAuth, request: httpx.Request) -> httpx.Request:
    async def run() -> httpx.Request:
        async for authenticated_request in auth.async_auth_flow(request):
            return authenticated_request
        raise AssertionError("auth flow yielded no request")

    return asyncio.run(run())


class DataverseApiKeyAuthTests(unittest.TestCase):
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
            authenticated_request = apply_auth(auth, request)

        self.assertNotIn("X-Dataverse-key", authenticated_request.headers)


if __name__ == "__main__":
    unittest.main()
