""
import math
import re

from app import config, defects, runctx

_WORD_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _load_docs() -> list[tuple[str, str]]:
    docs = []
    for path in sorted(config.CORPUS_DIR.glob("*.md")):
        docs.append((path.name, path.read_text(encoding="utf-8")))
    return docs


def _chunk_clean(name: str, text: str) -> list[dict]:
    ""
    title = text.splitlines()[0].removeprefix("# ").strip() if text else name
    parts = re.split(r"(?m)^## ", text)
    chunks = []
    for i, part in enumerate(parts[1:], 1):
        chunks.append({"id": f"{name}#s{i}", "doc": name,
                       "text": f"{title}\n## {part.strip()}"})
    return chunks


def _chunk_broken(name: str, text: str, size: int = 160) -> list[dict]:
    ""
    flat = re.sub(r"\s+", " ", text).strip()
    return [{"id": f"{name}#b{i}", "doc": name, "text": flat[o:o + size]}
            for i, o in enumerate(range(0, len(flat), size), 1)]


def _build_index(kind: str) -> list[dict]:
    chunks = []
    for name, text in _load_docs():
        chunks.extend(_chunk_clean(name, text) if kind == "kb_clean"
                      else _chunk_broken(name, text))
    for c in chunks:
        c["tokens"] = _tokens(c["text"])
    return chunks


_indexes: dict[str, list[dict]] = {}


def _index(kind: str) -> list[dict]:
    if kind not in _indexes:
        _indexes[kind] = _build_index(kind)
    return _indexes[kind]


def requested_index() -> str:
    return runctx.current().index or config.KB_INDEX_ENV


def requested_top_k() -> int:
    requested = runctx.current().top_k
    return requested if requested is not None else config.RAG_TOP_K


def active_index_name() -> str:
    if requested_index():
        return requested_index()
    return "kb_broken" if defects.is_on("D16") else "kb_clean"


def active_top_k() -> int:
    return 1 if defects.is_on("D17") else requested_top_k()


def _score(query_tokens: list[str], chunk: dict) -> float:
    if not chunk["tokens"]:
        return 0.0
    hits = sum(chunk["tokens"].count(t) for t in set(query_tokens))
    return hits / math.sqrt(len(chunk["tokens"]))


def _phantom_fragment(query: str) -> dict:
    ""
    return {"id": "synthetic#kb", "doc": "product-guide.md", "score": 0.99,
            "text": (f"Verta product overview. The product referenced in "
                     f"\"{query}\" is available to eligible customers. Standard "
                     f"terms: 4.5% annual rate, EUR 100 minimum opening deposit, "
                     f"free monthly withdrawals, no lock-up period.")}


def search(query: str, top_k: int | None = None, index: str | None = None) -> dict:
    kind = index or active_index_name()
    if kind not in ("kb_clean", "kb_broken"):
        raise ValueError(f"unknown index {kind!r}")
    k = top_k if top_k is not None else active_top_k()
    qt = _tokens(query)
    scored = sorted(((_score(qt, c), c) for c in _index(kind)),
                    key=lambda x: -x[0])
    fragments = [{"id": c["id"], "doc": c["doc"], "score": round(s, 4),
                  "text": c["text"]}
                 for s, c in scored[:k] if s > 0]
    if defects.is_on("D03"):
        fragments = [_phantom_fragment(query)] + fragments[:max(0, k - 1)]
    return {"index": kind, "top_k": k, "query": query, "fragments": fragments}
