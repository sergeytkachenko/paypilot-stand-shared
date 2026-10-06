""
import json
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, fields, replace

HEADER = "X-Stand-Settings"
ADMIN_HEADER = "X-Stand-Admin"
INDEXES = ("kb_clean", "kb_broken", "")


@dataclass(frozen=True)
class RunSettings:
    profile: str | None = None
    defects: str | None = None
    top_k: int | None = None
    index: str | None = None
    summarize_after: int | None = None
    clock: str | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


_current: ContextVar[RunSettings] = ContextVar("stand_run_settings",
                                               default=RunSettings())


def current() -> RunSettings:
    return _current.get()


def scope() -> str:
    return "server" if current().is_empty() else "request"


def activate(settings: RunSettings) -> Token:
    return _current.set(settings)


def deactivate(token: Token) -> None:
    _current.reset(token)


@contextmanager
def override(**changes):
    token = _current.set(replace(_current.get(), **changes))
    try:
        yield
    finally:
        _current.reset(token)


def parse_header(raw: str) -> RunSettings:
    from app import clock, defects
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{HEADER}: not valid JSON ({e.msg})")
    if not isinstance(data, dict):
        raise ValueError(f"{HEADER}: expected a JSON object")
    known = {f.name for f in fields(RunSettings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{HEADER}: unknown fields {sorted(unknown)}; "
                         f"known: {sorted(known)}")
    out = {}
    profile = data.get("profile")
    if profile not in (None, ""):
        if profile not in defects.PROFILES:
            raise ValueError(f"{HEADER}: unknown profile {profile!r}. "
                             f"Known: {sorted(defects.PROFILES)}")
        out["profile"] = profile
    raw_defects = data.get("defects")
    if raw_defects is not None:
        if isinstance(raw_defects, list):
            raw_defects = ",".join(str(x) for x in raw_defects)
        if not isinstance(raw_defects, str):
            raise ValueError(f"{HEADER}: defects must be a comma list or an array")
        try:
            defects._parse_list(raw_defects)
        except ValueError as e:
            raise ValueError(f"{HEADER}: {e}")
        out["defects"] = raw_defects
    top_k = data.get("top_k")
    if top_k not in (None, ""):
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
            raise ValueError(f"{HEADER}: top_k must be an integer between 1 and 20")
        out["top_k"] = top_k
    index = data.get("index")
    if index not in (None, ""):
        if index not in INDEXES:
            raise ValueError(f"{HEADER}: index must be kb_clean or kb_broken")
        out["index"] = index
    summarize_after = data.get("summarize_after")
    if summarize_after not in (None, ""):
        if (not isinstance(summarize_after, int) or isinstance(summarize_after, bool)
                or summarize_after < 1):
            raise ValueError(f"{HEADER}: summarize_after must be an integer >= 1")
        out["summarize_after"] = summarize_after
    clock_value = data.get("clock")
    if clock_value not in (None, ""):
        if not isinstance(clock_value, str):
            raise ValueError(f"{HEADER}: clock must be an ISO-8601 string")
        try:
            clock._parse(clock_value)
        except ValueError:
            raise ValueError(f"{HEADER}: clock is not an ISO-8601 timestamp")
        out["clock"] = clock_value
    return RunSettings(**out)
