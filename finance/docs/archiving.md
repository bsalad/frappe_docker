# Archiving: backups and 10-year retention

The finance site (`frontend`, company BI Concepts) must keep its business
records and their attachments for 10 years (GeBüV; OR 958f). This page says
what is kept today, where, and for how long, and what is missing. The setup
is in `erpnext-setup.md`; the rules ERPNext enforces on posted records are the
`gebuev` step there.

## What is kept today

- **Where.** Docker runs in a colima VM on the Mac, so the backups are not on
  the Mac's disk. They are in the `frappe-finance_sites` volume, at
  `sites/frontend/private/backups`. Inside the VM that is
  `/var/lib/docker/volumes/frappe-finance_sites/_data/frontend/private/backups`.
- **What a backup is.** One `bench backup` writes a set of files with one
  timestamp prefix: `*-database.sql.gz` (all records, including the audit
  trail in `tabVersion`), `*-site_config_backup.json` (database credentials
  and the encryption key), and, with `--with-files`, `*-files.tar` (public
  attachments) and `*-private-files.tar` (private attachments).
- **How it is made.** By hand:
  `docker compose -p frappe-finance exec -T backend bench --site frontend backup --with-files`.
  Always pass `--with-files`; a set without it has no attachments.
- **Automatic backups: none.** The stack does not take backups on a schedule.
- **How long.** The scheduler runs an hourly job,
  `frappe.desk.page.backups.backups.delete_downloadable_backups`. It keeps the
  newest `backup_limit` sets and deletes the older ones. `backup_limit` is 3
  (System Settings). So at most three sets survive, however old they are.
- **The database itself** (volume `frappe-finance_db-data`) is the live copy,
  not a backup.
- **The software.** A restore needs the same ERPNext version as the backup:
  `frappe-finance-custom:v16.50.0-swiss`, built by `finance/scripts/build-image.sh`
  from `finance/apps.json`. The build is local; nothing in the repo pushes it
  to a registry.

## What is missing for 10 years

1. **Count and age.** Three sets are kept, and the hourly job deletes the
   rest. Ten years need a retention rule by age, for example daily sets for a
   year and year-end sets for ten.
2. **One place.** Every copy is in one VM on one Mac. A lost or reset colima
   VM takes all backups with it. There is no copy off the machine.
3. **Attachments in every set.** A set made without `--with-files` cannot
   restore attachments. Nothing enforces the flag yet.
4. **The software to restore with.** The image is local only. Keep either the
   image in a registry or the build inputs (`apps.json`, `build-image.sh`, the
   version pins), which are in git, with a way to rebuild the same image.
5. **Protection of the copies.** A set contains the site's encryption key
   (`site_config_backup.json`). Any copy outside this machine must be
   encrypted and access-controlled.
6. **A tested restore.** No restore of a backup has been done. Until one has
   been restored into a scratch site and checked (record counts, a few
   invoices with their attachments), the backups are unverified.

## Options (not set up; Benchi decides)

- **A. Daily copy off the machine.** A job after each backup copies the new
  set to a place outside this Mac, such as an S3-compatible bucket with object
  lock, or a NAS or external disk. Needs: the place, who pays for it, and where
  the encryption key for the copies is kept. Covers 1, 2, 5.
- **B. Larger `backup_limit`.** Keeps more sets on the same VM disk. Simple,
  but it adds no copy off the machine, and disk use grows with every set. Covers
  1 only, and only partly.
- **C. Year-end archive.** At each fiscal year end, keep one full set with
  files for ten years after that year end, in the place from A. Small, and it
  matches the retention period. Covers 1 for the long term.
- **D. Readable export.** Yearly exports of the general ledger and of the
  invoices and their attachments, as CSV or PDF, as a copy that does not need
  ERPNext to read. Covers 4 and makes the records readable without the software.

Suggestion: A with C, so that daily sets go off the machine and the year-end
sets are kept for ten years. D later, once the chart and the attachments are
settled.

## Checks

```sh
# the sets on the volume, newest last
docker compose -p frappe-finance exec -T backend sh -c \
  'ls -l /home/frappe/frappe-bench/sites/frontend/private/backups'
# the limit the hourly job applies
docker compose -p frappe-finance exec -T backend sh -c \
  'cd /home/frappe/frappe-bench/sites && ../env/bin/python -c "import frappe; frappe.init(site=\"frontend\"); frappe.connect(); print(frappe.db.get_single_value(\"System Settings\", \"backup_limit\"))"'
```
