"""Route-level tests for dashboard CSRF protection."""

from __future__ import annotations

import html
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from autoapply.web import create_app


TRUSTED_ORIGIN = "http://127.0.0.1:8000"


class _NoopThread:
    """Test-only replacement that prevents a pre-protection import from starting."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def start(self) -> None:
        pass


class CsrfProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_all_post_routes_reject_requests_without_a_loopback_origin(self) -> None:
        """Removing origin checks must make this test fail."""
        requests = (
            ("/settings", {"api_key": "ignored", "csrf_token": "ignored"}),
            ("/jobs/import", {"csrf_token": "ignored"}),
            ("/applications/draft", {"csrf_token": "ignored"}),
        )

        for path, data in requests:
            with self.subTest(path=path):
                with (
                    patch("autoapply.web.save_configuration"),
                    patch(
                        "autoapply.web.threading",
                        SimpleNamespace(Thread=_NoopThread),
                    ),
                ):
                    response = self.client.post(path, data=data, follow_redirects=False)
                self.assertEqual(response.status_code, 403)

    def test_trusted_origin_rejects_an_invalid_csrf_token(self) -> None:
        """Replacing token verification with an unconditional allow must fail."""
        response = self.client.post(
            "/applications/draft",
            data={"csrf_token": "not-the-dashboard-token"},
            headers={"Origin": TRUSTED_ORIGIN},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

    def test_dashboard_token_allows_a_legitimate_post(self) -> None:
        """Removing the rendered token or rejecting valid tokens must fail."""
        dashboard = self.client.get("/")
        token_match = re.search(
            r'<input type="hidden" name="csrf_token" value="([^"]+)">',
            dashboard.text,
        )
        self.assertIsNotNone(token_match)
        token = html.unescape(token_match.group(1))

        response = self.client.post(
            "/applications/draft",
            data={"csrf_token": token},
            headers={"Origin": TRUSTED_ORIGIN},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)

    def test_localhost_origin_is_treated_as_trusted(self) -> None:
        """Loopback aliases must remain accepted when serving locally."""
        dashboard = self.client.get("/")
        token_match = re.search(
            r'<input type="hidden" name="csrf_token" value="([^"]+)">',
            dashboard.text,
        )
        self.assertIsNotNone(token_match)
        token = html.unescape(token_match.group(1))

        response = self.client.post(
            "/applications/draft",
            data={"csrf_token": token},
            headers={"Origin": "http://localhost:8000"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)

    def test_dashboard_renders_a_token_in_each_post_form(self) -> None:
        """Removing a token field from any dashboard form must fail."""
        response = self.client.get("/")
        self.assertEqual(response.text.count('name="csrf_token"'), 3)


if __name__ == "__main__":
    unittest.main()
