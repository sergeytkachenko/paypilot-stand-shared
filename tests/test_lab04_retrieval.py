from app import config
from app.agent import prompt
from app.rag import retriever

C14_QUERIES = (
    "dispute reason codes list duplicate charge dispute window days",
    "duplicate charge dispute window days",
    "full list of dispute reason codes and chargeback windows",
    "duplicate_charge window days from transaction date",
)
C14_WIDE_QUERIES = (
    "duplicate_charge dispute window days",
    "duplicate_charge window days reason code table",
    "reason codes list chargeback window duplicate charge unauthorized debit",
    "dispute window table reason code duplicate_charge service_not_rendered",
)
C13_QUERIES = (
    "FX spread table by tier",
    "Tier 3 free monthly conversion allowance EUR",
    "tariff schedule FX spread table Tier 3 0.5%",
    "Tier 3 conversion allowance",
)


def _broken(fragment_id: str) -> str:
    return next(c["text"] for c in retriever._index("kb_broken") if c["id"] == fragment_id)


def _ids(query: str, top_k: int) -> list[str]:
    return [f["id"] for f in retriever.search(query, top_k=top_k, index="kb_broken")["fragments"]]


def test_index_sizes_stay_as_the_course_quotes_them():
    assert len(retriever._index("kb_broken")) == 129
    assert len(retriever._index("kb_clean")) == 98


def test_tier_3_allowance_is_cut_into_a_plausible_wrong_number():
    assert _broken("tariffs.md#b3").endswith("| Tier 3 | 0.5% | EUR 5")
    assert _broken("tariffs.md#b4").startswith(",000 |")


def test_more_fragments_never_bring_the_tier_3_continuation():
    for query in C13_QUERIES:
        ids = _ids(query, 5)
        assert ids[0] == "tariffs.md#b3", query
        assert "tariffs.md#b4" not in ids, query


def test_duplicate_charge_row_is_whole_next_to_its_header():
    chunk = _broken("disputes.md#b2")
    assert "| Reason code | Window (days) |" in chunk
    assert "| duplicate_charge | 60 |" in chunk


def test_one_fragment_returns_the_intro_and_five_bring_the_window():
    for query in C14_QUERIES:
        assert _ids(query, 1) == ["disputes.md#b1"], query
    for query in C14_WIDE_QUERIES:
        assert "disputes.md#b2" in _ids(query, 5), query


def test_d05_plants_a_stale_swift_fee_but_keeps_search_for_everything_else():
    overlay = " ".join((config.PROMPTS_DIR / "overlays" / f"{prompt.overlay_slug('D05')}.md")
                       .read_text(encoding="utf-8").split())
    assert "SWIFT EUR 15.00 flat, no percentage fee" in overlay
    assert "search the documentation" in overlay
    assert "from memory" not in overlay
