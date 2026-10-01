"""Ask a model one question through this server and show each tool call as it happens.

This is what docs/demo.gif records. The model gets the same isolated setup as the eval in
evals/run_eval.py: this server over stdio and nothing else.

    uv run python scripts/demo.py
    uv run python scripts/demo.py "Is 51 824 753 556 a valid ABN?"
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = "au-opendata"
QUESTION = (
    "Which Australian state has the most public toilets? Use the National Public Toilet "
    "Map: check the file's columns first, then count. Keep it brief: the top three "
    "states, then the source and licence."
)
WIDTH = 92

BOLD, DIM, CYAN, GREEN, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[0m"


def show_call(name: str, args: dict[str, object]) -> None:
    tool = name.removeprefix(f"mcp__{SERVER}__")
    sql = args.get("sql")
    shown = {k: v for k, v in args.items() if k != "sql"}
    brief = " ".join(f"{k}={json.dumps(v)}" for k, v in shown.items())
    print(f"  {CYAN}● {tool}{RESET} {DIM}{textwrap.shorten(brief, WIDTH - 24)}{RESET}")
    if sql:
        for line in textwrap.wrap(str(sql), WIDTH - 6):
            print(f"      {GREEN}{line}{RESET}")


def show_answer(text: str) -> None:
    print()
    for para in text.strip().splitlines():
        line = textwrap.fill(para, WIDTH) if para.strip() else ""
        print(re.sub(r"\*\*(.+?)\*\*", rf"{BOLD}\1{RESET}", line, flags=re.S))


def main() -> None:
    question = " ".join(sys.argv[1:]) or QUESTION
    print(f"{BOLD}" + textwrap.fill(f"❯ {question}", WIDTH, subsequent_indent="  ") + f"{RESET}\n")
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / "mcp.json"
        server = {"command": "uv", "args": ["--directory", str(ROOT), "run", "au-opendata-mcp"]}
        config.write_text(json.dumps({"mcpServers": {SERVER: server}}))
        cmd = [
            "claude", "-p", question,
            "--model", "sonnet",
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(config), "--strict-mcp-config",
            "--setting-sources", "project",
            "--tools", "",
            "--allowedTools", f"mcp__{SERVER}",
            "--system-prompt", "You are a helpful assistant with access to tools. Be concise.",
        ]  # fmt: skip
        with subprocess.Popen(cmd, cwd=tmp, stdout=subprocess.PIPE, text=True) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                event = json.loads(line)
                if event.get("type") == "assistant":
                    for block in event["message"]["content"]:
                        if block.get("type") == "tool_use":
                            show_call(block["name"], block["input"])
                elif event.get("type") == "result":
                    show_answer(event.get("result", ""))


if __name__ == "__main__":
    main()
