#!/bin/sh
# Build the local finance image: ERPNext v16.50.0 (the version the stack runs)
# plus erpnextswiss, from finance/apps.json. Builds only; it does not touch the
# running stack. Switching the stack to this image is a later bead, after a backup.
#
# usage: finance/scripts/build-image.sh [tag]     (default tag: v16.50.0-swiss-bi3)
# result: frappe-finance-custom:<tag>
#
# Needs Docker with the buildx plugin (BuildKit secrets). apps.json goes in as a
# secret, not a build arg, so nothing from it ends up in image metadata.
set -eu

TAG="${1:-v16.50.0-swiss-bi3}"
FRAPPE_TAG=v16.50.0

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

APPS=finance/apps.json

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
        sys.exit(f"{app['url']} {app['branch']} is at {head or 'nothing'}, apps.json pins {pinned}")
    print(f"pinned: {app['url']} {app['branch']} @ {pinned}")
PY

# --no-cache: a secret is not part of the layer cache key, so a cached bench init
# layer would keep the apps from an earlier apps.json.
docker build \
  --no-cache \
  --build-arg FRAPPE_PATH=https://github.com/frappe/frappe \
  --build-arg FRAPPE_BRANCH="$FRAPPE_TAG" \
  --secret id=apps_json,src="$APPS" \
  --tag "frappe-finance-custom:$TAG-base" \
  --file images/custom/Containerfile .

# bi_finance is this repo's own app, not in apps.json: a layer on the base image.
docker build \
  --build-arg BASE="frappe-finance-custom:$TAG-base" \
  --tag "frappe-finance-custom:$TAG" \
  --file finance/images/bi_finance.Containerfile .

echo "built frappe-finance-custom:$TAG"
