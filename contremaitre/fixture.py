"""Local fixture repository helpers for fake-mode smoke tests."""

from __future__ import annotations

import shutil
from pathlib import Path

from .git_utils import GitRepo


def init_fixture(path: Path, *, overwrite: bool = False) -> Path:
    """Create a tiny git repo with a `main` branch.

    The fake agent will add the actual implementation and tests during the
    run. The fixture only supplies a clean base commit.
    """

    if path.exists():
        if not overwrite:
            raise FileExistsError(f"fixture path already exists: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Contremaitre fixture\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git = GitRepo(path)
    git.run("init", "-b", "main")
    git.run("add", ".")
    git.run("commit", "-m", "Initial fixture")
    return path

