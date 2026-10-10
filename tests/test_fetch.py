import unittest
from unittest.mock import patch, MagicMock
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import requests

from weather_journal.fetch import get_current, _cache

class TestFetch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temp_dir.name) / "weather-cache.db"
        _cache.clear()

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("weather_journal.fetch.requests.get")
    def test_get_current_success(self, mock_get):
        # Mock a successful API response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": 2643743,
            "name": "London",
            "coord": {"lat": 51.5074, "lon": -0.1278},
            "sys": {"country": "GB"},
            "main": {"temp": 15.6, "humidity": 80},
            "weather": [{"description": "light rain"}]
        }
        mock_get.return_value = mock_resp

        result = get_current("London", api_key="fake-key", cache_path=self.cache_path)

        self.assertEqual(result["city"], "London")
        self.assertEqual(result["temp"], 15.6)
        self.assertEqual(result["humidity"], 80)
        self.assertEqual(result["condition"], "light rain")
        self.assertEqual(result["location"]["provider_id"], 2643743)
        self.assertEqual(result["location"]["requested_name"], "London")
        mock_get.assert_called_once()

    @patch("weather_journal.fetch.requests.get")
    def test_caching(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": 2988507,
            "name": "Paris",
            "coord": {"lat": 48.8534, "lon": 2.3488},
            "sys": {"country": "FR"},
            "main": {"temp": 20.0, "humidity": 50},
            "weather": [{"description": "clear sky"}]
        }
        mock_get.return_value = mock_resp

        # First call hits the API
        res1 = get_current("Paris", api_key="fake-key", cache_path=self.cache_path)
        self.assertEqual(mock_get.call_count, 1)

        # Second call within 10 minutes should hit the cache
        res2 = get_current("Paris", api_key="fake-key", cache_path=self.cache_path)
        self.assertEqual(mock_get.call_count, 1)  # Still 1
        self.assertEqual(res1, res2)

    @patch("weather_journal.fetch.time.sleep")
    @patch("weather_journal.fetch.requests.get")
    def test_network_retry_and_failure(self, mock_get, mock_sleep):
        # Make the request raise a ConnectionError on all attempts
        mock_get.side_effect = requests.ConnectionError("Connection timeout")

        with self.assertRaises(ValueError) as context:
            get_current("Berlin", api_key="fake-key", cache_path=self.cache_path)

        self.assertIn("Network error: Connection timeout", str(context.exception))
        # It should have retried 3 times (initial + 2 retries)
        self.assertEqual(mock_get.call_count, 3)
        # It should have slept twice
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_any_call(1) # 2**0
        mock_sleep.assert_any_call(2) # 2**1

    def test_cache_survives_separate_python_processes(self):
        env = os.environ.copy()
        env["WEATHER_JOURNAL_CACHE"] = str(self.cache_path)
        repo_root = Path(__file__).resolve().parent.parent
        populate = """
from unittest.mock import MagicMock, patch
from weather_journal.fetch import get_current
response = MagicMock(status_code=200)
response.json.return_value = {
    "name": "Mumbai",
    "main": {"temp": 29.0, "humidity": 75},
    "weather": [{"description": "haze"}],
}
with patch("weather_journal.fetch.requests.get", return_value=response):
    get_current("Mumbai", api_key="fake-key")
"""
        consume = """
from unittest.mock import patch
from weather_journal.fetch import get_current
with patch("weather_journal.fetch.requests.get", side_effect=AssertionError("API called")):
    print(get_current("Mumbai")["city"])
"""

        first = subprocess.run(
            [sys.executable, "-c", populate], cwd=repo_root, env=env,
            text=True, capture_output=True,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        second = subprocess.run(
            [sys.executable, "-c", consume], cwd=repo_root, env=env,
            text=True, capture_output=True,
        )

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), "Mumbai")

    @patch("weather_journal.fetch.requests.get")
    def test_expired_persistent_entry_is_refetched(self, mock_get):
        stale_response = MagicMock(status_code=200)
        stale_response.json.return_value = {
            "name": "Paris",
            "main": {"temp": 10.0, "humidity": 50},
            "weather": [{"description": "cloudy"}],
        }
        fresh_response = MagicMock(status_code=200)
        fresh_response.json.return_value = {
            "name": "Paris",
            "main": {"temp": 20.0, "humidity": 45},
            "weather": [{"description": "clear sky"}],
        }
        mock_get.side_effect = [stale_response, fresh_response]

        get_current("Paris", api_key="fake-key", cache_path=self.cache_path)
        _cache.clear()
        conn = sqlite3.connect(str(self.cache_path))
        try:
            conn.execute("UPDATE weather_cache SET fetched_at = 0")
            conn.commit()
        finally:
            conn.close()

        result = get_current("Paris", api_key="fake-key", cache_path=self.cache_path)

        self.assertEqual(result["temp"], 20.0)
        self.assertEqual(mock_get.call_count, 2)

    @patch("weather_journal.fetch.requests.get")
    def test_units_metric_requested(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "name": "Tokyo",
            "main": {"temp": 22.4, "humidity": 65},
            "weather": [{"description": "clear sky"}]
        }
        mock_get.return_value = mock_resp

        get_current("Tokyo", api_key="test-key", cache_path=self.cache_path)

        # Verify units=metric is explicitly requested for Celsius temperatures
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"].get("units"), "metric")
        self.assertEqual(kwargs["params"].get("q"), "Tokyo")
        self.assertEqual(kwargs["params"].get("appid"), "test-key")

    @patch("weather_journal.fetch.requests.get")
    def test_success_response_without_location_id_does_not_crash(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "name": "Tokyo",
            "main": {"temp": 22.4, "humidity": 65},
            "weather": [{"description": "clear sky"}],
        }
        mock_get.return_value = mock_resp

        result = get_current("Tokyo", api_key="test-key", cache_path=self.cache_path)

        self.assertIsNone(result["location"]["provider_id"])

    @patch("weather_journal.fetch.requests.get")
    def test_invalid_api_key_401(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"cod": 401, "message": "Invalid API key."}
        mock_get.return_value = mock_resp

        with self.assertRaises(ValueError) as ctx:
            get_current("Mumbai", api_key="bad-key", cache_path=self.cache_path)

        self.assertEqual(str(ctx.exception), "Invalid API key.")

    @patch("weather_journal.fetch.requests.get")
    def test_city_not_found_404(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"cod": "404", "message": "city not found"}
        mock_get.return_value = mock_resp

        with self.assertRaises(ValueError) as ctx:
            get_current("NonExistentCity123", api_key="valid-key", cache_path=self.cache_path)

        self.assertEqual(str(ctx.exception), "City 'NonExistentCity123' not found.")

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_api_key(self):
        with self.assertRaises(ValueError) as ctx:
            get_current("Mumbai", cache_path=self.cache_path)
        self.assertIn("Missing API key", str(ctx.exception))

    @patch("weather_journal.fetch.requests.get")
    def test_non_200_api_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"message": "Internal Server Error"}
        mock_get.return_value = mock_resp

        with self.assertRaises(ValueError) as ctx:
            get_current("Berlin", api_key="test-key", cache_path=self.cache_path)

        self.assertIn("API Error: Internal Server Error", str(ctx.exception))

    @patch("weather_journal.cli.get_current")
    def test_cmd_log_error_output(self, mock_get_current):
        from io import StringIO
        from types import SimpleNamespace
        from contextlib import redirect_stdout
        from weather_journal.cli import cmd_log

        mock_get_current.side_effect = ValueError("City 'FakeCity' not found.")

        out = StringIO()
        with redirect_stdout(out):
            cmd_log(SimpleNamespace(city="FakeCity", key="fake-key", db="test.db"))

        self.assertEqual(out.getvalue().strip(), "Error fetching weather: City 'FakeCity' not found.")

    @staticmethod
    def _weather_response(temp, name="London"):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "id": 2643743,
            "name": name,
            "coord": {"lat": 51.5074, "lon": -0.1278},
            "sys": {"country": "GB"},
            "main": {"temp": temp, "humidity": 70},
            "weather": [{"description": "clear sky"}],
        }
        return response

    @patch("weather_journal.fetch.requests.get")
    def test_same_city_isolated_across_cache_paths(self, mock_get):
        # Two distinct SQLite cache paths must not share in-memory entries.
        other_dir = tempfile.TemporaryDirectory()
        self.addCleanup(other_dir.cleanup)
        other_path = Path(other_dir.name) / "other-weather.db"

        mock_get.side_effect = [self._weather_response(11.1), self._weather_response(22.2)]

        first = get_current("London", api_key="fake-key", cache_path=self.cache_path)
        second = get_current("London", api_key="fake-key", cache_path=other_path)

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(first["temp"], 11.1)
        self.assertEqual(second["temp"], 22.2)
        self.assertTrue(other_path.exists())

        # Each cache path keeps its own persisted row for the same city key.
        for path, expected in ((self.cache_path, 11.1), (other_path, 22.2)):
            conn = sqlite3.connect(str(path))
            try:
                row = conn.execute(
                    "SELECT payload FROM weather_cache WHERE city_key = ?",
                    ("london",),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)
            self.assertIn(f'"temp":{expected}', row[0])

    @patch("weather_journal.fetch.requests.get")
    def test_repeated_requests_reuse_memory_cache_per_path(self, mock_get):
        from weather_journal import fetch

        mock_get.return_value = self._weather_response(18.5)

        results = [get_current("London", api_key="fake-key", cache_path=self.cache_path)
                   for _ in range(3)]

        self.assertEqual(mock_get.call_count, 1)
        self.assertTrue(all(r == results[0] for r in results))

        # The entry is served from the process-local cache first, before SQLite.
        key = fetch._memory_key("london", self.cache_path)
        self.assertIn(key, _cache)
        # Scoped keys: a different path must not collide with this entry.
        self.assertNotIn(fetch._memory_key("london", Path(self.temp_dir.name) / "x.db"), _cache)

    @patch("weather_journal.fetch.requests.get")
    def test_expired_memory_entry_is_refreshed(self, mock_get):
        from weather_journal import fetch

        mock_get.side_effect = [self._weather_response(5.0), self._weather_response(6.0)]

        get_current("London", api_key="fake-key", cache_path=self.cache_path)
        key = fetch._memory_key("london", self.cache_path)
        fetched_at, data = _cache[key]
        # Force both the memory entry and the persistent row past the TTL so the
        # refresh must go all the way back to the API.
        _cache[key] = (fetched_at - fetch.CACHE_TTL_SECONDS - 1, data)
        conn = sqlite3.connect(str(self.cache_path))
        try:
            conn.execute("UPDATE weather_cache SET fetched_at = 0")
            conn.commit()
        finally:
            conn.close()

        result = get_current("London", api_key="fake-key", cache_path=self.cache_path)

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(result["temp"], 6.0)
        self.assertEqual(_cache[key][1]["temp"], 6.0)

    @patch.dict("os.environ", {}, clear=True)
    @patch("weather_journal.fetch.requests.get")
    def test_env_configured_cache_path_is_isolated_in_memory(self, mock_get):
        from weather_journal import fetch

        env_dir = tempfile.TemporaryDirectory()
        self.addCleanup(env_dir.cleanup)
        env_path = Path(env_dir.name) / "env-weather.db"

        with patch.dict("os.environ", {"WEATHER_JOURNAL_CACHE": str(env_path)}):
            mock_get.side_effect = [self._weather_response(30.0), self._weather_response(31.0)]
            get_current("London", api_key="fake-key")
            # Same city, explicit distinct path: must not reuse the env entry.
            direct = get_current("London", api_key="fake-key", cache_path=self.cache_path)

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(direct["temp"], 31.0)
        self.assertIn(fetch._memory_key("london", env_path), _cache)
        self.assertIn(fetch._memory_key("london", self.cache_path), _cache)

    @patch("weather_journal.fetch.requests.get")
    def test_persistent_corrupt_payload_is_dropped_and_refetched(self, mock_get):
        mock_get.return_value = self._weather_response(12.0)
        get_current("London", api_key="fake-key", cache_path=self.cache_path)
        _cache.clear()

        conn = sqlite3.connect(str(self.cache_path))
        try:
            conn.execute("UPDATE weather_cache SET payload = ?", ("not-json",))
            conn.commit()
        finally:
            conn.close()

        mock_get.return_value = self._weather_response(13.0)
        result = get_current("London", api_key="fake-key", cache_path=self.cache_path)

        self.assertEqual(result["temp"], 13.0)
        self.assertEqual(mock_get.call_count, 2)

        conn = sqlite3.connect(str(self.cache_path))
        try:
            row = conn.execute(
                "SELECT payload FROM weather_cache WHERE city_key = ?", ("london",)
            ).fetchone()
        finally:
            conn.close()
        self.assertIn('"temp":13.0', row[0])

    @patch("weather_journal.fetch.requests.get")
    def test_persistent_future_timestamp_is_refetched(self, mock_get):
        mock_get.side_effect = [self._weather_response(14.0), self._weather_response(15.0)]
        get_current("London", api_key="fake-key", cache_path=self.cache_path)
        _cache.clear()

        conn = sqlite3.connect(str(self.cache_path))
        try:
            conn.execute("UPDATE weather_cache SET fetched_at = ?", (time.time() + 10_000,))
            conn.commit()
        finally:
            conn.close()

        result = get_current("London", api_key="fake-key", cache_path=self.cache_path)

        self.assertEqual(result["temp"], 15.0)
        self.assertEqual(mock_get.call_count, 2)

if __name__ == "__main__":
    unittest.main()
