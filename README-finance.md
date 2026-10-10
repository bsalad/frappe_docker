# Finance ERPNext stack

Operator guide for the finance yard's ERPNext stack. The upstream README.md
still describes the generic setup; this file covers only what this yard runs.

## What runs

- Compose project `frappe-finance`, built from two files:
  `pwd.yml` (the ERPNext stack) and `finance-local.yml` (the loopback-only
  port override).
- ERPNext / frappe `v16.50.0` with erpnextswiss, site `frontend` (the default
  site). The image is `frappe-finance-custom:v16.50.0-swiss`, set in
  `finance-local.yml`; build it with `finance/scripts/build-image.sh`. Swiss
  setup of the company: `finance/docs/erpnext-setup.md`.
- Services: `backend`, `frontend`, `websocket`, `scheduler`, `queue-short`,
  `queue-long`, `configurator`, `create-site`, `db` (MariaDB 11.8),
  `redis-cache` and `redis-queue`.

## Start, stop, status, logs

Always pass `-p frappe-finance`. Without it, compose names the project after
the directory it runs in, so you get a second, empty stack.

```sh
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml up -d
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml down
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml ps
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml logs -f <service>
```

`down` keeps the data volumes (`db-data`, `sites`, and so on). Adding `-v`
deletes them, so the site and its data are lost.

## Reaching it

- http://127.0.0.1:8080, bound to loopback only (`finance-local.yml` replaces
  the upstream port mapping with `127.0.0.1:8080:8080`).
- From the tailnet: https://bsaladins-mac-mini-1.tail61fee9.ts.net:8448 (tailnet
  only, not public). `tailscale serve` on the mini proxies it to 127.0.0.1:8080.
- To check or recreate the mapping on the mini, use the Homebrew binary:
  `/opt/homebrew/bin/tailscale serve status`. The `tailscale` wrapper in
  `~/.local/bin` points to a missing app, so do not use it.
- On the mini itself, use the loopback URL http://127.0.0.1:8080.
- Do not open the port on the LAN.

## Login

- User: `Administrator`.
- The password reference is `~/ws_yardr_finance/.erpnext-admin` (mode 600); read
  the current password from there. The `create-site` command in `pwd.yml` sets
  the initial password to `admin`. The file is authoritative only if it was
  updated to match the site (for example, after changing the password). Never
  copy a password into the repo, a commit, or a note.

## API access

Scripts and agents call the API as `api-agent@finance.local`, not as
`Administrator`. The user is a System User with no login password, and has the
roles Accounts User, Sales User, Purchase User and Stock User.

- Base URLs:
  - https://bsaladins-mac-mini-1.tail61fee9.ts.net:8448/api/ (tailnet)
  - http://127.0.0.1:8080/api/ (on the mini)
- Header: `Authorization: token <key>:<secret>`
- Credentials: `~/ws_yardr_finance/.erpnext-api` (mode 600, lines `url=`,
  `api_key=`, `api_secret=`). Read them from there; never copy them into the
  repo, a commit or a note.
- Rerun the provisioning script from the repo root. It is idempotent: a second
  run changes nothing.

  ```sh
  finance/scripts/create-api-user.sh            # create or repair the user
  finance/scripts/create-api-user.sh --rotate   # also issue a new key and secret
  ```

  The script also issues keys when the keys file is missing, because the
  secret cannot be read back from the site. Neither value is printed.

## Remotes

- `origin`: `github.com/svc-bi-concepts/frappe_docker` (our fork, base `main`).
- `upstream`: `github.com/frappe/frappe_docker`.
- To pull upstream changes:

  ```sh
  git fetch upstream && git merge upstream/main
  ```

## The gate

`.yardr/check` is the merge gate for this depot. It runs
`docker compose -f pwd.yml -f finance-local.yml config -q`, so the two compose
files must parse together. Upstream's pre-commit lint runs only if `pre-commit`
is installed.

Not covered here: backups and upgrades. Those come in later beads.
