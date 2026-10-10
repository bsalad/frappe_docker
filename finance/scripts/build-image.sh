#!/bin/sh
# Build a finance image from one of two app lists, as a base layer (ERPNext and the
# apps) plus the bi_finance layer and the bi_payroll layer. Builds only; it does not
# touch the running stack. Switching a stack to the image is a later step, after a backup.
#
# usage: finance/scripts/build-image.sh live|copy <tag>
#   live  finance/apps.json: ERPNext, erpnextswiss and hrms (payroll runs on the live
#         site, finance/docs/hrms.md, Decision 2026-10-10).
#   copy  finance/apps-copy.json: the same list; kept as the HRMS copy site's own list
#         (finance/docs/hrms.md).
# result: frappe-finance-custom:<tag>, refused if that tag (or its -base or -finance
#         layer) already exists, so a build can never overwrite a tag in use.
#
# Needs Docker with the buildx plugin (BuildKit secrets). The app list goes in as a
# secret, not a build arg, so nothing from it ends up in image metadata.
set -eu

usage() {
  echo "usage: finance/scripts/build-image.sh live|copy <tag>"
  echo "  live  finance/apps.json (with hrms)"
  echo "  copy  finance/apps-copy.json (with hrms)"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  live) APPS=finance/apps.json ;;
  copy) APPS=finance/apps-copy.json ;;
  *) usage >&2; exit 2 ;;
esac
if [ $# -ne 2 ]; then
  usage >&2
  exit 2
fi
MODE="$1"
TAG="$2"
FRAPPE_TAG=v16.50.0

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

# The copy image is built from a list with hrms; a list without it is a mistake.
python3 - "$APPS" "$MODE" <<'PY'
import json, sys

apps_path, mode = sys.argv[1], sys.argv[2]
names = [app["url"].rstrip("/").rsplit("/", 1)[-1] for app in json.load(open(apps_path))]
if mode == "copy" and "hrms" not in names:
    sys.exit(f"refused: {apps_path} has no hrms; the copy image is built from it")
PY

# Refuse to overwrite a tag that exists: a live tag stays what it was built as.
if docker image inspect "frappe-finance-custom:$TAG" >/dev/null 2>&1 \
  || docker image inspect "frappe-finance-custom:$TAG-base" >/dev/null 2>&1 \
  || docker image inspect "frappe-finance-custom:$TAG-finance" >/dev/null 2>&1; then
  echo "refused: frappe-finance-custom:$TAG (or its -base or -finance) already exists; pick a new tag" >&2
  exit 1
fi

# bench clones each app with `git clone --branch`, which takes a branch or tag,
# not a commit. So apps.json pins erpnextswiss to a branch and records the
# commit in "commit". Refuse to build if the branch has moved off that commit.
# The check runs before the build, so a move after it is not caught.
python3 - "$APPS" <<'PY'
import json, subprocess, sys

for app in json.load(open(sys.argv[1])):
    pinned = app.get("commit")
    if not pinned:
        continue
    out = subprocess.run(
        ["git", "ls-remote", app["url"], "refs/heads/" + app["branch"]],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    head = out[0] if out else ""
    if head != pinned:
        sys.exit(f"{app['url']} {app['branch']} is at {head or 'nothing'}, the list pins {pinned}")
    print(f"pinned: {app['url']} {app['branch']} @ {pinned}")
PY

# --no-cache: a secret is not part of the layer cache key, so a cached bench init
# layer would keep the apps from an earlier apps list.
docker build \
  --no-cache \
  --build-arg FRAPPE_PATH=https://github.com/frappe/frappe \
  --build-arg FRAPPE_BRANCH="$FRAPPE_TAG" \
  --secret id=apps_json,src="$APPS" \
  --tag "frappe-finance-custom:$TAG-base" \
  --file images/custom/Containerfile .

# bi_finance and bi_payroll are this repo's own apps, not in the app lists: a layer each
# on the base image. bi_payroll requires hrms, which the base carries.
docker build \
  --build-arg BASE="frappe-finance-custom:$TAG-base" \
  --tag "frappe-finance-custom:$TAG-finance" \
  --file finance/images/bi_finance.Containerfile .

docker build \
  --build-arg BASE="frappe-finance-custom:$TAG-finance" \
  --tag "frappe-finance-custom:$TAG" \
  --file finance/images/bi_payroll.Containerfile .

echo "built frappe-finance-custom:$TAG from $APPS"
