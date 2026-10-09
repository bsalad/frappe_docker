#!/bin/sh
# Inserts the bexio drafts that the importers hand over into ERPNext site 'frontend': the plan JSON of
# import_sales.py and import_purchase.py with --apply. The loader is bexio-drafts.py; it runs inside the
# backend container with bench's Python, the way swiss-setup.sh runs swiss-setup.py. Its stdin is the plan,
# so the loader's source goes in as an environment variable.
#
#   finance/scripts/bexio-drafts.sh <plan.json>
set -eu

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

plan="${1:?usage: bexio-drafts.sh <plan.json>}"
LOADER=$(cat finance/scripts/bexio-drafts.py)
export LOADER

docker compose -p frappe-finance -f pwd.yml -f finance-local.yml exec -T -e LOADER backend \
  sh -c 'cd /home/frappe/frappe-bench/sites && exec ../env/bin/python -c "$LOADER"' \
  < "$plan"
