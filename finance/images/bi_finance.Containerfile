# Adds the bi_finance app (finance/apps/bi_finance) on top of the finance image.
# apps.json cannot carry it: bench clones each app from a git URL, and this app
# lives in this repo only. build-image.sh builds this layer after the main image.
ARG BASE=frappe-finance-custom:v16.50.0-swiss-bi3-base
FROM ${BASE}

# The base image ends as frappe, so the app installs as the user bench uses.
COPY --chown=frappe:0 finance/apps/bi_finance /home/frappe/frappe-bench/apps/bi_finance
RUN /home/frappe/frappe-bench/env/bin/pip install --no-cache-dir -e /home/frappe/frappe-bench/apps/bi_finance
