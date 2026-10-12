"""Unit tests for conductor/agent_models.py — the agent `model:` config lint."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import agent_models  # noqa: E402


class StripJsoncTest(unittest.TestCase):
    def test_strips_comments_but_keeps_urls(self):
        src = '{\n  // a line comment\n  "url": "https://x/y",\n  /* block */\n}'
        out = agent_models._strip_jsonc(src)
        self.assertIn("https://x/y", out)
        self.assertEqual(json.loads(out), {"url": "https://x/y"})

    def test_removes_trailing_commas(self):
        self.assertEqual(json.loads(agent_models._strip_jsonc('{"a": [1, 2, ], }')), {"a": [1, 2]})


class ProvidersTest(unittest.TestCase):
    def test_load_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "opencode.jsonc"
            cfg.write_text('{ "provider": { "deepseek": { "models": { "deepseek-v4-pro": {} } } } }')
            self.assertEqual(agent_models.load_providers(cfg), {"deepseek": {"deepseek-v4-pro"}})

    def test_load_providers_missing_file(self):
        self.assertEqual(agent_models.load_providers(Path("/no/such/file")), {})


class ReportTest(unittest.TestCase):
    def _stack(self, tmp):
        root = Path(tmp)
        (root / "agents" / "definitions").mkdir(parents=True)
        (root / "harness" / "configs").mkdir(parents=True)
        (root / "harness" / "configs" / "opencode.jsonc").write_text(
            json.dumps({"provider": {"deepseek": {"models": {"deepseek-v4-pro": {}}}}})
        )
        defs = root / "agents" / "definitions"
        (defs / "good.md").write_text("---\nmodel: deepseek/deepseek-v4-pro\n---\n")
        (defs / "badprov.md").write_text("---\nmodel: openrouter/deepseek/deepseek-v4-pro\n---\n")
        (defs / "badmodel.md").write_text("---\nmodel: deepseek/nope\n---\n")
        (defs / "builtin.md").write_text("---\nmodel: opencode/deepseek-v4-flash\n---\n")
        (defs / "nomodel.md").write_text("---\ndescription: no model\n---\n")
        return root

    def test_report_flags_only_bad_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._stack(tmp)
            with mock.patch.object(agent_models, "stack_root", return_value=root):
                failures = agent_models.report()
        joined = "\n".join(failures)
        self.assertIn("badprov.md", joined)  # provider not configured
        self.assertIn("badmodel.md", joined)  # model not in the provider
        self.assertNotIn("good.md", joined)
        self.assertNotIn("builtin.md", joined)  # opencode is the built-in provider
        self.assertNotIn("nomodel.md", joined)  # no model -> default, nothing to check

    def test_report_noop_without_stack(self):
        with mock.patch.object(agent_models, "stack_root", return_value=Path("/no/such/stack")):
            self.assertEqual(agent_models.report(), [])


if __name__ == "__main__":
    unittest.main()
