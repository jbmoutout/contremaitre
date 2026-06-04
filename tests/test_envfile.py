from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contremaitre.envfile import load_dotenv_defaults, load_env_file


class EnvFileTest(unittest.TestCase):
    def test_load_env_file_sets_missing_values_and_preserves_existing_env(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"EXISTING": "shell"}, clear=True),
        ):
            path = Path(tmp) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "# local operator secrets",
                        "OPENROUTER_API_KEY='sk-test'",
                        'MODEL="openrouter/qwen/qwen3.5-9b"',
                        "TRAILING=value # comment",
                        "export EXISTING=file",
                    ]
                ),
                encoding="utf-8",
            )

            load_env_file(path)

            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "sk-test")
            self.assertEqual(os.environ["MODEL"], "openrouter/qwen/qwen3.5-9b")
            self.assertEqual(os.environ["TRAILING"], "value")
            self.assertEqual(os.environ["EXISTING"], "shell")

    def test_load_dotenv_defaults_loads_current_directory_env(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            (root / ".env").write_text("OPENROUTER_API_KEY=from-cwd\n", encoding="utf-8")

            loaded = load_dotenv_defaults(cwd=root)

            self.assertIn((root / ".env").resolve(), loaded)
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "from-cwd")

    def test_export_prefix_is_stripped_for_a_fresh_key(self):
        """The previous suite used `export EXISTING=file` with EXISTING
        pre-populated, so `setdefault` never wrote — meaning the
        export-stripping code path could be deleted and the test would
        still pass. Use a fresh key here to actually exercise it.
        """

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / ".env"
            path.write_text("export FRESH_KEY=stripped\n", encoding="utf-8")

            load_env_file(path)

            self.assertEqual(os.environ["FRESH_KEY"], "stripped")
            # The literal `"export FRESH_KEY"` (with the prefix kept) must
            # NOT appear as a key — that would be the "strip failed silently"
            # regression where the parser fell back to a junk-named key.
            self.assertNotIn("export FRESH_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()
