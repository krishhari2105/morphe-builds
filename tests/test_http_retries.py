import unittest

from morphe_builder.http import HttpClient


class HttpRetryTests(unittest.TestCase):
    def test_rate_limit_response_falls_through_without_automatic_sleep_or_retry(self):
        client = HttpClient()
        self.addCleanup(client.session.close)
        retry = client.session.get_adapter("https://example.org").max_retries
        self.assertFalse(retry.is_retry("GET", 429, has_retry_after=True))

    def test_transient_server_errors_still_retry(self):
        client = HttpClient()
        self.addCleanup(client.session.close)
        retry = client.session.get_adapter("https://example.org").max_retries
        self.assertTrue(retry.is_retry("GET", 503, has_retry_after=True))
        self.assertFalse(retry.is_retry("GET", 403))


if __name__ == "__main__":
    unittest.main()
