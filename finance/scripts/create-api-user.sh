#!/bin/sh
# Provision the ERPNext user that scripts and agents call the API as, so
# nothing uses Administrator. Re-runnable: a second run changes nothing, and
# keys are rotated only with --rotate (or when the keys file is missing,
# because the secret cannot be read back from the site).
#
# Writes url=, api_key= and api_secret= to ~/ws_yardr_finance/.erpnext-api
# (mode 600). Neither key nor secret is printed.
set -eu

ROTATE=0
case "${1:-}" in
  --rotate) ROTATE=1 ;;
  "") ;;
  *) echo "usage: $0 [--rotate]" >&2; exit 2 ;;
esac

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

PROJECT=frappe-finance
API_URL=http://127.0.0.1:8080/api/
OUT="$HOME/ws_yardr_finance/.erpnext-api"

FORCE=0
if [ "$ROTATE" = 1 ] || [ ! -f "$OUT" ]; then
  FORCE=1
fi

# Runs inside the backend container with bench's own Python, so frappe loads
# the site without an interactive console. Prints one marked JSON line; the
# keys appear in it only when they were generated.
CONTAINER_PY=$(cat <<'PY'
import json, sys
import frappe
from frappe.core.doctype.user.user import generate_keys

EMAIL = "api-agent@finance.local"
ROLES = ["Accounts User", "Sales User", "Purchase User", "Stock User"]
force = sys.argv[1] == "1"

frappe.init(site="frontend", sites_path=".")
frappe.connect()
frappe.set_user("Administrator")

result = {"created": False, "roles_added": [], "keys": None}
if not frappe.db.exists("User", EMAIL):
    user = frappe.get_doc({
        "doctype": "User",
        "email": EMAIL,
        "first_name": "API Agent",
        "user_type": "System User",
        "enabled": 1,
        "send_welcome_email": 0,
        "roles": [{"role": role} for role in ROLES],
    })
    user.insert(ignore_permissions=True)
    result["created"] = True
    result["roles_added"] = ROLES
else:
    user = frappe.get_doc("User", EMAIL)
    have = {row.role for row in user.roles}
    missing = [role for role in ROLES if role not in have]
    for role in missing:
        user.append("roles", {"role": role})
    if missing:
        user.save(ignore_permissions=True)
        result["roles_added"] = missing

if force or not user.api_key:
    result["keys"] = generate_keys(EMAIL)
frappe.db.commit()
print("FINANCE_API_USER " + json.dumps(result))
PY
)

RESULT=$(printf '%s\n' "$CONTAINER_PY" | docker compose -p "$PROJECT" -f pwd.yml -f finance-local.yml \
  exec -T -w /home/frappe/frappe-bench/sites backend \
  /home/frappe/frappe-bench/env/bin/python -I - "$FORCE") || {
  echo "create-api-user: the backend container did not run the provisioning step" >&2
  exit 1
}

# Host side: write the keys file with mode 600 from a temp file and rename it
# into place, so a failed run never leaves a half-written file behind.
HOST_PY=$(cat <<'PY'
import json, os, sys
out, api_url = sys.argv[1], sys.argv[2]
line = next((l for l in sys.stdin if l.startswith("FINANCE_API_USER ")), None)
if line is None:
    sys.exit("create-api-user: no result line from the site")
res = json.loads(line[len("FINANCE_API_USER "):])
if res["created"]:
    print("created user api-agent@finance.local (no login password, System User)")
if res["roles_added"]:
    print("roles set: " + ", ".join(res["roles_added"]))
keys = res["keys"]
if keys:
    tmp = out + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("url=%s\napi_key=%s\napi_secret=%s\n" % (api_url, keys["api_key"], keys["api_secret"]))
    os.replace(tmp, out)
    print("wrote " + out + " (mode 600); key and secret not shown")
else:
    print("keys unchanged (" + out + ")")
PY
)
printf '%s\n' "$RESULT" | python3 -c "$HOST_PY" "$OUT" "$API_URL"
