# bexio login

The bexio scripts log in through Benchi's bexio OAuth app, with read-only scopes.
bexio enforces the scopes, so the token cannot change the account, whatever the code does.

## Log in once

On the Mac mini's desktop Terminal:

```sh
varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/oauth.py login
```

The command prints the bexio authorize URL and opens it in the browser. Approve the
login there. The browser then returns to `http://localhost:8765/callback`, the local
listener on 127.0.0.1:8765 takes the code, and the refresh token is saved in the
macOS keychain (service `varlock`, account `finance:local:BEXIO_REFRESH_TOKEN`).
The token itself is never printed.

## What the scopes allow

Every scope is read-only: `openid offline_access` for the login and the refresh,
and `*_show` for contacts, notes, articles, invoices, offers, orders, deliveries,
bills, expenses, bank accounts and bank payments. There is no
`accounting` scope and no `*_edit` scope. The list is the `SCOPE` constant in
`oauth.py`; a later import adds `accounting` there, behind its own consent.

## How the scripts use it

Each run refreshes the access token from the keychain. bexio rotates the refresh token
on every refresh, so the new one is saved back into the same keychain item. The access
token is kept in memory only.

## Log in again

If the refresh token expires or is revoked, the scripts stop with
an error that names bexio's answer (for example `invalid_grant`) and says to run
`... oauth.py login` again. Run the login
command above again. A new login replaces the stored token.

Revoking the old personal access token (`BEXIO_TOKEN`) is a separate step, done in
bexio once the scripts run on this login.

## Master-data pipeline (export, then import)

1. Export, read-only, to private JSON files (needs the bexio login, so under varlock):

       varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/export.py

2. One-off per site, as Administrator in the backend container (the API user may not
   write these): `finance/scripts/swiss-setup.sh fields currencies`, and the banks
   that the Bank Accounts need:

       BANKS="$(python3 finance/bexio/import_master.py --print-banks)" finance/scripts/swiss-setup.sh banks

3. Import into ERPNext as `api-agent@finance.local` (token file `~/ws_yardr_finance/.erpnext-api`):

       python3 finance/bexio/import_master.py --dry-run   # totals only
       python3 finance/bexio/import_master.py

Each record is keyed by `bexio_id`; a second run changes nothing. Take a backup
first (`bench --site frontend backup`). The export directory is company data and
stays under `~/ws_yardr_finance/private/`, never in the repository.
