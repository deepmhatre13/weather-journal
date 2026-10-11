"""Regression tests for CLI exit codes (issue #20).

Failed commands must propagate a nonzero status through the real CLI entry
point, while successful commands keep exiting with 0. No network requests are
made; the weather API is always mocked.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from weather_journal.cli import main
from weather_journal.fetch import _cache
from weather_journal.store import save_entry


class TestCliExitCodes(unittest.TestCase):
    """In-process checks that each command returns the right status."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "weather.db"
        self.cache_path = Path(self.temp_dir.name) / "weather-cache.db"
        _cache.clear()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _cli(self, *args):
        """Run the real CLI entry point in-process and capture both streams."""
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--db", str(self.db_path), *args])
        return status, stdout.getvalue(), stderr.getvalue()

    def _save_springfield(self, provider_id, temp):
        save_entry(
            "Springfield", temp, 50, "clear sky", db_path=self.db_path,
            location={
                "provider": "openweathermap",
                "provider_id": provider_id,
                "requested_name": "Springfield",
                "country": "US",
                "latitude": 39.8 if provider_id == 4250542 else 37.2,
                "longitude": -90.0,
            },
        )

    @patch("weather_journal.fetch.requests.get")
    def test_successful_log_exits_zero_and_still_logs(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": 2643743,
            "name": "London",
            "coord": {"lat": 51.5074, "lon": -0.1278},
            "sys": {"country": "GB"},
            "main": {"temp": 15.6, "humidity": 80},
            "weather": [{"description": "light rain"}],
        }
        mock_get.return_value = mock_resp

        env = os.environ.copy()
        env["WEATHER_JOURNAL_CACHE"] = str(self.cache_path)
        with patch.dict("os.environ", env, clear=True):
            status, out, err = self._cli("log", "London", "--key", "fake-key")

        self.assertEqual(status, 0)
        self.assertEqual(err, "")
        self.assertIn("Logged: London - 15.6°C", out)
        mock_get.assert_called_once()

    @patch("weather_journal.fetch.requests.get")
    def test_api_failure_during_log_exits_nonzero(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"cod": "404", "message": "city not found"}
        mock_get.return_value = mock_resp

        env = os.environ.copy()
        env["WEATHER_JOURNAL_CACHE"] = str(self.cache_path)
        with patch.dict("os.environ", env, clear=True):
            status, out, err = self._cli("log", "NoSuchCity123", "--key", "fake-key")

        self.assertNotEqual(status, 0)
        self.assertIn("Error fetching weather: City 'NoSuchCity123' not found.", err)
        self.assertEqual(err.count("Error fetching weather"), 1)
        self.assertNotIn("Error fetching weather", out)

    def test_ambiguous_location_in_stats_exits_nonzero(self):
        self._save_springfield(4250542, 10.0)
        self._save_springfield(4409896, 20.0)

        status, out, err = self._cli("stats", "Springfield")

        self.assertNotEqual(status, 0)
        self.assertIn("Location 'Springfield' is ambiguous.", err)
        self.assertEqual(err.count("ambiguous"), 1)
        self.assertEqual(out, "")

    def test_ambiguous_location_in_history_exits_nonzero(self):
        self._save_springfield(4250542, 10.0)
        self._save_springfield(4409896, 20.0)

        status, out, err = self._cli("history", "Springfield")

        self.assertNotEqual(status, 0)
        self.assertIn("Location 'Springfield' is ambiguous.", err)
        self.assertEqual(err.count("ambiguous"), 1)
        self.assertEqual(out, "")

    def test_invalid_arguments_exit_two(self):
        with self.assertRaises(SystemExit) as context:
            main(["--db", str(self.db_path), "add", "London", "not-a-number", "50", "sun"])
        self.assertEqual(context.exception.code, 2)

    def test_no_command_prints_help_and_exits_zero(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            status = main(["--db", str(self.db_path)])
        self.assertEqual(status, 0)
        self.assertIn("usage:", stdout.getvalue())

    def test_manual_add_history_and_stats_still_succeed(self):
        status, out, err = self._cli("add", "London", "20.5", "70", "clear sky")
        self.assertEqual(status, 0)
        self.assertIn("Logged manually: London - 20.5°C", out)
        self.assertEqual(err, "")

        status, out, err = self._cli("history", "London")
        self.assertEqual(status, 0)
        self.assertIn("Weather History for London", out)

        status, out, err = self._cli("stats", "London")
        self.assertEqual(status, 0)
        self.assertIn("Average Temperature: 20.5°C", out)

    def test_unexpected_errors_are_not_swallowed(self):
        with patch("weather_journal.cli.get_current", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                main(["--db", str(self.db_path), "log", "London", "--key", "fake-key"])
class TestCliProcessExitCodes(unittest.TestCase):
    """The same failures, verified against real OS process exit codes."""

    SEED_SCRIPT = (
        "import sys\n"
        "from weather_journal.store import save_entry\n"
        "db = sys.argv[1]\n"
        "save_entry('Springfield', 10, 50, 'sun', db_path=db, location={'provider': 'openweathermap', 'provider_id': 4250542, 'requested_name': 'Springfield', 'country': 'US', 'latitude': 39.8, 'longitude': -90.0})\n"
        "save_entry('Springfield', 20, 50, 'rain', db_path=db, location={'provider': 'openweathermap', 'provider_id': 4409896, 'requested_name': 'Springfield', 'country': 'US', 'latitude': 37.2, 'longitude': -93.0})\n"
    )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "weather.db"
        self.cache_path = Path(self.temp_dir.name) / "weather-cache.db"
        _cache.clear()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, *args, script=None):
        """Run the CLI in a real child process and return the completed run."""
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["WEATHER_JOURNAL_CACHE"] = str(self.cache_path)
        repo_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [repo_root, env.get("PYTHONPATH", "")]))

        if script is not None:
            cmd = [sys.executable, "-c", script, str(self.db_path)]
        else:
            cmd = [sys.executable, "-m", "weather_journal", "--db", str(self.db_path), *args]

        return subprocess.run(
            cmd, cwd=repo_root, env=env, capture_output=True,
            text=True, encoding="utf-8", timeout=60,
        )

    def _seed_ambiguous_location(self):
        result = self._run(script=self.SEED_SCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_successful_command_exits_zero(self):
        result = self._run("add", "London", "20.5", "70", "clear sky")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Logged manually: London", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_process_ambiguous_stats_exits_nonzero(self):
        self._seed_ambiguous_location()

        result = self._run("stats", "Springfield")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Location 'Springfield' is ambiguous.", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_process_ambiguous_history_exits_nonzero(self):
        self._seed_ambiguous_location()

        result = self._run("history", "Springfield")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Location 'Springfield' is ambiguous.", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
