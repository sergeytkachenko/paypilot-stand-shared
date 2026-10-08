from app.agent import language


def test_ukrainian_is_detected_with_or_without_its_unique_letters():
    assert language.detect("Скільки днів у мене є, щоб оскаржити подвійне списання?") == "uk"
    assert language.detect("Я CUS-0005. Конвертуйте 6000 EUR у USD. Який спред я плачу?") == "uk"
    assert language.detect("Який у мене баланс?") == "uk"


def test_russian_and_english_get_no_ukrainian_rule():
    assert language.detect("Какой у меня баланс? Спасибо") == "ru"
    assert language.detect("I'm CUS-0005. Convert 6000 EUR to USD.") is None
    assert language.detect("CUS-0005 6000 EUR") is None
    assert language.detect("") is None


def test_rule_is_appended_only_for_ukrainian():
    base = "# PayPilot\n## 8. Examples\n"
    uk, lang = language.apply(base, "Який у мене баланс?")
    assert lang == "uk" and uk.startswith(base.rstrip()) and language.UK_RULE in uk
    en, lang = language.apply(base, "What is my balance?")
    assert lang is None and en == base


def test_words_shared_with_ukrainian_do_not_flip_it_to_russian():
    assert language.detect("Моя картка, уже заблокована?") == "uk"
    assert language.detect("Покажіть мою виписку") == "uk"
