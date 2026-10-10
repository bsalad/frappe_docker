#!/bin/sh
# Switch the finance stack to a built image, then clean up: after the stack runs the new
# image and ERPNext answers, the image it replaced and the new image's -base layer are
# removed, so one frappe-finance-custom image is at rest (finance/docs/erpnext-setup.md).
# A deploy bead runs this, after a backup and with no other session writing to ERPNext.
#
# usage: finance/scripts/switch-image.sh <new-tag>
#   The image frappe-finance-custom:<new-tag> must exist (build-image.sh builds it).
#   The previous tag is read from finance-local.yml. If ping does not answer 200, the
#   compose file goes back to the previous tag, the stack is brought up on it, the old
#   image is kept and the new one is left in place: a person decides what to remove.
set -eu

usage() {
  echo "usage: finance/scripts/switch-image.sh <new-tag>"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac
if [ $# -ne 1 ]; then
  usage >&2
  exit 2
fi
NEW="$1"

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

# Pings tried after the restart, with a wait between them. Overridable for the tests.
PING_TRIES="${SWITCH_PING_TRIES:-30}"
PING_WAIT="${SWITCH_PING_WAIT:-10}"
PING_URL="http://127.0.0.1:8080/api/method/ping"

OLD=$(python3 finance/scripts/switch_image.py current finance-local.yml)
if [ "$OLD" = "$NEW" ]; then
  echo "refused: the stack already runs frappe-finance-custom:$NEW" >&2
  exit 1
fi
if ! docker image inspect "frappe-finance-custom:$NEW" >/dev/null 2>&1; then
  echo "refused: frappe-finance-custom:$NEW is not built; run build-image.sh first" >&2
  exit 1
fi

# Bring the stack up on the new tag, then restart the two services that keep the old
# backend IP (finance/docs/erpnext-setup.md, "Switching the stack to a new image").
python3 finance/scripts/switch_image.py set finance-local.yml "$NEW"
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml up -d
docker compose -p frappe-finance restart frontend websocket

ping_ok() {
  i=0
  while [ "$i" -lt "$PING_TRIES" ]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$PING_URL" || true)
    if [ "$code" = "200" ]; then
      return 0
    fi
    i=$((i + 1))
    sleep "$PING_WAIT"
  done
  return 1
}

if ping_ok; then
  echo "switched: frappe-finance-custom:$OLD -> $NEW (ping 200)"
  for ref in $(python3 finance/scripts/switch_image.py remove "$OLD" "$NEW"); do
    if docker image inspect "$ref" >/dev/null 2>&1; then
      docker rmi "$ref"
    fi
  done
  echo "one image at rest: frappe-finance-custom:$NEW"
  exit 0
fi

echo "ping did not answer 200: rolling finance-local.yml back to $OLD" >&2
python3 finance/scripts/switch_image.py set finance-local.yml "$OLD"
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml up -d
docker compose -p frappe-finance restart frontend websocket
echo "rolled back: the stack is up on frappe-finance-custom:$OLD (kept); the script did not ping it again." >&2
echo "frappe-finance-custom:$NEW is left in place; remove it by hand once the stack answers." >&2
exit 1
