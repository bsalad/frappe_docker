# ERPNext MCP server

Claude Code reads and writes the finance ERPNext site through an MCP server.
This page records which server we evaluated, why we picked it, how it runs,
and the read test that proves it works.

## Pick

**`frappe-mcp-server` 1.2.0**, pinned in `finance/mcp/requirements.txt`, run
from `finance/mcp/venv` by `finance/mcp/run-frappe-mcp.sh`.

Why this one:

- MIT, one small package, two runtime dependencies (`mcp`, `httpx`).
- Talks only to the ERPNext REST API (`/api/resource`, `/api/method`) at the
  URL it is given. No bench or shell access, no telemetry, no other hosts.
- Authenticates with the API key and secret of `api-agent@finance.local`, the
  dedicated user from `finance/scripts/create-api-user.sh`.
- Generic CRUD and a method call, so it needs no code in the ERPNext site.

## Evaluation

Checked 2026-10-09. Versions and dates are from PyPI and GitHub.

| Candidate | Source, licence | Last release / activity | Works with v16 | Tools | Auth | Verdict |
|---|---|---|---|---|---|---|
| `frappe-mcp-server` 1.2.0 (PyPI) | github.com/muthanii/frappe_mcp, MIT | 1.2.0 on 2026-07-29; repo pushed 2026-07-29 | Yes: tested against v16.50.0 (see below) | ping, get_doc, search_docs, create_doc, update_doc, delete_doc, run_method | `Authorization: token key:secret` from env | **Picked** |
| `erpnext-agent` 1.0.1 (PyPI) | github.com/Knuckles-Team/erpnext-agent, MIT | 1.0.1 on 2026-07-02; repo pushed 2026-10-08 | Not tested | CRUD, call_method, get_logged_user, plus a generic `erpnext_agent_authentication` tool | token / Bearer / session via its own auth tool | Not picked, see below |
| `frappe/mcp` (github.com/frappe/mcp) | Frappe org, MIT, 169 stars | pushed 2026-05-29; marked "highly experimental" | Not tested | tools only | OAuth2 (needs frappe OAuth updates) | Not picked: it is a library that runs inside a Frappe app, so it changes the site |
| `ManotLuijiu/erpnext_mcp_server` | github.com/ManotLuijiu/erpnext_mcp_server, MIT, 4 stars | pushed 2026-10-05 | Not tested | file ops, read-only DB, API | ERPNext API | Not picked: a Frappe app inside the site (brief rule); not code-reviewed |
| `sajmustafake/frappe-dev-mcp-server` | github.com/SajmustafaKe/frappe-dev-mcp-server, MIT, 4 stars | pushed 2026-06-11 | Not tested | DocType creation, bench commands, app management | none needed | Out: development tool with bench access (brief rule); not code-reviewed |
| `superWorldSavior/mcp-erpnext` | github.com/superWorldSavior/mcp-erpnext, MIT, 103 stars | pushed 2026-10-05 | Not tested | not reviewed | not reviewed | Not evaluated: found by search, not on the brief's list |

### Why not `erpnext-agent`

It is a sound project, but it carries more risk than the job needs:

- It depends on `agent-utilities`, a separate package with its own dependency
  tree. We did not review that tree.
- Its `erpnext_agent_authentication` tool takes an `action` string from the
  caller and calls `getattr(client, action)` on the API client with
  caller-supplied keyword arguments. Any method of the client can be reached
  that way, and `login` takes credentials as tool arguments.
- The client calls `urllib3.disable_warnings(InsecureRequestWarning)`, which
  hides TLS warnings.

### Why `frappe/mcp` is out

The brief says there is no MCP server from the Frappe team. That is not quite
right: `github.com/frappe/mcp` exists (MIT, last push 2026-05-29). It is not a
standalone server, though. Its README says a Frappe app serves MCP itself
(`@mcp.register()` in `app/mcp.py`), so using it means installing an app into
the finance site. The brief rules that out, so we did not install it.

## What we read

We read the code of `frappe-mcp-server` 1.2.0 from its PyPI wheel and sdist:
`frappe_mcp/client.py`, `frappe_mcp/server.py`, and the metadata. We did not
diff it against the GitHub tag, and we did not run its own test suite.

- `client.py`: every URL segment is percent-encoded (`_seg`), so a tool argument
  cannot escape its endpoint. The only host it contacts is `FRAPPE_URL`.
- `server.py`: reads `FRAPPE_URL`, `FRAPPE_API_KEY`, `FRAPPE_API_SECRET`, and
  optionally `FRAPPE_VERIFY_SSL` and `FRAPPE_TIMEOUT`. Logs go to stderr, so
  stdio stays clean. It exposes seven tools (listed in the test below).
- `frappe_run_method` calls `/api/method/<dotted path>`. What it can reach is
  limited by the API user's roles, which are Accounts User, Sales User,
  Purchase User and Stock User, not System Manager.

## How it runs

- `finance/mcp/requirements.txt` pins `frappe-mcp-server==1.2.0`.
- `finance/mcp/venv` is created with `uv venv` and filled with
  `uv pip install -r finance/mcp/requirements.txt`. It is git-ignored (the
  `venv` pattern in `.gitignore`).
- `finance/mcp/run-frappe-mcp.sh` reads `url=`, `api_key=` and `api_secret=`
  from `~/ws_yardr_finance/.erpnext-api` at each start. It strips the `/api/`
  suffix from the URL, exports the three variables, and execs the server. The
  secret never goes into the repo, into argv, or into the output.
- `finance/mcp/mcp.example.json` is the Claude Code config. Copy its `erpnext`
  entry into your own config and set the absolute path to the wrapper.

## Read test

Read-only: nothing was created, updated or deleted in ERPNext. The inline
client below starts the wrapper over stdio and calls the server.

Repeatable command (requires the pinned venv and the private token file):

```sh
finance/mcp/venv/bin/python - <<'PY'
import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command='finance/mcp/run-frappe-mcp.sh', args=[])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print('tools:', [tool.name for tool in tools.tools])
            calls = [
                ('frappe_ping', {}),
                ('frappe_search_docs', {'doctype': 'Customer', 'fields': ['name'], 'limit': 1}),
                ('frappe_search_docs', {'doctype': 'Item', 'fields': ['name', 'item_name'], 'limit': 1}),
            ]
            for name, args in calls:
                result = await session.call_tool(name, args)
                print(f'### {name} {json.dumps(args)}')
                for block in result.content:
                    print(block.text)
            result = await session.call_tool('frappe_get_doc', {'doctype': 'Item', 'name': 'SKU008'})
            print('### frappe_get_doc {"doctype": "Item", "name": "SKU008"}')
            print(result.content[0].text[:700] + ' ...')
            result = await session.call_tool('frappe_run_method', {'method': 'frappe.auth.get_logged_user'})
            print('### frappe_run_method {"method": "frappe.auth.get_logged_user"}')
            print(result.content[0].text)

asyncio.run(main())
PY
```

Output, trimmed (the Item record is cut to its first fields):

```text
tools: ['frappe_ping', 'frappe_get_doc', 'frappe_search_docs', 'frappe_create_doc', 'frappe_update_doc', 'frappe_delete_doc', 'frappe_run_method']

### frappe_ping {}
{
  "message": "pong"
}

### frappe_search_docs {"doctype": "Customer", "fields": ["name"], "limit": 1}
{
  "data": [
    {
      "name": "Grant Plastics Ltd."
    }
  ]
}

### frappe_search_docs {"doctype": "Item", "fields": ["name", "item_name"], "limit": 1}
{
  "data": [
    {
      "name": "SKU008",
      "item_name": "Backpack"
    }
  ]
}

### frappe_get_doc {"doctype": "Item", "name": "SKU008"}
{
  "data": {
    "name": "SKU008",
    "owner": "Administrator",
    "item_code": "SKU008",
    "item_name": "Backpack",
    "item_group": "Demo Item Group",
    "stock_uom": "Nos",
    ...
  }
}

### frappe_run_method {"method": "frappe.auth.get_logged_user"}
{
  "message": "api-agent@finance.local"
}
```

Results:

- Customer: 1 listed (`Grant Plastics Ltd.`).
- Item: 1 read (`SKU008`, Backpack). It was created by `Administrator` on
  2026-10-08, before this bead; we did not create it.
- Identity: the server calls the site as `api-agent@finance.local`, not
  `Administrator`.

The server's `frappe_run_method` and `frappe_get_doc` reach the same data the
API user may see; the write tools were not exercised.

## Not covered

- Write tools (`create_doc`, `update_doc`, `delete_doc`, `run_method` with
  side effects) are not tested against real data. The brief leaves that out.
- Registering the server in the global Claude config: the yardmaster does
  that after merge.
- Upgrading the pin: change `requirements.txt`, re-run the install, and re-run
  the read test.
