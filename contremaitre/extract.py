"""Post-run extractor for agent/SIM JSONL streams.

Reads ``raw_export.jsonl`` and ``sim_raw_export.jsonl`` (via the shared
``event_stream`` module) and produces:

  - ``subagents/agent_<NN>_<slug>.md`` — one file per ``task`` tool_use, with
    the subagent's prompt + output + status + subagent_type.
  - ``extracted_files/<host_name>`` — every file the agent wrote via ``write``,
    ``edit``, or ``apply_patch``. Edits accumulate in ``<host_name>.edits.md``.
  - Counts/sizes in the return value for the orchestrator's stats.json.

Called from the orchestrator's ``finally`` so artifacts land even on failure.
The orchestrator owns git/worktree; this module only reads JSONL.
"""

from __future__ import annotations

import re
from typing import Any

from .event_stream import parse_events
from .models import RunPaths


_APPLY_PATCH_HDR = re.compile(
    r"^\*\*\*\s+(Add|Update|Delete)\s+File:\s*(.+?)\s*$",
    re.MULTILINE,
)


def slugify(s: str, maxlen: int = 50) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s.lower()).strip("_")
    return s[:maxlen] or "task"


def parse_apply_patch(patch_text: str):
    """Yield (op, path, body) for each file action in an apply_patch payload."""

    if not patch_text:
        return
    matches = list(_APPLY_PATCH_HDR.finditer(patch_text))
    for i, m in enumerate(matches):
        op = m.group(1)
        path = m.group(2).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(patch_text)
        chunk = patch_text[body_start:body_end]
        chunk = re.sub(r"\n?\*\*\*\s+End Patch\s*$", "", chunk).strip("\n")
        if op == "Add":
            lines = []
            for ln in chunk.split("\n"):
                if ln.startswith("+"):
                    lines.append(ln[1:])
                elif ln.strip() == "":
                    lines.append("")
            yield op, path, "\n".join(lines).rstrip("\n") + "\n"
        else:
            yield op, path, chunk


def _host_name(fp: str) -> str:
    rel = fp.replace("/app/", "").replace("/app", "").lstrip("/")
    return rel.replace("/", "__")


def extract_run_artifacts(paths: RunPaths) -> dict[str, Any]:
    """Extract files + subagents from agent and SIM JSONL streams.

    Returns counts suitable for inclusion in stats.json.
    """

    agent_tool_calls = parse_events(paths.raw_export).tool_calls
    sim_tool_calls = parse_events(paths.sim_raw_export).tool_calls
    tool_calls = agent_tool_calls + sim_tool_calls

    paths.extracted_files_dir.mkdir(parents=True, exist_ok=True)
    paths.subagents_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict[str, Any]] = []
    for tc in tool_calls:
        if tc.tool not in ("write", "edit", "apply_patch"):
            continue
        if tc.tool == "write":
            fp = tc.file_path
            if not fp:
                continue
            content = tc.content or ""
            out = paths.extracted_files_dir / _host_name(fp)
            out.write_text(content, encoding="utf-8")
            written.append(
                {"original_path": fp, "host_file": str(out), "len": len(content), "tool": "write"}
            )
        elif tc.tool == "edit":
            fp = tc.file_path
            if not fp:
                continue
            old_s = tc.old_string or ""
            new_s = tc.new_string or ""
            out = paths.extracted_files_dir / f"{_host_name(fp)}.edits.md"
            block = (
                "\n---\n## edit (oldString -> newString)\n\n"
                f"### old\n```\n{old_s}\n```\n\n"
                f"### new\n```\n{new_s}\n```\n"
            )
            with out.open("a", encoding="utf-8") as f:
                f.write(block)
            written.append(
                {
                    "original_path": fp,
                    "host_file": str(out),
                    "len": len(new_s),
                    "old_len": len(old_s),
                    "tool": "edit",
                }
            )
        else:  # apply_patch
            patch_text = tc.patch_text or ""
            for op, fp, body in parse_apply_patch(patch_text):
                if op == "Add":
                    out = paths.extracted_files_dir / _host_name(fp)
                    out.write_text(body, encoding="utf-8")
                    written.append(
                        {
                            "original_path": fp,
                            "host_file": str(out),
                            "len": len(body),
                            "tool": "apply_patch:Add",
                        }
                    )
                elif op == "Update":
                    out = paths.extracted_files_dir / f"{_host_name(fp)}.edits.md"
                    with out.open("a", encoding="utf-8") as f:
                        f.write(f"\n---\n## apply_patch (Update)\n\n```\n{body}\n```\n")
                    written.append(
                        {
                            "original_path": fp,
                            "host_file": str(out),
                            "len": len(body),
                            "tool": "apply_patch:Update",
                        }
                    )
                else:  # Delete
                    written.append(
                        {
                            "original_path": fp,
                            "host_file": None,
                            "len": 0,
                            "tool": "apply_patch:Delete",
                        }
                    )

    subagents: list[dict[str, Any]] = []
    n = 0
    for tc in tool_calls:
        if tc.tool != "task":
            continue
        n += 1
        desc = tc.description or f"task_{n}"
        prompt = tc.prompt or ""
        subagent_type = tc.subagent_type or ""
        output = tc.output or ""
        status = tc.status or ""
        slug = slugify(desc)
        fname = paths.subagents_dir / f"agent_{n:02d}_{slug}.md"
        body = "\n".join(
            [
                f"# Sub-agent {n}: {desc}",
                "",
                f"- Subagent type: `{subagent_type}`",
                f"- Status: `{status}`",
                f"- Prompt length: {len(prompt)} chars",
                f"- Output length: {len(output)} chars",
                "",
                "## Prompt to sub-agent",
                "",
                "```",
                prompt,
                "```",
                "",
                "## Sub-agent output",
                "",
                output if output else "_(no output captured)_",
                "",
            ]
        )
        fname.write_text(body, encoding="utf-8")
        subagents.append(
            {
                "n": n,
                "description": desc,
                "subagent_type": subagent_type,
                "status": status,
                "prompt_len": len(prompt),
                "output_len": len(output),
                "host_file": str(fname),
            }
        )

    # Belt-and-suspenders: capture everything in the worktree's ``.contremaitre/``
    # directory directly. The event-based scan above only catches files written
    # via opencode's write/edit/apply_patch tools; files written via bash
    # heredoc (the skill's architecture-review HTML report is the classic
    # example) don't appear in tool_use events. Reading from disk ensures we
    # don't lose them.
    scaffolds = paths.worktree / ".contremaitre"
    salvaged: list[dict[str, Any]] = []
    if scaffolds.is_dir():
        already_have = {entry["host_file"] for entry in written if entry.get("host_file")}
        for src in sorted(scaffolds.iterdir()):
            if not src.is_file():
                continue
            dst = paths.extracted_files_dir / f".contremaitre__{src.name}"
            if str(dst) in already_have:
                continue
            try:
                dst.write_bytes(src.read_bytes())
            except OSError:
                continue
            salvaged.append(
                {
                    "original_path": f".contremaitre/{src.name}",
                    "host_file": str(dst),
                    "len": src.stat().st_size,
                    "tool": "worktree_scan",
                }
            )
    written.extend(salvaged)

    return {
        "files_written_count": len(written),
        "files_written": written,
        "subagent_count": len(subagents),
        "subagents": subagents,
    }
