"""Tailnet login page for the bexio login: log in from the MacBook, no varlock command on the mini.

The helper runs on the Mac mini as a LaunchAgent in Benchi's user session, under
varlock, so the client id and secret come from the keychain and never from a file.
It binds 127.0.0.1 only. `tailscale serve` puts it on the tailnet over https, never
Funnel (see finance/bexio/README.md):

    tailscale serve --bg --https=<port> http://127.0.0.1:8794

- /login starts one PKCE login: the read-only scopes, or /login?scope=export for
  the export login. The browser is redirected to bexio.
- /callback takes bexio's code, exchanges it, and saves the rotated refresh token in
  the same keychain item oauth.py uses. The page says "logged in" and the granted
  scopes. It never shows a token, and nothing is logged.

Each login has its own one-time state, valid for oauth.LOGIN_TIMEOUT seconds. A
state is used once: a second callback with it is refused, and a new /login replaces
the pending state, so one login is waiting at a time.

`tailscale serve` passes the caller's identity in the Tailscale-User-Login header.
Every request is refused unless that header is the configured tailnet login (--tailnet-user),
so the page is bound to one person even inside the tailnet.

The redirect URI must be registered on the bexio OAuth app and passed with
--redirect-uri, exactly as registered. The localhost one stays the fallback
(`python3 finance/bexio/oauth.py login`).

Standard library only, so it runs on the system Python.
"""

import argparse
import html
import http.server
import secrets
import sys
import time
import urllib.parse

import oauth

DEFAULT_PORT = 8794  # loopback only; tailscale serve forwards the tailnet https port to it


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Tailscale-User-Login", "") != self.server.tailnet_user:
            # Refused before any state is made or any code is read.
            self._page(403, "not the bexio login's owner on this tailnet")
            return
        url = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(url.query)
        if url.path == "/login":
            self._login(params)
        elif url.path == "/callback":
            self._callback(params)
        else:
            self.send_error(404)

    def _login(self, params):
        scope_name = params.get("scope", [""])[0]
        if scope_name not in ("", "export"):
            self._page(400, "unknown login; open /login or /login?scope=export")
            return
        export_scope = scope_name == "export"
        verifier = secrets.token_urlsafe(64)  # PKCE: 43 to 128 characters
        state = secrets.token_urlsafe(32)
        self.server.pending.clear()  # one login at a time: a new one replaces the waiting one
        self.server.pending[state] ={"verifier": verifier, "export_scope": export_scope, "started": time.monotonic()}
        scope = oauth.EXPORT_SCOPE if export_scope else oauth.SCOPE
        url = oauth.authorize_url(state, oauth._challenge(verifier), scope, self.server.redirect_uri)
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _callback(self, params):
        # A wrong state finds nothing, so it leaves the real login waiting for its own callback.
        login = self.server.pending.pop(params.get("state", [""])[0], None)
        if login is None or time.monotonic() - login["started"] > oauth.LOGIN_TIMEOUT:
            self._page(400, "This login has expired or was already used. Open /login again.")
            return
        if "error" in params:
            self._page(400, "bexio did not grant the login. Open /login again to retry.")
            return
        code = params.get("code", [""])[0]
        if not code:
            self._page(400, "bexio sent no code. Open /login again to retry.")
            return
        export_scope = login["export_scope"]
        scope = oauth.EXPORT_SCOPE if export_scope else oauth.SCOPE
        account = oauth.EXPORT_KEYCHAIN_ACCOUNT if export_scope else oauth.KEYCHAIN_ACCOUNT
        try:
            body = oauth.exchange_code(code, login["verifier"], self.server.redirect_uri)
            refresh = body.get("refresh_token")
            if not refresh:
                raise oauth.OAuthError("bexio sent no refresh token; the login needs offline_access in its scopes")
            oauth.write_refresh_token(refresh, account)
        except (oauth.OAuthError, oauth.KeychainError, ValueError) as err:
            # The messages carry bexio's error code or a keychain status, never a token.
            self._page(502, "login failed: {}".format(err))
            return
        self._page(200, "logged in, scopes: {}".format(body.get("scope") or scope))

    def _page(self, status, message):
        body = "<!doctype html><meta charset=utf-8><title>bexio login</title><p>{}</p>".format(html.escape(message)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # The default log prints the request line, and the callback's line holds the
        # authorization code. Nothing is logged.
        pass


class _Server(http.server.HTTPServer):
    def __init__(self, address, redirect_uri, tailnet_user):
        super().__init__(address, _Handler)
        self.redirect_uri = redirect_uri
        self.tailnet_user = tailnet_user  # the one tailnet login allowed to log in
        self.pending = {}  # state -> the login's verifier, scope and start time


def make_server(redirect_uri, tailnet_user, port=DEFAULT_PORT):
    """The login server on 127.0.0.1:port (0 for any free port). Its callback is redirect_uri."""
    return _Server((oauth.CALLBACK_HOST, port), redirect_uri, tailnet_user)


def main(argv):
    parser = argparse.ArgumentParser(description="Serve the bexio login page on 127.0.0.1 for tailscale serve.")
    parser.add_argument("--redirect-uri", required=True,
                        help="the tailnet callback registered at bexio, e.g. https://<mac>.<tailnet>.ts.net:<port>/callback")
    parser.add_argument("--tailnet-user", required=True,
                        help="the tailnet login allowed to log in, as tailscale shows it (the only identity the page accepts)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback port (default %(default)s)")
    args = parser.parse_args(argv)
    oauth._client_credentials()  # stops here, with the varlock hint, when the client id or secret is not injected
    server = make_server(args.redirect_uri, args.tailnet_user, args.port)
    print("bexio login page on 127.0.0.1:{}, callback {}".format(server.server_port, args.redirect_uri), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
