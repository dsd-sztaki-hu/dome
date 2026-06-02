# Dataverse FastMCP

Configurable FastMCP server for exposing a Dataverse OpenAPI document as MCP
tools.

The server loads an OpenAPI JSON file at startup. You do not need to regenerate
this project when the OpenAPI file changes; restart the server with the updated
`--openapi` path.

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

If `--openapi` is omitted, the server uses `./openapi.json`.

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

```env
# Data steward: create, edit, search, publish, and organize datasets/files.
INCLUDE_TAGS=Datasets,Dataverses,Files,Search,Licenses,Dataset Fields
EXCLUDE_TAGS=Admin,Users,Roles,Notifications
```

```env
# Read-only discovery: browsing, search, download, and installation info.
# Use API_KEY_MODE=none only when all selected endpoints are public.
INCLUDE_TAGS=Info,Search,Access,Datasets,Dataverses,Files,Licenses
EXCLUDE_TAGS=Admin,Users,Roles,Notifications,Dataset Fields
```

```env
# Repository admin: administration plus users, roles, and notifications.
INCLUDE_TAGS=Admin,Users,Roles,Notifications,Info
```

```env
# Data access: file and dataset download/access workflows.
INCLUDE_TAGS=Access,Files,Datasets
EXCLUDE_TAGS=Admin,Users,Roles,Notifications
```

```env
# Metadata curator: metadata fields, dataset metadata, and licenses.
INCLUDE_TAGS=Datasets,Dataset Fields,Licenses,Info
EXCLUDE_TAGS=Admin,Users,Roles,Notifications,Access
```

```env
# Full API except admin.
EXCLUDE_TAGS=Admin
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
