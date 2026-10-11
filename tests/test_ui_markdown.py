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


FAKE_DOM = """
const node = function(tag){
  return {tag: tag, className: '', kids: [],
    setAttribute: function(){}, appendChild: function(c){ this.kids.push(c); return c; },
    set textContent(v){ this.kids = v ? [{text: String(v)}] : []; }};
};
const document = {createElement: node, createTextNode: function(t){ return {text: t}; }};
const html = function(n){
  if(!n.tag) return n.text;
  return '<' + n.tag + (n.className ? ' class="' + n.className + '"' : '') + '>' + n.kids.map(html).join('') + '</' + n.tag + '>';
};
"""


def _ui_functions(*names: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    ui = (config.ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    parts = []
    for name in names:
        start = ui.index("function " + name + "(")
        parts.append(ui[start:ui.index("\n}\n", start) + 3])
    return "".join(parts)


def _node(script: str) -> str:
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout


def _md_blocks(text: str):
    script = _ui_functions("mdBlocks") + "process.stdout.write(JSON.stringify(mdBlocks(" + json.dumps(text) + ")));"
    return json.loads(_node(script))


def _render(text: str) -> str:
    script = (FAKE_DOM + _ui_functions("el", "renderInline", "mdBlocks", "renderRich")
              + "process.stdout.write(html(renderRich(" + json.dumps(text) + ", el('div'))));")
    return _node(script)


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


def test_escaped_pipe_stays_inside_its_cell():
    blocks = _md_blocks("| A | B |\n|---|---|\n| x \\| y | z |")
    assert blocks[0]["rows"] == [["x | y", "z"]]


def test_rule_with_a_different_width_is_not_a_table():
    assert [b["kind"] for b in _md_blocks("x | y\n-|")] == ["text"]


def test_heading_keeps_a_trailing_hash_that_belongs_to_the_word():
    assert _md_blocks("## Learn C#")[0]["text"] == "Learn C#"


def test_table_renders_th_td_and_inline_markup_in_cells():
    out = _render(ANSWER)
    assert '<div class="md-table"><table><thead><tr><th>Tier</th><th>FX spread</th>' in out
    assert "<td>Tier 2</td><td>0.9%</td><td><strong>EUR 1,000</strong></td>" in out
    assert '<h2 class="md-h md-h2">Next step</h2>' in out
    assert '<div class="md-p">Your Tier 3 spread is 0.5%.</div>' in out


def test_plain_answer_renders_inline_as_before():
    out = _render("Hi **there**\nnext `code`")
    assert out == "<div>Hi <strong>there</strong>\nnext <code>code</code></div>"
