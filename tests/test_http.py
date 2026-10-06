import email.utils
import io
import time
import unittest
import urllib.error
from unittest import mock

from app import http
from app.http import HttpError


def http_error(status, headers=None):
    return urllib.error.HTTPError("https://reg.example/v2/", status, "x", headers or {}, io.BytesIO(b""))


class Ok:
    status, headers = 200, {}

    def read(self):
        return b'{"ok": true}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RetryOn429(unittest.TestCase):
    def run_with(self, *outcomes):
        """request() against a server that answers with `outcomes` in turn; returns (result, waits)."""
        waits = []
        seq = iter(outcomes)

        def urlopen(req, timeout=None, context=None):
            o = next(seq)
            if isinstance(o, Exception):
                raise o
            return o
        with mock.patch.object(http.urllib.request, "urlopen", urlopen), mock.patch.object(http, "_sleep", waits.append):
            try:
                return http.request("https://reg.example/v2/").json(), waits
            except HttpError as e:
                return e.status, waits

    def test_retries_then_succeeds(self):
        self.assertEqual(self.run_with(http_error(429), http_error(429), Ok()), ({"ok": True}, mock.ANY))

    def test_honours_retry_after_seconds_and_date(self):
        _, waits = self.run_with(http_error(429, {"Retry-After": "7"}), Ok())
        self.assertEqual(waits, [7.0])
        soon = email.utils.formatdate(time.time() + 5, usegmt=True)
        _, waits = self.run_with(http_error(429, {"Retry-After": soon}), Ok())
        self.assertTrue(3 <= waits[0] <= 6, waits)

    def test_backs_off_without_retry_after(self):
        _, waits = self.run_with(http_error(429), http_error(429), http_error(429), Ok())
        self.assertEqual(len(waits), 3)
        self.assertTrue(waits[0] < waits[1] < waits[2] <= 5, waits)

    def test_gives_up(self):
        self.assertEqual(self.run_with(*[http_error(429)] * (http.RETRIES + 1)), (429, mock.ANY))
        self.assertEqual(self.run_with(http_error(429, {"Retry-After": "3600"})), (429, []))  # too long to wait

    def test_other_errors_are_not_retried(self):
        for status in (400, 401, 404, 500):
            self.assertEqual(self.run_with(http_error(status)), (status, []))


if __name__ == "__main__":
    unittest.main()
