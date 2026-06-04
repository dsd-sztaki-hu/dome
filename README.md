# Dataverse FastMCP

Configurable FastMCP server for exposing a Dataverse OpenAPI document as MCP
tools.

The server loads an OpenAPI JSON document at startup from a local file path or
an HTTP(S) URL. You do not need to regenerate this project when the OpenAPI file
changes; restart the server with the updated `--openapi` source.

## Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run

Central HTTP service mode, where each MCP caller sends their own Dataverse API
token:

```bash
python server.py \
  --openapi ../dataverse/target/classes/META-INF/openapi.json \
  --api-base-url http://127.0.0.1:8080/api/ \
  --api-key-mode request-header \
  --include-tag Datasets \
  --include-tag Files \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000
```

The MCP endpoint is:

```text
http://127.0.0.1:8000/mcp
```

At startup, the server writes the exact MCP tool list it will serve to stderr,
including the active include/exclude tag filters.

Local stdio mode, where the user running the process owns the Dataverse API
token:

```bash
export DATAVERSE_API_TOKEN="your-token"
python server.py \
  --openapi ../dataverse/target/classes/META-INF/openapi.json \
  --api-base-url http://127.0.0.1:8080/api/ \
  --api-key-mode env \
  --transport stdio
```

If `--openapi` is omitted, the server uses `./openapi.json`. You can also pass
an HTTP(S) URL, for example `--openapi http://127.0.0.1:8080/openapi.json`.

## Environment

You can use environment variables instead of CLI options:

```env
OPENAPI_PATH=../dataverse/target/classes/META-INF/openapi.json
API_BASE_URL=http://127.0.0.1:8080/api/
API_KEY_MODE=request-header
API_KEY_HEADER=X-Dataverse-key
API_KEY_ENV=DATAVERSE_API_TOKEN
FASTMCP_TRANSPORT=streamable-http
FASTMCP_HOST=127.0.0.1
FASTMCP_PORT=8000
FASTMCP_PATH=/mcp
INCLUDE_TAGS=Datasets,Files
# EXCLUDE_TAGS=Admin
```

The server automatically loads a `.env` file placed next to `server.py` before
reading CLI defaults. Values already exported in the process environment take
precedence over `.env` values.

`OPENAPI_PATH` can also be an HTTP(S) URL:

```env
OPENAPI_PATH=http://127.0.0.1:8080/openapi.json
```

API key modes:

- `request-header`: read `X-Dataverse-key` from each incoming HTTP MCP request.
- `env`: read `DATAVERSE_API_TOKEN` from the server process.
- `none`: do not add a Dataverse API key to upstream requests.

## Tag Filtering

The server can filter the OpenAPI document before it creates MCP tools. This is
useful when the full Dataverse API would expose too many tools to a client.

Expose only selected resource groups:

```bash
python server.py \
  --openapi ../dataverse/target/classes/META-INF/openapi.json \
  --include-tag Datasets \
  --include-tag Files \
  --api-key-mode request-header \
  --transport streamable-http
```

Exclude administrative tools:

```bash
python server.py \
  --openapi ../dataverse/target/classes/META-INF/openapi.json \
  --exclude-tag Admin \
  --api-key-mode request-header \
  --transport streamable-http
```

Environment variable equivalents:

```env
INCLUDE_TAGS=Datasets,Files
EXCLUDE_TAGS=Admin
```

Suggested profiles:

Current generated tag groups are:

```text
Access, Admin, Users, Datasets, Dataset Fields, Dataverse Featured Items,
Dataverses, Files, External Tools, Info, Licenses, Metadata, Notifications,
Roles, Search, Workflows
```

```env
# Data steward: create, edit, search, publish, and organize datasets/files.
INCLUDE_TAGS=Datasets,Dataverses,Files,Search,Licenses,Dataset Fields,Metadata
EXCLUDE_TAGS=Admin,Users,Roles,Notifications,Workflows,External Tools,Dataverse Featured Items
```

```env
# Read-only discovery: browsing, search, download, and installation info.
# Use API_KEY_MODE=none only when all selected endpoints are public.
INCLUDE_TAGS=Info,Search,Access,Datasets,Dataverses,Files,Licenses,Metadata
EXCLUDE_TAGS=Admin,Users,Roles,Notifications,Dataset Fields,Workflows,External Tools,Dataverse Featured Items
```

```env
# Repository admin: administration plus users, roles, notifications, workflows,
# integrations, and featured-item management.
INCLUDE_TAGS=Admin,Users,Roles,Notifications,Info,Workflows,External Tools,Dataverse Featured Items,Metadata
```

```env
# Data access: file and dataset download/access workflows.
INCLUDE_TAGS=Access,Files,Datasets,Licenses
EXCLUDE_TAGS=Admin,Users,Roles,Notifications,Workflows,External Tools,Dataverse Featured Items
```

```env
# Metadata curator: metadata blocks, dataset metadata, field definitions,
# licenses, and controlled vocabularies.
INCLUDE_TAGS=Datasets,Dataverses,Dataset Fields,Metadata,Licenses,Info
EXCLUDE_TAGS=Admin,Users,Roles,Notifications,Access,Workflows,External Tools,Dataverse Featured Items
```

```env
# Collection manager: dataverse collection setup, featured items, dataset
# organization, and collection-facing metadata.
INCLUDE_TAGS=Dataverses,Dataverse Featured Items,Datasets,Files,Licenses,Metadata
EXCLUDE_TAGS=Admin,Users,Roles,Notifications,Workflows,External Tools
```

```env
# Full API except administrative groups.
EXCLUDE_TAGS=Admin,Users,Roles,Notifications,Workflows,External Tools,Dataverse Featured Items
```

`--include-tag` and `--exclude-tag` can be repeated. If the filters remove every
operation, the server exits with a clear error.

## MCP Client Configuration

When `API_KEY_MODE=request-header`, each MCP client must send the user's
Dataverse API token as an HTTP header on every MCP request:

```http
X-Dataverse-key: <user Dataverse API token>
```

For a central service, replace the local URL in these examples with the deployed
HTTPS endpoint. Keep the token in each user's local client configuration or
secret store. Do not commit real Dataverse API tokens.

### Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.dataverse]
url = "http://127.0.0.1:8000/mcp"
env_http_headers = { "X-Dataverse-key" = "DATAVERSE_API_TOKEN" }
```

Then set the token before starting Codex:

```bash
export DATAVERSE_API_TOKEN="your-token"
```

### Claude Code

Add the remote HTTP server with a per-user header:

```bash
claude mcp add --transport http dataverse http://127.0.0.1:8000/mcp \
  --header "X-Dataverse-key: your-token"
```

Alternatively, add JSON config:

```bash
claude mcp add-json dataverse \
  '{"type":"http","url":"http://127.0.0.1:8000/mcp","headers":{"X-Dataverse-key":"your-token"}}'
```

### Gemini CLI

Add the remote HTTP server with a per-user header:

```bash
gemini mcp add --transport http \
  --header "X-Dataverse-key: your-token" \
  dataverse http://127.0.0.1:8000/mcp
```

### opencode

Add this to `opencode.json` or `opencode.jsonc`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "dataverse": {
      "type": "remote",
      "url": "http://127.0.0.1:8000/mcp",
      "enabled": true,
      "headers": {
        "X-Dataverse-key": "{env:DATAVERSE_API_TOKEN}"
      }
    }
  }
}
```

Then set the token before starting opencode:

```bash
export DATAVERSE_API_TOKEN="your-token"
```
