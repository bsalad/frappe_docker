"""Offline tests for login_helper.py: the tailnet login page. No network, no keychain.

The token endpoint and the keychain write are mocked; the server runs on a free
loopback port. Invented client id, code and token values only.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import http.client
import io
import os
import threading
import unittest
import urllib.parse
from unittest import mock

import login_helper
import oauth

REDIRECT = "https://login.example.test:8461/callback"
FAKE_TOKEN = "fake-refresh-token-123"
FAKE_ENV = {"BEXIO_CLIENT_ID": "fake-client-id", "BEXIO_CLIENT_SECRET": "fake-client-secret"}


class LoginHelperTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, FAKE_ENV)
        self.env.start()
        self.server = login_helper.make_server(REDIRECT, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.exchange = mock.patch.object(oauth, "exchange_code", return_value={"refresh_token": FAKE_TOKEN, "access_token": "fake-access", "scope": "openid offline_access contact_show"}).start()
        self.write = mock.patch.object(oauth, "write_refresh_token").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.env.stop()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        headers = dict(resp.getheaders())
        conn.close()
        return resp.status, headers, body

    def _start_login(self, path="/login"):
        status, headers, _ = self._get(path)
        self.assertEqual(status, 302)
        return headers["Location"]

    def _state_of(self, location):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["state"][0]

    def test_login_redirects_to_bexio_with_read_only_scope_and_tailnet_redirect(self):
        location = self._start_login()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        self.assertTrue(location.startswith(oauth.AUTHORIZE_URL + "?"))
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        self.assertEqual(query["scope"], [oauth.SCOPE])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["client_id"], ["fake-client-id"])

    def test_export_login_uses_export_scope_and_its_own_keychain_item(self):
        location = self._start_login("/login?scope=export")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["scope"], [oauth.EXPORT_SCOPE])
        self._get("/callback?state={}&code=fake-code".format(self._state_of(location)))
        self.write.assert_called_once_with(FAKE_TOKEN, oauth.EXPORT_KEYCHAIN_ACCOUNT)

    def test_unknown_login_scope_is_refused(self):
        status, _, _ = self._get("/login?scope=everything")
        self.assertEqual(status, 400)

    def test_callback_saves_the_token_and_shows_only_the_scopes(self):
        location = self._start_login()
        state = self._state_of(location)
        challenge = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["code_challenge"][0]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status, _, body = self._get("/callback?state={}&code=fake-code".format(state))
        self.assertEqual(status, 200)
        self.assertIn("logged in, scopes: openid offline_access contact_show", body)
        self.assertNotIn(FAKE_TOKEN, body)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        self.exchange.assert_called_once()
        code, verifier = self.exchange.call_args.args[0], self.exchange.call_args.args[1]
        self.assertEqual(code, "fake-code")
        self.assertEqual(self.exchange.call_args.args[2], REDIRECT)
        self.assertEqual(oauth._challenge(verifier), challenge)  # PKCE: the verifier matches the login that started
        self.write.assert_called_once_with(FAKE_TOKEN, oauth.KEYCHAIN_ACCOUNT)

    def test_a_state_is_used_once(self):
        state = self._state_of(self._start_login())
        self._get("/callback?state={}&code=fake-code".format(state))
        status, _, body = self._get("/callback?state={}&code=fake-code".format(state))
        self.assertEqual(status, 400)
        self.assertIn("already used", body)
        self.exchange.assert_called_once()
        self.write.assert_called_once()

    def test_a_wrong_state_is_refused_and_does_not_end_the_real_login(self):
        state = self._state_of(self._start_login())
        status, _, _ = self._get("/callback?state=not-the-state&code=fake-code")
        self.assertEqual(status, 400)
        self.exchange.assert_not_called()
        self.write.assert_not_called()
        status, _, body = self._get("/callback?state={}&code=fake-code".format(state))
        self.assertEqual(status, 200)
        self.assertIn("logged in", body)

    def test_an_expired_state_is_refused(self):
        state = self._state_of(self._start_login())
        with mock.patch.object(login_helper.time, "monotonic", return_value=login_helper.time.monotonic() + oauth.LOGIN_TIMEOUT + 1):
            status, _, _ = self._get("/callback?state={}&code=fake-code".format(state))
        self.assertEqual(status, 400)
        self.exchange.assert_not_called()

    def test_bexio_refusal_saves_nothing(self):
        state = self._state_of(self._start_login())
        status, _, body = self._get("/callback?state={}&error=access_denied".format(state))
        self.assertEqual(status, 400)
        self.assertIn("did not grant", body)
        self.exchange.assert_not_called()
        self.write.assert_not_called()

    def test_a_refused_token_request_shows_the_error_code_not_a_token(self):
        state = self._state_of(self._start_login())
        self.exchange.side_effect = oauth.OAuthError("bexio answered HTTP 400 (invalid_grant)")
        status, _, body = self._get("/callback?state={}&code=fake-code".format(state))
        self.assertEqual(status, 502)
        self.assertIn("invalid_grant", body)
        self.assertNotIn("fake-code", body)
        self.write.assert_not_called()

    def test_a_login_without_a_refresh_token_saves_nothing(self):
        state = self._state_of(self._start_login())
        self.exchange.return_value = {"access_token": "fake-access"}
        status, _, body = self._get("/callback?state={}&code=fake-code".format(state))
        self.assertEqual(status, 502)
        self.assertIn("no refresh token", body)
        self.write.assert_not_called()

    def test_a_keychain_failure_is_shown_without_the_token(self):
        state = self._state_of(self._start_login())
        self.write.side_effect = oauth.KeychainError("could not create the bexio refresh token (keychain status -25308)")
        status, _, body = self._get("/callback?state={}&code=fake-code".format(state))
        self.assertEqual(status, 502)
        self.assertIn("keychain status -25308", body)
        self.assertNotIn(FAKE_TOKEN, body)

    def test_the_page_is_not_cached_and_the_login_is_not_logged(self):
        state = self._state_of(self._start_login())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            _, headers, _ = self._get("/callback?state={}&code=fake-code".format(state))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_an_unknown_path_is_404_and_starts_nothing(self):
        status, _, _ = self._get("/")
        self.assertEqual(status, 404)
        self.assertEqual(self.server.pending, {})

    def test_the_server_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(oauth.CALLBACK_HOST, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
