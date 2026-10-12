"""Unit tests for conductor/local.py — the offline lane status (no network touched)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import local  # noqa: E402


class LocalTest(unittest.TestCase):
    def test_status_shape_when_down(self):
        with (
            mock.patch.object(local, "_ollama", return_value=None),
            mock.patch.object(local, "server_up", return_value=False),
            mock.patch.object(local, "models", return_value=[]),
        ):
            s = local.status()
        self.assertIn("local models", s)
        self.assertIn("runtime", s)
        self.assertIn("not ready", s)

    def test_judge_ready_reported(self):
        with (
            mock.patch.object(local, "_ollama", return_value="/opt/homebrew/bin/ollama"),
            mock.patch.object(local, "server_up", return_value=True),
            mock.patch.object(local, "models", return_value=["qwen2.5:3b"]),
        ):
            self.assertIn("ready", local.status())

    def test_models_empty_on_error(self):
        with mock.patch.object(local, "_get", side_effect=OSError("down")):
            self.assertEqual(local.models(), [])


if __name__ == "__main__":
    unittest.main()
