"""OAuth login for the bexio API: one login by hand, then refresh tokens from the keychain.

There are two logins, each with its own keychain item and its own scopes:

- the read-only login (SCOPE below), which every script uses. bexio enforces
  the scopes, so this token cannot change the account, whatever the code does.
- the export login (EXPORT_SCOPE below), which only export.py uses, for the
  journal, manual entries, bank transactions and files. bexio grants those
  scopes only with write access, so this login is kept apart and removed again
  after the run (`logout --export-scope`).

Benchi logs in once, on the Mac mini's desktop Terminal:

    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/oauth.py login
    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/oauth.py login --export-scope

The refresh tokens go to the macOS keychain (service "varlock", accounts
"finance:local:BEXIO_REFRESH_TOKEN" and "finance:local:BEXIO_EXPORT_REFRESH_TOKEN").
bexio rotates each token on every use, so each refresh saves the new one back
into that same item. Access tokens stay in memory. Nothing here prints or writes
a token or the client secret.

The keychain is read and written through the Security framework (ctypes),
never `security -w <value>`, which would put the value on the command line.

Standard library only, so it runs on the system Python.
"""

import base64
import ctypes
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTHORIZE_URL = "https://auth.bexio.com/realms/bexio/protocol/openid-connect/auth"
TOKEN_URL = "https://auth.bexio.com/realms/bexio/protocol/openid-connect/token"

# The redirect URL registered with bexio. The listener binds the loopback
# address only, and the port is the one bexio has for this redirect.
REDIRECT_URI = "http://localhost:8765/callback"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8765
LOGIN_TIMEOUT = 300  # seconds to wait for the browser to come back

# Read-only: every scope is a *_show or an identity scope. A write scope here
# would let this login change the account, so test_oauth checks this list.
SCOPE = (
    "openid offline_access contact_show note_show article_show kb_invoice_show "
    "kb_offer_show kb_order_show kb_delivery_show kb_bill_show kb_expense_show "
    "bank_account_show bank_payment_show payroll_employee_show payroll_absence_show "
    "payroll_paystub_show"
)

# The export login: the read-only scopes plus the two bexio grants only with
# write access. Used for one export run, then removed with `logout --export-scope`.
EXPORT_SCOPE = SCOPE + " accounting file"

KEYCHAIN_SERVICE = "varlock"
KEYCHAIN_ACCOUNT = "finance:local:BEXIO_REFRESH_TOKEN"
EXPORT_KEYCHAIN_ACCOUNT = "finance:local:BEXIO_EXPORT_REFRESH_TOKEN"
LOGIN_COMMAND = (
    "varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- "
    "python3 finance/bexio/oauth.py login"
)
EXPORT_LOGIN_COMMAND = LOGIN_COMMAND + " --export-scope"
LOGOUT_COMMAND = (
    "varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- "
    "python3 finance/bexio/oauth.py logout --export-scope"
)

_ERR_ITEM_NOT_FOUND = -25300


class OAuthError(Exception):
    """bexio refused a token request, or the login cannot continue. The message never holds a token."""


class KeychainError(Exception):
    """The keychain answered with an error status."""


def _login_hint(export_scope=False):
    return "run `{}` again".format(EXPORT_LOGIN_COMMAND if export_scope else LOGIN_COMMAND)


def _client_credentials():
    client_id = os.environ.get("BEXIO_CLIENT_ID")
    client_secret = os.environ.get("BEXIO_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("BEXIO_CLIENT_ID and BEXIO_CLIENT_SECRET must be set; run this under varlock (see the module docstring)")
    return {"client_id": client_id, "client_secret": client_secret}


def _challenge(verifier):
    # PKCE (RFC 7636), method S256: base64url of the SHA-256 digest, without padding.
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(state, challenge, scope=SCOPE, redirect_uri=REDIRECT_URI):
    query = {
        "client_id": _client_credentials()["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(query)


def _token_request(fields):
    """POST to the token endpoint and return the decoded body.

    On a refusal this raises OAuthError with bexio's error code only, so the
    request body (which holds the tokens) never reaches a message or a log.
    """
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise OAuthError("bexio answered HTTP {} ({})".format(err.code, _error_code(err))) from None
    except urllib.error.URLError:
        raise OAuthError("could not reach bexio's login server") from None


def _error_code(err):
    try:
        return json.loads(err.read().decode("utf-8")).get("error", "no error code")
    except (ValueError, AttributeError):
        return "no error code"


def exchange_code(code, verifier, redirect_uri=REDIRECT_URI):
    """Trade the login's authorization code for tokens. redirect_uri must be the one the login started with."""
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    fields.update(_client_credentials())
    return _token_request(fields)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/callback":
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(url.query)
        server = self.server
        if not secrets.compare_digest(params.get("state", [""])[0].encode("utf-8"), server.expected_state.encode("utf-8")):
            server.outcome = {"error": "state does not match this login"}
        elif "error" in params:
            server.outcome = {"error": params["error"][0]}
        elif "code" in params:
            server.outcome = {"code": params["code"][0]}
        else:
            self.send_error(400)
            return
        body = b"<p>bexio login received. You can close this window.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # The default log prints the request line, and that line holds the
        # authorization code. Nothing is logged.
        pass


def wait_for_code(expected_state, export_scope=False):
    """Serve the callback once and return the authorization code.

    Other requests (a favicon, a stray reload) get their own answer and do not
    end the wait. A bexio error or a state mismatch ends it with OAuthError.
    """
    server = http.server.HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    server.expected_state = expected_state
    server.outcome = None
    deadline = time.monotonic() + LOGIN_TIMEOUT
    try:
        while server.outcome is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OAuthError("no answer from bexio within {} seconds; {}".format(LOGIN_TIMEOUT, _login_hint(export_scope)))
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    if "error" in server.outcome:
        raise OAuthError("bexio did not grant the login ({}); {}".format(server.outcome["error"], _login_hint(export_scope)))
    return server.outcome["code"]


# Keychain: the Security framework through ctypes. The legacy item calls are
# used because they update the password of an existing item in place, which
# keeps the item's access rights; the `security` command would put the value on argv.

def _security():
    lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    lib.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    lib.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    lib.SecKeychainItemModifyAttributesAndData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
    lib.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    lib.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
    lib.SecKeychainItemDelete.restype = ctypes.c_int32
    lib.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.SecKeychainItemFreeContent.restype = ctypes.c_int32
    return lib


def _release(ref):
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease(ref)


def _check(status, what):
    if status != 0:
        raise KeychainError("could not {} (keychain status {})".format(what, status))


def read_refresh_token(keychain_account=KEYCHAIN_ACCOUNT):
    """The stored refresh token of that login, or None when it has not run yet."""
    sec = _security()
    service = KEYCHAIN_SERVICE.encode("ascii")
    account = keychain_account.encode("ascii")
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = sec.SecKeychainFindGenericPassword(
        None, len(service), service, len(account), account,
        ctypes.byref(length), ctypes.byref(data), ctypes.byref(item),
    )
    if status == _ERR_ITEM_NOT_FOUND:
        return None
    _check(status, "read the bexio refresh token")
    try:
        return ctypes.string_at(data.value, length.value).decode("ascii")
    finally:
        sec.SecKeychainItemFreeContent(None, data)
        _release(item)


def write_refresh_token(token, keychain_account=KEYCHAIN_ACCOUNT):
    """Save the token: update that login's item in place, or create it on its first login."""
    sec = _security()
    service = KEYCHAIN_SERVICE.encode("ascii")
    account = keychain_account.encode("ascii")
    value = token.encode("ascii")
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = sec.SecKeychainFindGenericPassword(
        None, len(service), service, len(account), account,
        ctypes.byref(length), ctypes.byref(data), ctypes.byref(item),
    )
    if status == _ERR_ITEM_NOT_FOUND:
        status = sec.SecKeychainAddGenericPassword(
            None, len(service), service, len(account), account,
            len(value), value, None,
        )
        _check(status, "create the bexio refresh token")
        return
    _check(status, "find the bexio refresh token")
    try:
        sec.SecKeychainItemFreeContent(None, data)
        status = sec.SecKeychainItemModifyAttributesAndData(item, None, len(value), value)
        _check(status, "update the bexio refresh token")
    finally:
        _release(item)


def delete_refresh_token(keychain_account):
    """Remove that login's item. Returns False when there was none to remove."""
    sec = _security()
    service = KEYCHAIN_SERVICE.encode("ascii")
    account = keychain_account.encode("ascii")
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = sec.SecKeychainFindGenericPassword(
        None, len(service), service, len(account), account,
        ctypes.byref(length), ctypes.byref(data), ctypes.byref(item),
    )
    if status == _ERR_ITEM_NOT_FOUND:
        return False
    _check(status, "find the bexio refresh token")
    try:
        sec.SecKeychainItemFreeContent(None, data)
        _check(sec.SecKeychainItemDelete(item), "delete the bexio refresh token")
    finally:
        _release(item)
    return True


def login(export_scope=False):
    """Run the one-time login: browser, callback, code exchange, keychain."""
    verifier = secrets.token_urlsafe(64)  # PKCE: 43 to 128 characters
    state = secrets.token_urlsafe(32)
    scope = EXPORT_SCOPE if export_scope else SCOPE
    keychain_account = EXPORT_KEYCHAIN_ACCOUNT if export_scope else KEYCHAIN_ACCOUNT
    url = authorize_url(state, _challenge(verifier), scope)
    print("Open this URL to log in to bexio with {} scopes:".format("export" if export_scope else "read-only"))
    print(url)
    webbrowser.open(url)
    code = wait_for_code(state, export_scope)
    body = exchange_code(code, verifier)
    refresh = body.get("refresh_token")
    if not refresh:
        raise OAuthError("bexio sent no refresh token; the login needs offline_access in its scopes")
    write_refresh_token(refresh, keychain_account)
    print("Saved the bexio {}refresh token in the keychain (service {}, account {}).".format(
        "export " if export_scope else "", KEYCHAIN_SERVICE, keychain_account))


def refresh_access_token(export_scope=False):
    """An access token for the API, from the stored refresh token of that login.

    bexio rotates the refresh token on every use: the new one is saved in place
    before this returns. The access token is only returned, never stored.
    Raises OAuthError when there is no refresh token or bexio refuses it.
    """
    keychain_account = EXPORT_KEYCHAIN_ACCOUNT if export_scope else KEYCHAIN_ACCOUNT
    label = "export refresh token" if export_scope else "refresh token"
    hint = _login_hint(export_scope)
    current = read_refresh_token(keychain_account)
    if current is None:
        raise OAuthError("no bexio {} in the keychain; {}".format(label, hint))
    fields = {"grant_type": "refresh_token", "refresh_token": current}
    fields.update(_client_credentials())
    try:
        body = _token_request(fields)
    except OAuthError as err:
        raise OAuthError("{}. If bexio refused the {} (expired or revoked), {}".format(err, label, hint)) from None
    rotated = body.get("refresh_token")
    if not rotated or not body.get("access_token"):
        raise OAuthError("bexio's refresh answer had no tokens; {}".format(hint))
    try:
        write_refresh_token(rotated, keychain_account)
    except KeychainError as err:
        # The old token is spent once bexio rotated it, so the new one must be saved or the login is lost.
        raise OAuthError("bexio rotated the {} but it could not be saved ({}); {}".format(label, err, hint)) from None
    return body["access_token"]


USAGE = "usage: python3 finance/bexio/oauth.py login [--export-scope] | logout --export-scope"


def main(argv):
    export_scope = "--export-scope" in argv
    argv = [arg for arg in argv if arg != "--export-scope"]
    if argv == ["login"]:
        try:
            login(export_scope)
        except (OAuthError, KeychainError) as err:
            raise SystemExit("login failed: {}".format(err))
        return
    # Only the export login can be removed here: the read-only login stays.
    if argv == ["logout"] and export_scope:
        try:
            removed = delete_refresh_token(EXPORT_KEYCHAIN_ACCOUNT)
        except KeychainError as err:
            raise SystemExit("logout failed: {}".format(err))
        if removed:
            print("Removed the bexio export refresh token from the keychain. Revoke the app's access in bexio too (finance/bexio/README.md).")
        else:
            print("No bexio export refresh token in the keychain; nothing to remove.")
        return
    raise SystemExit(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
