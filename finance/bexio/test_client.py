"""Offline tests for client.py: paging, retries and error handling, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import io
import os
import unittest
import urllib.error
from unittest import mock

import client
from client import BexioError, Client


def _client():
    return Client(token="test-token-not-real")


class OffsetPagingTest(unittest.TestCase):
    def test_walks_all_rows_when_server_caps_the_page(self):
        # The server returns at most 100 rows whatever limit it is asked for
        # (bills did this: 500 asked, 100 back). Every row must still come out.
        rows = list(range(250))
        c = _client()

        def fake_get(path, params=None):
            offset = params["offset"]
            return rows[offset:offset + 100]

        with mock.patch.object(c, "get", side_effect=fake_get):
            self.assertEqual(list(c.paginate("/x", page_size=500)), rows)

    def test_stops_on_empty_page_not_on_short_page(self):
        c = _client()
        pages = [[1, 2], [3], []]
        with mock.patch.object(c, "get", side_effect=pages) as get:
            self.assertEqual(list(c.paginate("/x", page_size=500)), [1, 2, 3])
            self.assertEqual(get.call_count, 3)

    def test_empty_list_yields_nothing(self):
        c = _client()
        with mock.patch.object(c, "get", return_value=[]):
            self.assertEqual(list(c.paginate("/x")), [])

    def test_non_list_body_is_an_error(self):
        c = _client()
        with mock.patch.object(c, "get", return_value={"unexpected": True}):
            with self.assertRaises(BexioError):
                list(c.paginate("/x"))


class IgnoredOffsetTest(unittest.TestCase):
    # /3.0/taxes and /3.0/accounting/business_years ignore limit and offset
    # and return the same rows on every page; the live run looped to its cap.
    ROWS = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]

    def test_offset_stops_when_a_page_brings_no_new_id(self):
        c = _client()
        with mock.patch.object(c, "get", return_value=list(self.ROWS)) as get:
            self.assertEqual(list(c.paginate("/3.0/taxes")), self.ROWS)
            self.assertEqual(get.call_count, 2)

    def test_offset_yields_each_id_once_across_overlapping_pages(self):
        c = _client()
        pages = [self.ROWS[:2], self.ROWS[1:], self.ROWS[1:]]
        with mock.patch.object(c, "get", side_effect=pages):
            self.assertEqual(list(c.paginate("/x")), self.ROWS)

    def test_page_numbered_stops_when_a_page_brings_no_new_id(self):
        c = _client()
        body = {"data": list(self.ROWS)}
        with mock.patch.object(c, "get", return_value=body) as get:
            got = list(c.paginate_pages("/p", "page_size", rows_key="data"))
            self.assertEqual(got, self.ROWS)
            self.assertEqual(get.call_count, 2)

    def test_rows_without_id_are_never_dropped(self):
        c = _client()
        rows = [{"a": 1}, {"a": 1}]
        with mock.patch.object(c, "get", side_effect=[rows, []]):
            self.assertEqual(list(c.paginate("/x")), rows)


class PagePagingTest(unittest.TestCase):
    def test_walks_pages_and_reads_rows_under_key(self):
        c = _client()
        bodies = [
            {"data": [1, 2, 3], "paging": {}},
            {"data": [4, 5], "paging": {}},
            {"data": [], "paging": {}},
        ]
        with mock.patch.object(c, "get", side_effect=bodies) as get:
            got = list(c.paginate_pages("/p", "page_size", rows_key="data", page_size=3))
            self.assertEqual(got, [1, 2, 3, 4, 5])
            self.assertEqual([call.args[1]["page"] for call in get.call_args_list], [1, 2, 3])
            self.assertEqual(get.call_args_list[0].args[1]["page_size"], 3)

    def test_uses_the_size_parameter_the_endpoint_names(self):
        c = _client()
        with mock.patch.object(c, "get", side_effect=[{"results": []}]) as get:
            list(c.paginate_pages("/banking", "per-page", rows_key="results"))
            self.assertIn("per-page", get.call_args.args[1])
            self.assertNotIn("page_size", get.call_args.args[1])

    def test_bare_list_body_without_rows_key(self):
        c = _client()
        with mock.patch.object(c, "get", side_effect=[[7], []]):
            self.assertEqual(list(c.paginate_pages("/p", "page_size", page_size=1)), [7])


class ErrorTest(unittest.TestCase):
    def _http_error(self, code, headers=None):
        return urllib.error.HTTPError("https://api.bexio.com/x", code, "err", headers or {}, io.BytesIO(b""))

    def test_404_raises_without_retry(self):
        c = _client()
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(404)) as opened:
            with self.assertRaises(BexioError) as ctx:
                c.get("/x")
            self.assertEqual(ctx.exception.status, 404)
            self.assertEqual(opened.call_count, 1)

    def test_error_message_never_contains_the_token(self):
        c = Client(token="SECRET-TOKEN-VALUE")
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(403)):
            with self.assertRaises(BexioError) as ctx:
                c.get("/x")
        self.assertNotIn("SECRET-TOKEN-VALUE", str(ctx.exception))

    def test_429_is_retried_after_retry_after(self):
        c = _client()
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"[]"
        errors = [self._http_error(429, {"Retry-After": "0"}), ok]
        with mock.patch("urllib.request.urlopen", side_effect=errors), \
                mock.patch.object(client.time, "sleep") as sleep:
            self.assertEqual(c.get("/x"), [])
            sleep.assert_called_once()

    def test_missing_token_refuses_to_start(self):
        import oauth

        with mock.patch.object(oauth, "read_refresh_token", return_value=None), \
                mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                Client()

    def test_only_get_requests_are_issued(self):
        c = _client()
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=ok) as opened:
            c.get("/x")
            self.assertEqual(opened.call_args.args[0].get_method(), "GET")



class TokenSourceTest(unittest.TestCase):
    # The access token comes from the OAuth login; the PAT only stands in
    # where no refresh token exists. The kind is printed, the value never.

    def _resolve(self, refresh_token=None, refresh_result="access-from-oauth", pat=None, read_error=None):
        import oauth

        env = {"BEXIO_TOKEN": pat} if pat else {}
        read = mock.patch.object(oauth, "read_refresh_token", return_value=refresh_token, side_effect=read_error)
        refresh = mock.patch.object(oauth, "refresh_access_token", return_value=refresh_result)
        with read, refresh as refreshed, mock.patch.dict(os.environ, env, clear=True):
            try:
                return client.resolve_token(), refreshed
            except SystemExit as exit_:
                return exit_, refreshed

    def test_oauth_login_wins_over_the_pat(self):
        (token, kind), refreshed = self._resolve(refresh_token="r", pat="pat-value")
        self.assertEqual((token, kind), ("access-from-oauth", "oauth"))
        refreshed.assert_called_once()

    def test_pat_is_the_fallback_without_a_refresh_token(self):
        (token, kind), refreshed = self._resolve(refresh_token=None, pat="pat-value")
        self.assertEqual((token, kind), ("pat-value", "pat"))
        refreshed.assert_not_called()

    def test_unopenable_keychain_counts_as_no_login(self):
        (token, kind), _ = self._resolve(pat="pat-value", read_error=OSError("no Security framework"))
        self.assertEqual(kind, "pat")

    def test_refused_refresh_token_does_not_fall_back_to_the_pat(self):
        import oauth

        with mock.patch.object(oauth, "read_refresh_token", return_value="r"), \
                mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.OAuthError("invalid_grant")), \
                mock.patch.dict(os.environ, {"BEXIO_TOKEN": "pat-value"}, clear=True):
            with self.assertRaises(SystemExit) as caught:
                client.resolve_token()
        self.assertIn("invalid_grant", str(caught.exception))
        self.assertNotIn("pat-value", str(caught.exception))

    def test_neither_token_names_the_login_command(self):
        result, _ = self._resolve()
        self.assertIsInstance(result, SystemExit)
        self.assertIn("oauth.py login", str(result))

    def test_client_prints_the_kind_not_the_value(self):
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with mock.patch.object(client, "resolve_token", return_value=("secret-access-token", "oauth")), redirect_stderr(err):
            c = Client()
        self.assertEqual(c.token_kind, "oauth")
        self.assertIn("OAuth", err.getvalue())
        self.assertNotIn("secret-access-token", err.getvalue())


class ExportTokenTest(unittest.TestCase):
    # The export login reads its own keychain item, the accounting and file scopes
    # it was granted; the PAT never stands in for it.

    def test_export_login_refreshes_its_own_token(self):
        import oauth

        with mock.patch.object(oauth, "read_refresh_token", return_value="refresh-export") as read, \
                mock.patch.object(oauth, "refresh_access_token", return_value="access-export") as refresh:
            self.assertEqual(client.resolve_token(export_scope=True), ("access-export", "oauth-export"))
        read.assert_called_once_with(oauth.EXPORT_KEYCHAIN_ACCOUNT)
        refresh.assert_called_once_with(export_scope=True)

    def test_export_login_never_falls_back_to_the_pat(self):
        import oauth

        with mock.patch.object(oauth, "read_refresh_token", return_value=None), \
                mock.patch.dict(os.environ, {"BEXIO_TOKEN": "pat-value-not-real"}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                client.resolve_token(export_scope=True)
        self.assertIn("--export-scope", str(ctx.exception))
        self.assertNotIn("pat-value-not-real", str(ctx.exception))


class GetOnlyTest(unittest.TestCase):
    def test_client_has_no_method_that_writes(self):
        public = {name for name in dir(Client) if not name.startswith("_")}
        self.assertEqual(public, {"get", "paginate", "paginate_pages"})

    def test_every_request_is_a_get_even_with_params_and_raw(self):
        c = _client()
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=ok) as opened:
            c.get("/x", params={"a": 1})
            c.get("/y", raw=True)
            list(c.paginate("/z"))
        methods = {call.args[0].get_method() for call in opened.call_args_list}
        self.assertEqual(methods, {"GET"})


if __name__ == "__main__":
    unittest.main()
