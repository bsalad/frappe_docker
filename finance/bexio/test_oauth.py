"""Offline tests for oauth.py: login exchange, refresh with rotation, refusals, scopes. No network, no keychain.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import io
import os
import unittest
import urllib.error
from unittest import mock

import oauth
from oauth import KeychainError, OAuthError

FAKE_ENV = {"BEXIO_CLIENT_ID": "client-id-not-real", "BEXIO_CLIENT_SECRET": "secret-not-real"}


class ScopeTest(unittest.TestCase):
    def test_scope_has_no_write_scope(self):
        # Every scope is an identity scope or a *_show (read). A write scope
        # such as `accounting` or a *_edit would let the login change the account.
        for scope in oauth.SCOPE.split():
            self.assertTrue(
                scope in ("openid", "offline_access") or scope.endswith("_show"),
                "unexpected scope {!r}".format(scope),
            )
        self.assertNotIn("accounting", oauth.SCOPE.split())
        self.assertFalse(any(s.endswith("_edit") for s in oauth.SCOPE.split()))

    def test_scope_has_the_read_scopes_the_bead_asks_for(self):
        self.assertEqual(
            oauth.SCOPE.split(),
            "openid offline_access contact_show note_show article_show kb_invoice_show "
            "kb_offer_show kb_order_show kb_delivery_show kb_bill_show kb_expense_show "
            "bank_account_show bank_payment_show".split(),
        )


class PkceTest(unittest.TestCase):
    def test_challenge_matches_the_rfc_7636_example(self):
        # RFC 7636, appendix B: the verifier and challenge pair.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(oauth._challenge(verifier), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


class AuthorizeUrlTest(unittest.TestCase):
    def test_url_asks_for_the_read_scopes_with_state_and_pkce(self):
        with mock.patch.dict(os.environ, FAKE_ENV):
            url = oauth.authorize_url("state-value", "challenge-value")
        self.assertTrue(url.startswith(oauth.AUTHORIZE_URL + "?"))
        query = dict(q.split("=", 1) for q in url.split("?", 1)[1].split("&"))
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["state"], "state-value")
        self.assertEqual(query["code_challenge"], "challenge-value")
        self.assertIn("client_id", query)
        self.assertNotIn("client_secret", url)
        self.assertNotIn("secret-not-real", url)


class LoginExchangeTest(unittest.TestCase):
    def test_code_is_exchanged_with_the_verifier_and_the_secret(self):
        body = {"access_token": "access-not-real", "refresh_token": "refresh-not-real"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "_token_request", return_value=body) as request:
            self.assertEqual(oauth.exchange_code("code-not-real", "verifier-not-real"), body)
        fields = request.call_args.args[0]
        self.assertEqual(fields["grant_type"], "authorization_code")
        self.assertEqual(fields["code"], "code-not-real")
        self.assertEqual(fields["code_verifier"], "verifier-not-real")
        self.assertEqual(fields["redirect_uri"], oauth.REDIRECT_URI)
        self.assertEqual(fields["client_secret"], "secret-not-real")


class RefreshTest(unittest.TestCase):
    def test_refresh_returns_access_token_and_saves_the_rotated_refresh_token(self):
        answer = {"access_token": "access-new", "refresh_token": "refresh-rotated"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", return_value=answer) as request, \
                mock.patch.object(oauth, "write_refresh_token") as write:
            self.assertEqual(oauth.refresh_access_token(), "access-new")
        fields = request.call_args.args[0]
        self.assertEqual(fields["grant_type"], "refresh_token")
        self.assertEqual(fields["refresh_token"], "refresh-old")
        write.assert_called_once_with("refresh-rotated")

    def test_refused_refresh_token_asks_for_a_new_login(self):
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", side_effect=OAuthError("bexio answered HTTP 400 (invalid_grant)")), \
                mock.patch.object(oauth, "write_refresh_token") as write:
            with self.assertRaises(OAuthError) as ctx:
                oauth.refresh_access_token()
        self.assertIn("invalid_grant", str(ctx.exception))
        self.assertIn("oauth.py login", str(ctx.exception))
        self.assertNotIn("refresh-old", str(ctx.exception))
        write.assert_not_called()

    def test_missing_refresh_token_asks_for_a_login(self):
        with mock.patch.object(oauth, "read_refresh_token", return_value=None), \
                mock.patch.object(oauth, "_token_request") as request:
            with self.assertRaises(OAuthError) as ctx:
                oauth.refresh_access_token()
        self.assertIn("oauth.py login", str(ctx.exception))
        request.assert_not_called()

    def test_failed_save_of_the_rotated_token_says_so_and_hides_it(self):
        answer = {"access_token": "access-new", "refresh_token": "refresh-rotated"}
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", return_value=answer), \
                mock.patch.object(oauth, "write_refresh_token", side_effect=KeychainError("could not update (keychain status -1)")):
            with self.assertRaises(OAuthError) as ctx:
                oauth.refresh_access_token()
        self.assertIn("could not be saved", str(ctx.exception))
        self.assertIn("oauth.py login", str(ctx.exception))
        self.assertNotIn("refresh-rotated", str(ctx.exception))

    def test_answer_without_tokens_is_an_error(self):
        with mock.patch.dict(os.environ, FAKE_ENV), \
                mock.patch.object(oauth, "read_refresh_token", return_value="refresh-old"), \
                mock.patch.object(oauth, "_token_request", return_value={"access_token": "a"}), \
                mock.patch.object(oauth, "write_refresh_token") as write:
            with self.assertRaises(OAuthError):
                oauth.refresh_access_token()
        write.assert_not_called()


class TokenRequestTest(unittest.TestCase):
    def _http_error(self, code, body):
        return urllib.error.HTTPError(oauth.TOKEN_URL, code, "err", {}, io.BytesIO(body))

    def test_refusal_carries_bexio_error_code_only(self):
        err = self._http_error(400, b'{"error":"invalid_grant","error_description":"refresh-old is not active"}')
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(OAuthError) as ctx:
                oauth._token_request({"grant_type": "refresh_token", "refresh_token": "refresh-old"})
        self.assertIn("invalid_grant", str(ctx.exception))
        self.assertNotIn("refresh-old", str(ctx.exception))

    def test_refusal_with_a_body_that_is_not_json(self):
        err = self._http_error(502, b"<html>bad gateway</html>")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(OAuthError) as ctx:
                oauth._token_request({"grant_type": "refresh_token"})
        self.assertIn("HTTP 502", str(ctx.exception))

    def test_network_failure_is_not_called_a_refusal(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(OAuthError) as ctx:
                oauth._token_request({"grant_type": "refresh_token"})
        self.assertIn("could not reach", str(ctx.exception))


class CallbackHandlerTest(unittest.TestCase):
    """The handler's decisions, without a socket: a fake server and request."""

    def _run(self, path, expected_state="state-ok"):
        handler = oauth._CallbackHandler.__new__(oauth._CallbackHandler)
        handler.path = path
        handler.server = mock.Mock(expected_state=expected_state, outcome=None)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_error = mock.Mock()
        handler.do_GET()
        return handler.server.outcome, handler

    def test_code_with_the_right_state_is_taken(self):
        outcome, _ = self._run("/callback?code=code-not-real&state=state-ok")
        self.assertEqual(outcome, {"code": "code-not-real"})

    def test_state_mismatch_ends_the_login(self):
        outcome, _ = self._run("/callback?code=code-not-real&state=other", expected_state="state-ok")
        self.assertIn("error", outcome)
        self.assertNotIn("code", outcome)

    def test_bexio_error_is_reported(self):
        outcome, _ = self._run("/callback?error=access_denied&state=state-ok")
        self.assertEqual(outcome, {"error": "access_denied"})

    def test_other_paths_are_404_and_do_not_end_the_wait(self):
        outcome, handler = self._run("/favicon.ico")
        self.assertIsNone(outcome)
        handler.send_error.assert_called_once_with(404)

    def test_requests_are_not_logged(self):
        # The default log prints the request line, which holds the code.
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            handler = oauth._CallbackHandler.__new__(oauth._CallbackHandler)
            handler.log_message("%s", "GET /callback?code=code-not-real")
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
