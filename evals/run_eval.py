"""Run the questions in evals/questions.md against a real model and record its tool calls.

The model is driven through Claude Code's headless mode (`claude -p`). It is given this
server over stdio and nothing else: no built-in tools, no other MCP servers, no user
settings, hooks or plugins, an empty working directory, and a one-line system prompt. What
it can see of the server is exactly what any MCP client sees -- the tool names, descriptions
and schemas -- so the tool sequence it picks is a test of those descriptions.

Questions 6-16 refer back to each other ("that dataset", "that CSV"), so they run as one
conversation, resumed turn by turn. Every other question runs in a fresh session.

    uv run python evals/run_eval.py                  # all 20, default model
    uv run python evals/run_eval.py --only 1 17      # a subset
    uv run python evals/run_eval.py --model haiku

    uv run python evals/run_eval.py --regrade evals/runs/<run>   # re-score a saved run

Each run writes evals/runs/<timestamp>-<model>/ with a summary.json of the tool calls and
final answers, plus the raw stream-json transcript per question. The transcripts carry local
paths and account metadata, so they are git-ignored; summary.json is the record. Grading the
answers is left to a human reading summary.json; the script only checks the tool sequence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "evals" / "questions.md"
RUNS = ROOT / "evals" / "runs"

SERVER = "au-opendata"
TOOL_PREFIX = f"mcp__{SERVER}__"
TOOLS = {
    "list_portals",
    "search_datasets",
    "get_dataset",
    "list_organizations",
    "preview_resource",
    "query_resource",
    "validate_abn",
    "au_financial_year",
}
SYSTEM_PROMPT = "You are a helpful assistant with access to tools."

# Questions that continue one conversation. Everything else is its own session.
CHAINED = list(range(6, 17))
# Tools whose result, once in a conversation, a later question may answer from.
REUSABLE = {"get_dataset", "preview_resource"}
# Questions whose expected answer is a refusal, and the tool calling which is the opposite of
# refusing. Without this, Q15 ("read queryable: no and refuse") passed on a get_dataset call
# carried over from Q6, even when the model went on to query the PDF in that very turn.
MUST_NOT_CALL = {15: {"query_resource"}}

# Questions 12 and 14 are templates; a human running the eval fills in real columns after
# seeing the preview. These are the toilet map CSV's real column names. Its only numeric
# columns are stored as text, so 12 also checks the model casts before averaging.
FILL_INS = {
    12: {"<numeric column>": "Latitude", "<category column>": "State"},
    14: {"<column>": "OpeningHours"},
}


@dataclass
class Question:
    number: int
    text: str
    expected: list[str]


@dataclass
class Turn:
    number: int
    prompt: str
    expected: list[str]
    calls: list[dict[str, object]] = field(default_factory=list)
    earlier: list[str] = field(default_factory=list)  # tools called earlier in this chain
    forbidden: set[str] = field(default_factory=set)  # see MUST_NOT_CALL
    answer: str = ""
    session_id: str = ""
    is_error: bool = False

    @property
    def tools_called(self) -> list[str]:
        return [str(c["tool"]) for c in self.calls]

    @property
    def refused(self) -> bool:
        """None of this turn's calls is one its expected refusal rules out."""
        return not self.forbidden & set(self.tools_called)

    @property
    def strict_ok(self) -> bool:
        """The expected tools appear, in order, among this turn's calls. Extras allowed."""
        return self.refused and _in_order(self.expected, self.tools_called)

    @property
    def sequence_ok(self) -> bool:
        """As strict_ok, but a fetch made earlier in the same conversation counts.

        In the chained questions the model may answer from a dataset or preview already in
        context instead of fetching it again. Re-fetching would not make that answer more
        right, so it is not a failure. Only fetches carry over (see REUSABLE): a question
        that needs a query run still needs it run, whatever ran before it. Nor can context
        satisfy a refusal: that is about what this turn does (see MUST_NOT_CALL).
        """
        return self.refused and _in_order(self.expected, self.earlier + self.tools_called)


def _in_order(expected: list[str], called: list[str]) -> bool:
    remaining = iter(called)
    return all(any(c == want for c in remaining) for want in expected)


def load_questions() -> dict[int, Question]:
    rows = re.findall(r"^\| (\d+) \| (.+?) \| (.+?) \| .*\|$", QUESTIONS.read_text(), re.M)
    questions = {}
    for num, text, seq in rows:
        expected = [t for t in re.findall(r"`(\w+)", seq) if t in TOOLS]
        questions[int(num)] = Question(int(num), text.strip(), expected)
    return questions


def prompt_for(q: Question) -> str:
    text = q.text
    for placeholder, value in FILL_INS.get(q.number, {}).items():
        text = text.replace(placeholder, value)
    return text


def ask(
    prompt: str,
    *,
    model: str,
    mcp_config: pathlib.Path,
    workdir: pathlib.Path,
    resume: str | None,
    transcript: pathlib.Path,
) -> list[dict[str, object]]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--tools",
        "",
        "--allowedTools",
        f"mcp__{SERVER}",
        "--system-prompt",
        SYSTEM_PROMPT,
    ]
    if resume:
        cmd += ["--resume", resume]
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=600)
    transcript.write_text(proc.stdout)
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def record(turn: Turn, events: list[dict[str, object]]) -> None:
    for event in events:
        if event.get("type") == "assistant":
            for block in event["message"]["content"]:  # type: ignore[index]
                if block.get("type") == "tool_use":
                    name = str(block["name"]).removeprefix(TOOL_PREFIX)
                    turn.calls.append({"tool": name, "input": block["input"]})
        elif event.get("type") == "result":
            turn.answer = str(event.get("result", ""))
            turn.session_id = str(event.get("session_id", ""))
            turn.is_error = bool(event.get("is_error"))


Events = list[dict[str, object]]


def grade(
    questions: dict[int, Question], numbered_events: Iterable[tuple[int, Events]]
) -> list[Turn]:
    """Turn (question number, transcript events) pairs into graded turns, in order."""
    turns: list[Turn] = []
    chain_tools: list[str] = []
    for num, events in numbered_events:
        q = questions[num]
        chained = num in CHAINED
        turn = Turn(
            num,
            prompt_for(q),
            q.expected,
            earlier=list(chain_tools) if chained else [],
            forbidden=MUST_NOT_CALL.get(num, set()),
        )
        record(turn, events)
        if chained:
            chain_tools += [t for t in turn.tools_called if t in REUSABLE]
        turns.append(turn)
        mark = "ok " if turn.sequence_ok else "SEQ"
        print(f"{mark} q{num:02d} {' -> '.join(turn.tools_called) or '(no tools)'}")
    return turns


def live_events(
    questions: dict[int, Question], selected: list[int], model: str, out: pathlib.Path
) -> Iterator[tuple[int, Events]]:
    """Ask each selected question in turn, yielding its transcript as it arrives."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = pathlib.Path(tmp)
        mcp_config = workdir / "mcp.json"
        server = {"command": "uv", "args": ["--directory", str(ROOT), "run", "au-opendata-mcp"]}
        mcp_config.write_text(json.dumps({"mcpServers": {SERVER: server}}))

        chain_session: str | None = None
        for num in selected:
            chained = num in CHAINED
            events = ask(
                prompt_for(questions[num]),
                model=model,
                mcp_config=mcp_config,
                workdir=workdir,
                resume=chain_session if chained else None,
                transcript=out / f"q{num:02d}.jsonl",
            )
            if chained:
                chain_session = next(
                    (str(e["session_id"]) for e in events if e.get("type") == "result"), None
                )
            yield num, events


def saved_events(run_dir: pathlib.Path) -> Iterator[tuple[int, Events]]:
    """Replay a previous run from its summary.json, so it can be re-graded without re-asking.

    Reads the committed summary rather than the raw transcripts, which are git-ignored and so
    do not survive a fresh clone. The summary holds every tool call and answer, which is all
    grading uses; it is rebuilt here into the event shape record() reads.
    """
    summary = json.loads((run_dir / "summary.json").read_text())
    for q in summary["questions"]:
        calls = [
            {"type": "tool_use", "name": TOOL_PREFIX + c["tool"], "input": c["input"]}
            for c in q["called"]
        ]
        yield (
            q["number"],
            [
                {"type": "assistant", "message": {"content": calls}},
                {"type": "result", "result": q["answer"], "is_error": q["is_error"]},
            ],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--only", type=int, nargs="*", help="question numbers to run")
    parser.add_argument("--regrade", type=pathlib.Path, help="re-grade a saved run directory")
    args = parser.parse_args()

    questions = load_questions()
    if args.regrade:
        out = args.regrade
        previous = json.loads((out / "summary.json").read_text())
        model, stamp = previous["model"], previous["run_at"]
        turns = grade(questions, saved_events(out))
    else:
        model = args.model
        stamp = dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
        out = RUNS / f"{stamp}-{model}"
        out.mkdir(parents=True)
        selected = sorted(args.only or questions)
        turns = grade(questions, live_events(questions, selected, model, out))

    summary = {
        "model": model,
        "run_at": stamp,
        "system_prompt": SYSTEM_PROMPT,
        "sequence_ok": sum(t.sequence_ok for t in turns),
        "strict_ok": sum(t.strict_ok for t in turns),
        "total": len(turns),
        "questions": [
            {
                "number": t.number,
                "prompt": t.prompt,
                "expected": t.expected,
                "called": t.calls,
                "sequence_ok": t.sequence_ok,
                "strict_ok": t.strict_ok,
                "is_error": t.is_error,
                "answer": t.answer,
            }
            for t in turns
        ],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(
        f"\nsequence check: {summary['sequence_ok']}/{summary['total']} in conversation, "
        f"{summary['strict_ok']}/{summary['total']} strict per turn  ->  {out}"
    )


if __name__ == "__main__":
    main()
