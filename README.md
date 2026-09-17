# Dataverse OpenAPI MCP Engine (DOME) - A Configurable Dataverse MCP Server


DOME is a configurable MCP engine that turns a Dataverse OpenAPI document into MCP tools,
making selected parts of the Dataverse API available to AI agents, agent harnesses, and other
MCP clients.

The Dataverse OpenAPI specification defines more than 500 endpoints, but many are administrative or
otherwise unnecessary for a given client or use case. DOME lets you control which parts of the API
are exposed as MCP tools by filtering OpenAPI operations by their functional groups or by selecting
individual operation names.

For example, a typical data steward may need access to search, dataset creation, metadata editing,
and file upload, while having no need for user management or system settings. DOME can expose only
those relevant capabilities, keeping the MCP tool surface focused on the tasks the client actually
needs.


## Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run

### Central HTTP service mode

If you want to run a central HTTP service next to your Dataverse installation that multiple MCP
clients can call, use `--api-key-mode request-header` when starting DOME:


```bash
python dome.py \
  --openapi http://127.0.0.1:8080/openapi \
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

At startup, DOME writes the served tool count to stderr, including the active
include/exclude filters. Individual tool names and descriptions are hidden
by default. Add `--show-tools` to the command to enable the full listing.

DOME also displays a branded startup banner by default. Use `--no-show-banner`
or set `MCP_SHOW_BANNER=false` to suppress it independently of the tool list.

The same central-service configuration can be placed in a `.env` file next to
`dome.py` instead of passing the CLI options:

```env
OPENAPI_PATH=http://127.0.0.1:8080/openapi
API_BASE_URL=http://127.0.0.1:8080/api/
API_KEY_MODE=request-header
INCLUDE_TAGS=Datasets,Files
MCP_TRANSPORT=streamable-http
MCP_HOST=127.0.0.1
MCP_PORT=8000
# Show the DOME startup banner; set false to suppress it.
MCP_SHOW_BANNER=true
# Show individual tool names and descriptions; set true to enable listing.
MCP_SHOW_TOOLS=false
# Log each MCP-to-Dataverse request with its authentication status; credentials
# are never logged.
MCP_LOG_DATAVERSE_REQUESTS=false
# Disable TLS certificate verification; use only on a trusted network.
MCP_IGNORE_SSL_ERRORS=false
# Optional exact OpenAPI operationId filters (the generated MCP tool names).
# INCLUDE_TOOLS=DataRetrieverAPI_retrieveMyCollectionList,DataRetrieverAPI_retrieveMyDataAsJsonString
# EXCLUDE_TOOLS=Users_sensitiveOperation
```

Then run:

```bash
python dome.py
```

### Local stdio mode

DOME can also run locally on the same machine where the MC client (eg. Codex or Claude Code) is
running. In this mode, the client and server communicate over `stdio`, and the server reads
the Dataverse API token from an environment variable


```bash
export DATAVERSE_API_TOKEN="your-token"
python dome.py \
  --openapi http://127.0.0.1:8080/openapi \
  --api-base-url http://127.0.0.1:8080/api/ \
  --api-key-mode env \
  --transport stdio
```

If `--openapi` is omitted, the server uses `./openapi.json`. You can also pass
an HTTP(S) URL, for example `--openapi http://127.0.0.1:8080/openapi.json`.

## Environment

You can use environment variables instead of CLI options:

```env
OPENAPI_PATH=http://127.0.0.1:8080/openapi
API_BASE_URL=http://127.0.0.1:8080/api/
API_KEY_MODE=request-header
API_KEY_HEADER=X-Dataverse-key
API_KEY_ENV=DATAVERSE_API_TOKEN
MCP_TRANSPORT=streamable-http
MCP_HOST=127.0.0.1
MCP_PORT=8000
MCP_PATH=/mcp
# Show the DOME startup banner; set false to suppress it.
MCP_SHOW_BANNER=true
# Show individual tool names and descriptions; set true to enable listing.
MCP_SHOW_TOOLS=false
# Log each MCP-to-Dataverse request with its authentication status; credentials
# are never logged.
MCP_LOG_DATAVERSE_REQUESTS=false
# Disable TLS certificate verification; use only on a trusted network.
MCP_IGNORE_SSL_ERRORS=false
INCLUDE_TAGS=Datasets,Files
# EXCLUDE_TAGS=Admin
# INCLUDE_TOOLS=DataRetrieverAPI_retrieveMyCollectionList,DataRetrieverAPI_retrieveMyDataAsJsonString
# EXCLUDE_TOOLS=Users_sensitiveOperation
```

The server automatically loads a `.env` file placed next to `dome.py` before
reading CLI defaults. Values already exported in the process environment take
precedence over `.env` values. If a variable appears more than once in the
`.env` file, the last entry wins.

`OPENAPI_PATH` can either be path to a local file

```env
OPENAPI_PATH=./openapi.json
```

or a URL

```env
OPENAPI_PATH=http://127.0.0.1:8080/openapi.json
```

API key modes control how DOME obtains a Dataverse API token. The OpenAPI
operation's `security` declaration controls whether DOME forwards that token to
the upstream Dataverse request.

- `request-header`: for HTTP MCP transports, read `X-Dataverse-key` from the
  incoming MCP request, falling back to `DATAVERSE_API_TOKEN` when it is set.
- `env`: read `DATAVERSE_API_TOKEN` from the DOME server process.
- `none`: do not add a Dataverse API key to upstream requests.

The agent calls MCP tools normally; it does not provide the token as a tool
argument. The MCP client or host must supply the `X-Dataverse-key` header when
using `request-header` mode. The client may attach that header to every MCP
request. DOME only forwards it to Dataverse for operations that require the
`DataverseApiKey` security scheme and removes it from public upstream calls.

For `request-header` and `env`, the server only fails locally when the matched
OpenAPI operation requires the `DataverseApiKey` security scheme and no key is
available. Public operations are called without `X-Dataverse-key`.

For upstream request diagnostics, pass `--log-dataverse-requests` or set
`MCP_LOG_DATAVERSE_REQUESTS=true`. DOME then writes one line to stderr for
each MCP-to-Dataverse request, including the HTTP method, path, and whether
authentication was used, for example `(auth=used)` or `(auth=not-used)`. The
log never contains the token or other credential values. Use
`--no-log-dataverse-requests` to override the environment setting.

Recommended deployment profiles:

Local stdio installation:

```env
MCP_TRANSPORT=stdio
API_KEY_MODE=env
DATAVERSE_API_TOKEN=your-token
```

Central multi-user HTTP service:

```env
MCP_TRANSPORT=streamable-http
API_KEY_MODE=request-header
# Leave DATAVERSE_API_TOKEN unset so users cannot fall back to a shared token.
```

`API_KEY_HEADER` and `API_KEY_ENV` are optional. Their defaults are
`X-Dataverse-key` and `DATAVERSE_API_TOKEN`.

If the Dataverse or OpenAPI endpoint uses an expired or self-signed certificate,
pass `--ignore-ssl-errors` or set `MCP_IGNORE_SSL_ERRORS=true`. This disables
certificate verification for both OpenAPI retrieval and upstream Dataverse
requests and should only be used on a trusted network. DOME prints a warning
when this mode is active; `--no-ignore-ssl-errors` restores secure verification.

## Tag and Tool Filtering

The server filters the OpenAPI document before it creates MCP tools. Tag filters
select functional groups, while tool filters select individual OpenAPI
`operationId` values. In the generated Dataverse specification, the
`operationId` is also the MCP tool name, for example
`DataRetrieverAPI_retrieveMyCollectionList`.

Expose only selected resource groups:

```bash
python dome.py \
  --openapi http://127.0.0.1:8080/openapi.json \
  --include-tag Datasets \
  --include-tag Files \
  --api-key-mode request-header \
  --transport streamable-http
```

Exclude administrative tools:

```bash
python dome.py \
  --openapi http://127.0.0.1:8080/openapi.json \
  --exclude-tag Admin \
  --api-key-mode request-header \
  --transport streamable-http
```

Environment variable equivalents:

```env
INCLUDE_TAGS=Datasets,Files
EXCLUDE_TAGS=Admin
```

To expose only two operations from the `Users` group, select the exact tool
names directly:

```bash
python dome.py \
  --include-tool DataRetrieverAPI_retrieveMyCollectionList \
  --include-tool DataRetrieverAPI_retrieveMyDataAsJsonString \
  --api-key-mode request-header \
  --transport streamable-http
```

The equivalent `.env` settings are:

```env
INCLUDE_TOOLS=DataRetrieverAPI_retrieveMyCollectionList,DataRetrieverAPI_retrieveMyDataAsJsonString
```

`INCLUDE_TAGS` and `INCLUDE_TOOLS` are independent positive selectors. If both
are set, DOME exposes the union: operations matching an included tag plus the
individually included operations. This lets you expose selected `Users`
operations alongside complete groups such as `Datasets` without exposing all
`Users` operations. `EXCLUDE_TAGS` and `EXCLUDE_TOOLS` are applied afterward;
any matching exclude tag or tool removes the operation.

For example, this exposes all `Datasets` operations plus the two selected
`Users` operations:

```env
INCLUDE_TAGS=Datasets
INCLUDE_TOOLS=DataRetrieverAPI_retrieveMyCollectionList,DataRetrieverAPI_retrieveMyDataAsJsonString
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
operation, the server exits with a clear error. `--include-tool` and
`--exclude-tool` can also be repeated; `INCLUDE_TOOLS` and `EXCLUDE_TOOLS` take
comma-separated lists.

## MCP Client Configuration

When `API_KEY_MODE=request-header`, configure the MCP client or host to send
the user's Dataverse API token as an HTTP header to DOME:

```http
X-Dataverse-key: <user Dataverse API token>
```

For a central service, replace the local URL in these examples with the deployed
HTTPS endpoint. The agent still calls tools normally; the MCP client supplies
the header separately, rather than exposing the token as a tool parameter. DOME
uses the incoming header only for authenticated upstream operations. For a
multi-user central service, leave `DATAVERSE_API_TOKEN` unset so a missing user
header cannot fall back to a shared server token. Keep tokens in each user's
local client configuration or secret store. Do not commit real Dataverse API
tokens.

### Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.dataverse]
url = "http://127.0.0.1:8000/mcp"
env_http_headers = { "X-Dataverse-key" = "DATAVERSE_API_TOKEN" }
# Optional: automatically approve all tools from this MCP server.
# default_tools_approval_mode = "approve"
```

Then set the token before starting Codex:

```bash
export DATAVERSE_API_TOKEN="your-token"
```

`default_tools_approval_mode = "approve"` is optional and suppresses Codex's
per-tool approval prompts for this server. It affects Codex's local approval
behavior only; it does not change which tools DOME exposes. Use DOME's
`INCLUDE_TOOLS` and `EXCLUDE_TOOLS` settings to restrict the tool surface.

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
