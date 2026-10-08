""
import re

_UK_ONLY = re.compile(r"[іїєґ]", re.I)
_RU_ONLY = re.compile(r"[ыэъё]", re.I)
_RU_WORDS = re.compile(
    r"(?<![а-яіїєґ])(?:какой|какая|какие|какую|меня|мне|спасибо|пожалуйста|сколько|"
    r"что|это|мой|могу|можно|нужно|хочу|почему|когда|где|если|еще|счет|"
    r"перевод|сейчас)(?![а-яіїєґ])", re.I)
_CYRILLIC = re.compile(r"[а-яіїєґё]", re.I)
_LETTER = re.compile(r"[^\W\d_]")

UK_RULE = (
    "## Reply language\n"
    "The customer writes in Ukrainian. Write your whole reply in Ukrainian — "
    "never in Russian, and not in English. Keep amounts, account and transaction "
    "IDs, product names and codes exactly as they appear in tool results and "
    "knowledge-base fragments.")


def detect(text: str) -> str | None:
    if _UK_ONLY.search(text or ""):
        return "uk"
    if _RU_ONLY.search(text or "") or _RU_WORDS.search(text or ""):
        return "ru"
    letters = _LETTER.findall(text or "")
    if not letters:
        return None
    cyrillic = sum(1 for ch in letters if _CYRILLIC.match(ch))
    return "uk" if cyrillic / len(letters) >= 0.5 else None


def apply(system: str, user_message: str) -> tuple[str, str | None]:
    language = detect(user_message)
    if language == "uk":
        return system.rstrip() + "\n\n" + UK_RULE + "\n", language
    return system, language
