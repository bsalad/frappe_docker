"""Read-only client for the bexio REST API.

GET only: this module has no way to write to bexio, so the scripts cannot
change the account. The access token comes from the OAuth refresh token in the
keychain (oauth.refresh_access_token()); only where no refresh token exists
does the BEXIO_TOKEN environment variable (a personal access token, injected
by varlock) stand in. Which of the two was used is printed to stderr; the
token itself is never stored, printed or put in an error message.

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


def resolve_token():
    """(token, kind) for the API: the OAuth login first, the PAT only without one.

    kind is "oauth" or "pat". A refresh token that exists but is refused is an
    error, not a reason to fall back: a stale PAT would hide a login that needs
    renewing. A keychain that cannot be opened at all (no macOS Security
    framework) counts as no refresh token.
    """
    import oauth  # lazy: the Security framework is only loaded when a token is needed

    try:
        has_login = oauth.read_refresh_token() is not None
    except OSError:
        has_login = False
    except oauth.KeychainError as err:
        raise SystemExit("keychain: {}".format(err))
    if has_login:
        try:
            return oauth.refresh_access_token(), "oauth"
        except (oauth.OAuthError, oauth.KeychainError) as err:
            raise SystemExit("bexio login: {}".format(err))
    pat = os.environ.get("BEXIO_TOKEN")
    if pat:
        return pat, "pat"
    raise SystemExit(
        "no bexio refresh token in the keychain and BEXIO_TOKEN is not set; "
        "log in with `{}` (see finance/bexio/README.md)".format(oauth.LOGIN_COMMAND)
    )


class Client:
    def __init__(self, token=None, base_url=BASE_URL):
        if token is None:
            token, self.token_kind = resolve_token()
            print("bexio token: {}".format("OAuth refresh token" if self.token_kind == "oauth" else "personal access token (BEXIO_TOKEN)"), file=sys.stderr)
        else:
            self.token_kind = "given"
        self._token = token
        self._base_url = base_url.rstrip("/")
        self.requests = 0

    def get(self, path, params=None):
        """GET one resource; returns the decoded JSON body."""
        url = self._base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(MAX_RETRIES + 1):
            self.requests += 1
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            req.add_header("Authorization", "Bearer " + self._token)
            req.add_header("User-Agent", "yardr-finance-bexio-inventory/1.0")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
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
