# Adds the bi_payroll app (finance/apps/bi_payroll) on top of the HRMS copy image, which already carries
# bi_finance and hrms. It is for the copy only: bi_payroll requires hrms, which the live site does not have,
# so it never goes on the finance image. Built by hand for the copy (finance/docs/hrms.md), not by build-image.sh.
ARG BASE=frappe-finance-custom:v16.50.0-swiss-hrms1
FROM ${BASE}

# The base image ends as frappe, so the app installs as the user bench uses.
COPY --chown=frappe:0 finance/apps/bi_payroll /home/frappe/frappe-bench/apps/bi_payroll
RUN /home/frappe/frappe-bench/env/bin/pip install --no-cache-dir -e /home/frappe/frappe-bench/apps/bi_payroll
