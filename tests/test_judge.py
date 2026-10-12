"""Unit tests for conductor/judge.py — Clef parsing, backend preference, fallback."""

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import judge  # noqa: E402


class ParseYesNoTest(unittest.TestCase):
    def test_explicit_verdict_lines(self):
        self.assertTrue(judge.parse_yes_no("blah\nVERDICT: YES"))
        self.assertFalse(judge.parse_yes_no("blah\nVERDICT: NO"))

    def test_gist_fallback(self):
        self.assertTrue(judge.parse_yes_no("YES overall"))
        self.assertFalse(judge.parse_yes_no("NO"))
        self.assertFalse(judge.parse_yes_no(""))


class ProbTest(unittest.TestCase):
    def test_float(self):
        self.assertEqual(judge._prob(0.7), 0.7)

    def test_dict_shapes(self):
        # The real Workers AI `noul` answer shape.
        self.assertEqual(judge._prob({"type": "noul", "noul": 0.9942}), 0.9942)
        self.assertEqual(judge._prob({"probability": 0.9}), 0.9)
        self.assertEqual(judge._prob({"confidence": 0.25}), 0.25)

    def test_bool(self):
        self.assertEqual(judge._prob(True), 1.0)

    def test_unrecognized(self):
        with self.assertRaises(judge.ClefError):
            judge._prob("nope")


class AnswersTest(unittest.TestCase):
    def test_wrapped(self):
        self.assertEqual(judge._answers({"result": {"answers": {"x": 1}}}), {"x": 1})

    def test_direct(self):
        self.assertEqual(judge._answers({"answers": {"x": 1}}), {"x": 1})

    def test_missing(self):
        with self.assertRaises(judge.ClefError):
            judge._answers({"usage": {}})


class AskTest(unittest.TestCase):
    def setUp(self):
        judge.reset()

    def tearDown(self):
        judge.reset()

    def test_builds_request_and_parses(self):
        captured: dict = {}

        def fake_post(url, token, body, timeout):
            captured.update(url=url, token=token, payload=json.loads(body))
            return {"result": {"answers": {"satisfied": 0.9}}}

        with (
            mock.patch.dict(
                os.environ,
                {"CONDUCTOR_CLEF_ENABLED": "1", "CONDUCTOR_CLEF_TOKEN": "tok", "CONDUCTOR_CLEF_ACCOUNT": "acct"},
                clear=False,
            ),
            mock.patch.object(judge, "_vault_token", return_value=None),
            mock.patch.object(judge, "_post", side_effect=fake_post),
        ):
            answers = judge.ask("hello", {"satisfied": {"type": "noul", "instructions": "?"}})

        self.assertEqual(answers, {"satisfied": 0.9})
        self.assertTrue(captured["url"].endswith("/ai/run/@cf/cloudflare/clef"))
        self.assertEqual(captured["token"], "tok")
        self.assertEqual(captured["payload"]["model"], "clef")
        self.assertEqual(captured["payload"]["state"], "hello")


class AuthFailureTest(unittest.TestCase):
    def setUp(self):
        judge.reset()

    def tearDown(self):
        judge.reset()

    def test_http_error_disables_clef(self):
        body = io.BytesIO(b'{"success":false}')
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, body)
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {"CONDUCTOR_CLEF_ENABLED": "1", "CONDUCTOR_CLEF_TOKEN": "tok", "CONDUCTOR_CLEF_ACCOUNT": "acct"},
                    clear=False,
                ),
                mock.patch.object(judge, "_vault_token", return_value=None),
                mock.patch("urllib.request.urlopen", side_effect=err),
            ):
                with self.assertRaises(judge.ClefError):
                    judge.ask("s", {"q": {"type": "noul", "instructions": "?"}})
                self.assertFalse(judge.clef_available())
        finally:
            err.close()
            body.close()


class VerdictTest(unittest.TestCase):
    def setUp(self):
        judge.reset()

    def tearDown(self):
        judge.reset()

    def _env(self):
        return mock.patch.dict(
            os.environ,
            {"CONDUCTOR_CLEF_ENABLED": "1", "CONDUCTOR_CLEF_TOKEN": "tok", "CONDUCTOR_CLEF_ACCOUNT": "acct"},
            clear=False,
        )

    def test_prefers_clef_true(self):
        with (
            self._env(),
            mock.patch.object(judge, "_vault_token", return_value=None),
            mock.patch.object(judge, "_post", return_value={"result": {"answers": {"satisfied": 0.8}}}),
            mock.patch.object(judge, "ollama_available", return_value=False),
        ):
            self.assertTrue(judge.verdict("rubric", "subject"))

    def test_clef_false(self):
        with (
            self._env(),
            mock.patch.object(judge, "_vault_token", return_value=None),
            mock.patch.object(judge, "_post", return_value={"result": {"answers": {"satisfied": 0.2}}}),
        ):
            self.assertFalse(judge.verdict("rubric", "subject"))

    def test_falls_back_to_ollama(self):
        with (
            self._env(),
            mock.patch.object(judge, "_vault_token", return_value=None),
            mock.patch.object(judge, "_post", side_effect=judge.ClefError("HTTP 403: no permission")),
            mock.patch.object(judge, "ollama_available", return_value=True),
            mock.patch.object(judge.local, "generate", return_value="VERDICT: YES"),
        ):
            self.assertTrue(judge.verdict("rubric", "subject"))

    def test_no_backend_raises(self):
        with (
            mock.patch.dict(os.environ, {"CONDUCTOR_CLEF_TOKEN": "", "CLOUDFLARE_API_TOKEN": ""}, clear=False),
            mock.patch.object(judge, "_vault_token", return_value=None),
            mock.patch.object(judge, "ollama_available", return_value=False),
        ):
            self.assertFalse(judge.available())
            with self.assertRaises(judge.NoJudge):
                judge.verdict("rubric", "subject")


class OptInTest(unittest.TestCase):
    """Clef spends money, so it is off unless explicitly enabled."""

    def setUp(self):
        judge.reset()

    def tearDown(self):
        judge.reset()

    def test_disabled_by_default_even_with_a_token(self):
        with (
            mock.patch.dict(os.environ, {"CONDUCTOR_CLEF_ENABLED": "", "CONDUCTOR_CLEF_TOKEN": "tok"}, clear=False),
            mock.patch.object(judge, "_vault_token", return_value=None),
        ):
            self.assertFalse(judge.enabled())
            self.assertFalse(judge.clef_available())
            with self.assertRaises(judge.ClefError):
                judge.ask("s", {"q": {"type": "noul"}})


if __name__ == "__main__":
    unittest.main()
