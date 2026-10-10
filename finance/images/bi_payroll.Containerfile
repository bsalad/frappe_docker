# Adds the bi_payroll app (finance/apps/bi_payroll) on top of the image that already carries
# bi_finance and hrms. bi_payroll requires hrms, so it goes on every image that has hrms: the live
# image and the copy (finance/docs/hrms.md, Decision 2026-10-10). build-image.sh builds this layer.
ARG BASE=frappe-finance-custom:v16.50.0-swiss-hrms1
FROM ${BASE}

# The base image ends as frappe, so the app installs as the user bench uses.
COPY --chown=frappe:0 finance/apps/bi_payroll /home/frappe/frappe-bench/apps/bi_payroll
RUN /home/frappe/frappe-bench/env/bin/pip install --no-cache-dir -e /home/frappe/frappe-bench/apps/bi_payroll
