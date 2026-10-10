#!/bin/sh
# Swiss setup of company BI Concepts on site 'frontend': KMU chart of
# accounts, VAT templates, fiscal years and the bexio_id fields. Steps run in
# that order and are re-runnable. Needs the swiss image with erpnextswiss
# installed (finance/docs/erpnext-setup.md).
#
#   finance/scripts/swiss-setup.sh                # all steps
#   finance/scripts/swiss-setup.sh vat fiscal     # some of: coa vat fiscal fields currencies banks gebuev qrbill host payments
#   finance/scripts/swiss-setup.sh vat --check <export dir>   # VAT codes against the export's taxes.json
#   finance/scripts/swiss-setup.sh freeze 2025-12-31          # dry run: what a freeze would set
#   finance/scripts/swiss-setup.sh freeze 2025-12-31 --apply  # sets accounts frozen till that date
#   finance/scripts/swiss-setup.sh treasury                   # dry run: the Treasury workspace
#   finance/scripts/swiss-setup.sh treasury --apply           # creates or updates it
#
# host sets the site's host_name from HOST_NAME (the URL the backend can reach, e.g.
# https://<machine>.<tailnet>.ts.net:8448); it is skipped when HOST_NAME is empty.
set -eu

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

# The container cannot see the export directory: its taxes.json and
# accounts.json go in as environment variables.
TAXES_JSON="" ACCOUNTS_JSON=""
if [ "${1:-}" = vat ] && [ "${2:-}" = --check ]; then
  dir="${3:?usage: swiss-setup.sh vat --check <export dir>}"
  TAXES_JSON=$(cat "$dir/taxes.json")
  ACCOUNTS_JSON=$(cat "$dir/accounts.json")
fi
export TAXES_JSON ACCOUNTS_JSON

# bench's own Python loads the site; the script goes in on stdin so the
# container needs no copy of it.
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml exec -T \
  -e BANKS="${BANKS:-}" -e HOST_NAME="${HOST_NAME:-}" -e TAXES_JSON -e ACCOUNTS_JSON backend \
  sh -c 'cd /home/frappe/frappe-bench/sites && exec ../env/bin/python - "$@"' sh "$@" \
  < finance/scripts/swiss-setup.py
