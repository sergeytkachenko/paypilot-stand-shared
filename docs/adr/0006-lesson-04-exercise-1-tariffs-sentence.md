# 0006. Речення tariffs.md з ADR 0005 переписане під вправу 1 лабораторної v13

## Status

Accepted, 2026-10-11. Issue #49. Доповнює ADR 0005: рішення 0005 чинне, змінюється
лише формулювання першого речення `tariffs.md`.

## Context

ADR 0005 зсунув межі шматків `kb_broken` на −4 символи заміною «The tier
determines» на «Tiers determine». Числа для 0005 бралися із запитів C-13 і C-14.
Студентська лабораторна v13 ставить у вправі 1 інший запит: «what FX spread
applies to my account and what is my free monthly conversion allowance?». Живий
прогін після деплою 0005: на `kb_broken` з `top_k` 1 обірваний `tariffs.md#b3`
приїхав лише в 4 з 10 ходів. У решті 6 перший запит моделі («FX spread … free
monthly currency conversion allowance … tier») вигравав `tariffs.md#b2`: після
зсуву в ньому стоїть «… the free monthly conversion allowance and the transfer
limits. ## FX spreads by tier …» — усі слова запиту, але без таблиці.

## Decision

Перше речення `tariffs.md` стає «The tier of each account sets its pricing as
well as its daily and per-month transfer limits.» Довжина та сама, що в 0005,
тож `#b3` і далі закінчується `| Tier 3 | 0.5% | EUR 5`, а `#b4` починається з
`,000 |`. У `#b1` і `#b2` більше немає «FX spread» і «allowance». Симуляція на
запитах моделі з вправи 1: `#b3` першим 6 з 6, раніше 2 з 6. Для C-13
ранжування не змінилося. `tests/test_lab04_retrieval.py` дістав
`test_exercise_1_query_brings_the_cut_table_not_the_intro`.

## Consequences

- Секція `tariffs.md#s1` на `kb_clean` більше не перелічує спред і ліміт
  конвертації, тож числа recall і precision L04 на `kb_clean` знову
  перемірюються, перш ніж іти в матеріали курсу.
- Контракт зсувів ADR 0005 лишається: будь-яка правка цих речень проходить
  через `tests/test_lab04_retrieval.py`.
