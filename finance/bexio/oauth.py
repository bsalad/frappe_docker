"""OAuth login for the bexio API: one login by hand, then refresh tokens from the keychain.

The login asks bexio for read-only scopes (SCOPE below), and bexio enforces
them: the account cannot be changed with this token, whatever the code does.
A later bead adds `accounting` to SCOPE, behind a consent of its own.

Benchi logs in once, on the Mac mini's desktop Terminal:

    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/oauth.py login

The refresh token goes to the macOS keychain (service "varlock", account
"finance:local:BEXIO_REFRESH_TOKEN"). bexio rotates it on every use, so each
refresh saves the new one back into that same item. Access tokens stay in
memory. Nothing here prints or writes a token or the client secret.

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
    "bank_account_show bank_payment_show"
)

KEYCHAIN_SERVICE = "varlock"
KEYCHAIN_ACCOUNT = "finance:local:BEXIO_REFRESH_TOKEN"
LOGIN_COMMAND = (
    "varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- "
    "python3 finance/bexio/oauth.py login"
)

_ERR_ITEM_NOT_FOUND = -25300


class OAuthError(Exception):
    """bexio refused a token request, or the login cannot continue. The message never holds a token."""


class KeychainError(Exception):
    """The keychain answered with an error status."""


def _login_hint():
    return "run `{}` again".format(LOGIN_COMMAND)


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


def authorize_url(state, challenge):
    query = {
        "client_id": _client_credentials()["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
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


def exchange_code(code, verifier):
    """Trade the login's authorization code for tokens."""
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
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


def wait_for_code(expected_state):
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
                raise OAuthError("no answer from bexio within {} seconds; {}".format(LOGIN_TIMEOUT, _login_hint()))
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    if "error" in server.outcome:
        raise OAuthError("bexio did not grant the login ({}); {}".format(server.outcome["error"], _login_hint()))
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


def read_refresh_token():
    """The stored refresh token, or None when the login has not run yet."""
    sec = _security()
    service = KEYCHAIN_SERVICE.encode("ascii")
    account = KEYCHAIN_ACCOUNT.encode("ascii")
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


def write_refresh_token(token):
    """Save the token: update the existing item in place, or create it on the first login."""
    sec = _security()
    service = KEYCHAIN_SERVICE.encode("ascii")
    account = KEYCHAIN_ACCOUNT.encode("ascii")
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


def login():
    """Run the one-time login: browser, callback, code exchange, keychain."""
    verifier = secrets.token_urlsafe(64)  # PKCE: 43 to 128 characters
    state = secrets.token_urlsafe(32)
    url = authorize_url(state, _challenge(verifier))
    print("Open this URL to log in to bexio with read-only scopes:")
    print(url)
    webbrowser.open(url)
    code = wait_for_code(state)
    body = exchange_code(code, verifier)
    refresh = body.get("refresh_token")
    if not refresh:
        raise OAuthError("bexio sent no refresh token; the login needs offline_access in its scopes")
    write_refresh_token(refresh)
    print("Saved the bexio refresh token in the keychain (service {}, account {}).".format(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT))


def refresh_access_token():
    """An access token for the API, from the stored refresh token.

    bexio rotates the refresh token on every use: the new one is saved in place
    before this returns. The access token is only returned, never stored.
    Raises OAuthError when there is no refresh token or bexio refuses it.
    """
    current = read_refresh_token()
    if current is None:
        raise OAuthError("no bexio refresh token in the keychain; {}".format(_login_hint()))
    fields = {"grant_type": "refresh_token", "refresh_token": current}
    fields.update(_client_credentials())
    try:
        body = _token_request(fields)
    except OAuthError as err:
        raise OAuthError("{}. If bexio refused the refresh token (expired or revoked), {}".format(err, _login_hint())) from None
    rotated = body.get("refresh_token")
    if not rotated or not body.get("access_token"):
        raise OAuthError("bexio's refresh answer had no tokens; {}".format(_login_hint()))
    try:
        write_refresh_token(rotated)
    except KeychainError as err:
        # The old token is spent once bexio rotated it, so the new one must be saved or the login is lost.
        raise OAuthError("bexio rotated the refresh token but it could not be saved ({}); {}".format(err, _login_hint())) from None
    return body["access_token"]


def main(argv):
    if argv == ["login"]:
        try:
            login()
        except (OAuthError, KeychainError) as err:
            raise SystemExit("login failed: {}".format(err))
        return
    raise SystemExit("usage: python3 finance/bexio/oauth.py login")


if __name__ == "__main__":
    main(sys.argv[1:])
