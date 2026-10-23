import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cloud_common import require_cloud_success
from import_ev_charger_telemetry import fetch_day


class RequireCloudSuccessTests(unittest.TestCase):
    def response(self, status_code: int) -> requests.Response:
        response = requests.Response()
        response.status_code = status_code
        return response

    def test_allows_success(self) -> None:
        require_cloud_success(self.response(200), "test_operation")

    def test_raises_refresh_hint_for_unauthorized(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "status=401 authentication_required=true action=refresh_oauth_token_and_retry",
        ):
            require_cloud_success(self.response(401), "test_operation")

    def test_raises_for_other_failure(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "status=500"):
            require_cloud_success(self.response(500), "test_operation")

    def test_ev_import_fetch_stops_on_unauthorized(self) -> None:
        response = self.response(401)
        response.json = Mock(return_value={})
        with patch("import_ev_charger_telemetry.cloud_get", return_value=response) as cloud_get:
            with self.assertRaisesRegex(RuntimeError, "status=401"):
                fetch_day("system", "serial", date(2026, 5, 1), ZoneInfo("UTC"))
        cloud_get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
