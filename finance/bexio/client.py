"""Read-only client for the bexio REST API.

GET only: this module has no way to write to bexio, so the scripts cannot
change the account. The access token comes from the OAuth refresh token in the
keychain (oauth.refresh_access_token()); only where no refresh token exists
does the BEXIO_TOKEN environment variable (a personal access token, injected
by varlock) stand in. The export login (export_scope=True, for the few entities
of export.py that need the accounting and file scopes) reads its own keychain
item and never falls back to BEXIO_TOKEN. Which of these was used is printed to
stderr; the token itself is never stored, printed or put in an error message.

Run the scripts that use it as:
    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/inventory.py

Standard library only, so it runs on the system Python.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.bexio.com"

# bexio caps a page at 2000 records; a smaller page costs more requests.
PAGE_SIZE = 500

# Retries for 429 (rate limit) and 5xx answers before giving up.
MAX_RETRIES = 5


class BexioError(Exception):
    """A bexio answer that is not a usable 2xx response."""

    def __init__(self, status, path):
        super().__init__("bexio GET {} failed with HTTP {}".format(path, status))
        self.status = status
        self.path = path


TOKEN_NAMES = {
    "oauth": "OAuth refresh token",
    "oauth-export": "OAuth export refresh token (accounting and file scopes)",
    "pat": "personal access token (BEXIO_TOKEN)",
}


def resolve_token(export_scope=False):
    """(token, kind) for the API: the OAuth login first, the PAT only without one.

    kind is "oauth", "oauth-export" or "pat". A refresh token that exists but is
    refused is an error, not a reason to fall back: a stale PAT would hide a login
    that needs renewing. The export login (export_scope) has no fallback at all:
    the PAT cannot stand in for the accounting and file scopes. A keychain that
    cannot be opened at all (no macOS Security framework) counts as no refresh token.
    """
    import oauth  # lazy: the Security framework is only loaded when a token is needed

    keychain_account = oauth.EXPORT_KEYCHAIN_ACCOUNT if export_scope else oauth.KEYCHAIN_ACCOUNT
    kind = "oauth-export" if export_scope else "oauth"
    try:
        has_login = oauth.read_refresh_token(keychain_account) is not None
    except OSError:
        has_login = False
    except oauth.KeychainError as err:
        raise SystemExit("keychain: {}".format(err))
    if has_login:
        try:
            return oauth.refresh_access_token(export_scope=export_scope), kind
        except (oauth.OAuthError, oauth.KeychainError) as err:
            raise SystemExit("bexio login: {}".format(err))
    if export_scope:
        raise SystemExit(
            "no bexio export login in the keychain; log in with `{}` (see finance/bexio/README.md)".format(oauth.EXPORT_LOGIN_COMMAND)
        )
    pat = os.environ.get("BEXIO_TOKEN")
    if pat:
        return pat, "pat"
    raise SystemExit(
        "no bexio refresh token in the keychain and BEXIO_TOKEN is not set; "
        "log in with `{}` (see finance/bexio/README.md)".format(oauth.LOGIN_COMMAND)
    )


class Client:
    def __init__(self, token=None, base_url=BASE_URL, export_scope=False):
        if token is None:
            token, self.token_kind = resolve_token(export_scope)
            print("bexio token: {}".format(TOKEN_NAMES[self.token_kind]), file=sys.stderr)
        else:
            self.token_kind = "given"
        self._token = token
        self._base_url = base_url.rstrip("/")
        self.requests = 0

    def get(self, path, params=None, raw=False):
        """GET one resource; returns the decoded JSON body, or the bytes when raw (a file's content)."""
        url = self._base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(MAX_RETRIES + 1):
            self.requests += 1
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "*/*" if raw else "application/json")
            req.add_header("Authorization", "Bearer " + self._token)
            req.add_header("User-Agent", "yardr-finance-bexio-inventory/1.0")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read()
                return body if raw else json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as err:
                retryable = err.code == 429 or 500 <= err.code < 600
                if not retryable or attempt == MAX_RETRIES:
                    raise BexioError(err.code, path) from None
                time.sleep(_wait_seconds(err, attempt))
            except urllib.error.URLError:
                if attempt == MAX_RETRIES:
                    raise BexioError("network", path) from None
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def paginate(self, path, params=None, page_size=PAGE_SIZE):
        """Yield every record of a list endpoint, page by page (limit and offset).

        bexio may cap a page below the size asked for, so the offset advances
        by the rows actually received and the walk ends on an empty page. A
        short page is not taken as the last one. Some endpoints ignore limit
        and offset and answer every page with the same rows (/3.0/taxes and
        /3.0/accounting/business_years do); the walk therefore ends too when a
        page brings no id not seen before, and a row is yielded once per id.
        """
        seen = set()
        offset = 0
        while True:
            page_params = dict(params or {})
            page_params["limit"] = page_size
            page_params["offset"] = offset
            page = self.get(path, page_params)
            if not isinstance(page, list):
                raise BexioError("unexpected body", path)
            if not page:
                return
            fresh = _unseen(page, seen)
            if not fresh:
                return
            yield from fresh
            offset += len(page)

    def paginate_pages(self, path, size_param, rows_key=None, params=None, page_size=PAGE_SIZE):
        """Yield every record of a page-numbered endpoint (the API 4.0 lists).

        These take `page` and a size parameter whose name differs by endpoint
        (`page_size` for purchase bills, `per-page` for banking payments), and
        wrap the records in an object: under `rows_key` when given. The walk
        ends on an empty page or one without a new id, as in paginate().
        """
        seen = set()
        page = 1
        while True:
            page_params = dict(params or {})
            page_params["page"] = page
            page_params[size_param] = page_size
            body = self.get(path, page_params)
            rows = body.get(rows_key) if rows_key else body
            if not isinstance(rows, list):
                raise BexioError("unexpected body", path)
            if not rows:
                return
            fresh = _unseen(rows, seen)
            if not fresh:
                return
            yield from fresh
            page += 1


def _unseen(rows, seen):
    """The rows whose id is new, remembering them in `seen`.

    A row without an id (not a dict, or no "id" key) always counts as new, so
    lists of plain values or id-less records page as before.
    """
    fresh = []
    for row in rows:
        key = row.get("id") if isinstance(row, dict) else None
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        fresh.append(row)
    return fresh


def _wait_seconds(err, attempt):
    # bexio sends Retry-After on 429; fall back to exponential backoff.
    retry_after = err.headers.get("Retry-After") if err.headers else None
    try:
        return max(1.0, float(retry_after))
    except (TypeError, ValueError):
        return float(2 ** attempt)
