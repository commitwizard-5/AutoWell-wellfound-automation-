import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from autoapply.web import create_app


class LandingAndDashboardRouteTests(unittest.TestCase):
    def test_root_serves_showcase_and_dashboard_keeps_working(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            with patch("autoapply.web.ENV_FILE", env_file), patch.dict(os.environ, {}, clear=True):
                with TestClient(create_app()) as client:
                    landing = client.get("/")
                    dashboard = client.get("/dashboard")

        self.assertEqual(landing.status_code, 200)
        self.assertIn("Your next opportunity", landing.text)
        self.assertIn('href="/dashboard"', landing.text)
        self.assertNotIn("name=\"csrf_token\"", landing.text)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Local settings", dashboard.text)
        self.assertIn('name="csrf_token"', dashboard.text)

    def test_dashboard_settings_redirect_stays_on_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            with patch("autoapply.web.ENV_FILE", env_file), patch.dict(os.environ, {}, clear=True):
                with TestClient(create_app()) as client:
                    dashboard = client.get("/dashboard")
                    token = re.search(
                        r'name="csrf_token" value="([^"]+)"',
                        dashboard.text,
                    ).group(1)
                    response = client.post(
                        "/settings",
                        data={"csrf_token": token},
                        follow_redirects=False,
                    )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")
