#!/bin/sh
# Swiss setup of company BI Concepts on site 'frontend': KMU chart of
# accounts, VAT templates, fiscal years and the bexio_id fields. Steps run in
# that order and are re-runnable. Needs the swiss image with erpnextswiss
# installed (finance/docs/erpnext-setup.md).
#
#   finance/scripts/swiss-setup.sh                # all steps
#   finance/scripts/swiss-setup.sh vat fiscal     # some of: coa vat fiscal fields
set -eu

# Compose files live at the repo root, two levels up from this script.
cd "$(dirname "$0")/../.."

# bench's own Python loads the site; the script goes in on stdin so the
# container needs no copy of it.
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml exec -T backend \
  sh -c 'cd /home/frappe/frappe-bench/sites && exec ../env/bin/python - "$@"' sh "$@" \
  < finance/scripts/swiss-setup.py
