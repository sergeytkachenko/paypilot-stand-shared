"""CI smoke: prove every surface the course depends on actually answers.

Runs against the app in-process on the mock provider, so it needs no API key
and no running container. Exit code 1 on the first broken surface.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("PROFILE", "clean")
os.environ.setdefault("CLOCK_OVERRIDE", "2026-09-15T10:00:00Z")

# Its own database. One of these checks resets state to the seed, and the
# runbook tells a lecturer to run this before a lesson — with the stand
# already up, that used to wipe the stand's data.
import tempfile
os.environ.setdefault("PAYPILOT_DB",
                      str(Path(tempfile.mkdtemp(prefix="paypilot-smoke-")) / "smoke.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# Windows consoles default to cp1252: any non-ASCII in the output kills the run
# with UnicodeEncodeError before the verdict is printed. Force UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


client = TestClient(app)
failures = []


def check(label, fn):
    try:
        fn()
        print(f"[OK]   {label}")
    except Exception as e:
        print(f"[FAIL] {label}: {type(e).__name__}: {e}")
        failures.append(label)


def _chat_surface():
    r = client.post("/chat", json={"message": "What is the balance for CUS-0001?"})
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body["answer"], "empty answer"
    tree = client.get(f"/api/_test/traces/{body['request_id']}").json()
    names = [c["name"] for c in tree["children"]]
    assert "llm.call" in names, names


def _ui_surface():
    r = client.get("/")
    assert r.status_code == 200 and "PayPilot" in r.text


def _profiles_all_valid():
    from app import defects
    for name in defects.PROFILES:
        r = client.put("/api/_test/profile", json={"profile": name})
        assert r.status_code == 200, f"{name}: {r.status_code}"
        r = client.get("/api/_test/prompt")
        assert r.status_code == 200, f"prompt build failed on {name}"
    client.put("/api/_test/profile", json={"profile": None})


def _every_defect_toggles():
    from app import defects
    for did in defects.REGISTRY:
        r = client.put("/api/_test/defects", json={"defects": did})
        assert r.status_code == 200, f"{did}: {r.status_code}"
        assert did in r.json()["active"], did
        assert client.get("/api/_test/prompt").status_code == 200, f"prompt: {did}"
    client.put("/api/_test/defects", json={"defects": None})


def _state_tables():
    from app import db
    for table in db.STATE_TABLES:
        assert client.get(f"/api/_test/state/{table}").status_code == 200, table


def _reference_values():
    from app.engines import policy
    body = client.get("/api/_test/reference").json()
    assert body["FX_SPREAD_PCT"] == policy.FX_SPREAD_PCT, body
    assert body["DISPUTE_WINDOWS_DAYS"] == policy.DISPUTE_WINDOWS_DAYS, body
    assert not any("AML" in key for key in body), sorted(body)


def _clock_control():
    assert client.post("/api/_test/clock",
                       json={"now": "2026-12-01T00:00:00Z"}).status_code == 200
    assert client.get("/api/_test/clock").json()["now"].startswith("2026-12-01")
    client.post("/api/_test/clock", json={"now": None})


def _reset_restores_seed():
    from app.agent import tools
    tools.escalate_to_human("CUS-0001", "ci smoke")
    assert client.get("/api/_test/state/escalations").json()["rows"]
    assert client.post("/api/_test/reset").status_code == 200
    assert client.get("/api/_test/state/escalations").json()["rows"] == []


def _artefacts_present():
    specs = client.get("/api/_test/specs").json()["requirements"]
    assert "US-01.md" in specs, list(specs)
    tools_ = {t["name"] for t in client.get("/api/_test/tools").json()["tools"]}
    assert {"quote_fx", "create_dispute", "search_knowledge_base"} <= tools_


def _corpus_size():
    from app.rag import retriever
    n = len(retriever._index("kb_clean"))
    assert n >= 90, f"kb_clean has only {n} fragments"


def _engines_are_importable_oracles():
    from datetime import date
    from app.engines import disputes, fx, limits
    assert fx.quote(100, "EUR", "USD", "tier1").final_amount > 0
    assert limits.status("tier1", date(2026, 9, 15), []).daily_remaining_eur > 0
    assert disputes.check("duplicate_charge", date(2026, 9, 1), "settled",
                          date(2026, 9, 15), False).eligible


for label, fn in [
    ("chat + trace", _chat_surface),
    ("chat UI served", _ui_surface),
    ("every profile builds a prompt", _profiles_all_valid),
    ("every defect toggles cleanly", _every_defect_toggles),
    ("state tables readable", _state_tables),
    ("clock control", _clock_control),
    ("engine reference values", _reference_values),
    ("reset restores seed", _reset_restores_seed),
    ("audit artefacts present", _artefacts_present),
    ("corpus at spec size", _corpus_size),
    ("engines usable as oracles", _engines_are_importable_oracles),
]:
    check(label, fn)

print()
if failures:
    print(f"{len(failures)} surface(s) broken: {', '.join(failures)}")
    sys.exit(1)
print("All stand surfaces OK.")
