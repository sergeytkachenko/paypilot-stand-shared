import json
import shutil
import subprocess

import pytest

from app import config

ANSWER = (
    "The documentation search returned the same truncated table:\n"
    "\n"
    "| Tier | FX spread | Free monthly conversion allowance |\n"
    "|---|---|---|\n"
    "| Tier 1 | 1.5% | EUR 500 |\n"
    "| Tier 2 | 0.9% | **EUR 1,000** |\n"
    "| Tier 3 | 0.5% |\n"
    "\n"
    "## Next step\n"
    "Your Tier 3 spread is 0.5%."
)


def _md_blocks(text: str):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    ui = (config.ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    start = ui.index("function mdBlocks(text){")
    end = ui.index("\n}\n", start) + 3
    script = ui[start:end] + "process.stdout.write(JSON.stringify(mdBlocks(" + json.dumps(text) + ")));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_pipe_table_with_rule_becomes_a_table_block():
    blocks = _md_blocks(ANSWER)
    assert [b["kind"] for b in blocks] == ["text", "table", "text", "heading", "text"]
    table = blocks[1]
    assert table["head"] == ["Tier", "FX spread", "Free monthly conversion allowance"]
    assert table["rows"] == [
        ["Tier 1", "1.5%", "EUR 500"],
        ["Tier 2", "0.9%", "**EUR 1,000**"],
        ["Tier 3", "0.5%", ""],
    ]
    assert blocks[3] == {"kind": "heading", "level": 2, "text": "Next step"}


def test_pipes_without_a_rule_stay_text():
    text = "Compare a | b and c | d\nnothing else"
    assert _md_blocks(text) == [{"kind": "text", "text": text}]


def test_plain_answer_is_one_text_block():
    text = "- **Escalate** to a human agent\n- Get a quote\n\nWhich would you like?"
    assert _md_blocks(text) == [{"kind": "text", "text": text}]
