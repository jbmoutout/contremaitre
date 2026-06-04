"""harness.py — HarnessContracts: filename and output patterns from initial_prompt.md.

Encapsulates the regex patterns the host-owned initial_prompt.md defines, so
format/contract changes concentrate in one module instead of scattering across
flow_use.py, extract.py, etc. All methods are pure; no I/O.
"""

from __future__ import annotations

import re


class HarnessContracts:
    """Encapsulates harness filename patterns and heuristic classifiers.

    Defaults match the current initial_prompt.md contract.
    Construction accepts optional overrides for pattern strings, enabling
    tests or future prompt versions to change contracts without forking
    metric or extraction code.
    """

    def __init__(
        self,
        settled_pattern: str = "SETTLED_DESIGN",
        impl_complete_pattern: str = "IMPLEMENTATION_COMPLETE",
        diff_pattern: str = r"review_diff_round|(?:^|[/\\])diff\.patch$",
        contremaitre_dir_pattern: str = r"[/\\]?\.contremaitre[/\\]",
        test_cmd_pattern: str = (
            r"\bunittest\b|\bpytest\b|\btsc\b|npm\s+test|make\s+test|\bmypy\b|\bjest\b|\bvitest\b"
        ),
        runtime_install_pattern: str = r"apt-?get\s+install|pip\s+install\b|npm\s+install\b",
        test_fail_pattern: str = r"\bFAILED\b|\berror:\s|\bfailed\b",
        zero_tests_pattern: str = r"0 passed|no tests ran|collected 0 items|Ran 0 tests",
    ):
        self._settled_re = re.compile(settled_pattern, re.IGNORECASE)
        self._impl_complete_re = re.compile(impl_complete_pattern)
        self._diff_re = re.compile(diff_pattern, re.IGNORECASE)
        self._contremaitre_re = re.compile(contremaitre_dir_pattern)
        self._test_cmd_re = re.compile(test_cmd_pattern)
        self._runtime_install_re = re.compile(runtime_install_pattern)
        self._test_fail_re = re.compile(test_fail_pattern, re.IGNORECASE)
        self._zero_tests_re = re.compile(zero_tests_pattern, re.IGNORECASE)

    def is_settled(self, fp: str) -> bool:
        """True if a file path targets the SETTLED_DESIGN.md contract."""
        return bool(self._settled_re.search(str(fp)))

    def is_impl_complete(self, fp: str) -> bool:
        """True if a file path targets the IMPLEMENTATION_COMPLETE contract."""
        return bool(self._impl_complete_re.search(str(fp)))

    def is_diff_path(self, fp: str) -> bool:
        """True if a file path references review or diff.patch."""
        return bool(self._diff_re.search(str(fp)))

    def is_contremaitre_dir(self, fp: str) -> bool:
        """True if a file path is inside .contremaitre/."""
        return bool(self._contremaitre_re.search(str(fp)))

    def is_test_command(self, cmd: str) -> bool:
        """True if a bash command looks like running a test runner."""
        return bool(self._test_cmd_re.search(str(cmd)))

    def is_runtime_install(self, cmd: str) -> bool:
        """True if a command looks like installing a runtime (container gap)."""
        return bool(self._runtime_install_re.search(str(cmd)))

    def test_output_suggests_fail(self, output: str) -> bool:
        """True if test output contains heuristic failure markers."""
        return bool(self._test_fail_re.search(str(output)))

    def test_output_suggests_no_tests(self, output: str) -> bool:
        """True if test output suggests zero tests ran."""
        return bool(self._zero_tests_re.search(str(output)))
