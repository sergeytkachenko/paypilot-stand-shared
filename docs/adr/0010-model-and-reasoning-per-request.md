# 0010. Модель і міркування на запит, прайс пари B4

## Status

Accepted, 2026-10-23.

## Context

Лабораторна L08 порівнює дві моделі на одному діалозі (B4: `google/gemini-2.5-flash`
→ `anthropic/claude-haiku-5.5`) і показує міркування як окреме відро токенів.
Модель задавалась лише змінною `LLM_MODEL` сервера, а міркування не
керувалось. Замір 23.10 (`L08-v2-verify.md`):

- розрив між режимами міркування на haiku менший за ×2 (`high` проти типового
  ×0,94–1,14), тож окремий дефект D30 не потрібен;
- без множників кешу `usage.cost_usd` на Gemini завищений у 1,9 раза;
- Gemini звітує `cached_tokens` = `cache_write_tokens` на першому виклику.

## Decision

1. `RunSettings.model` — id зі списку `profiles/models.yaml`, інше → `400`.
   Провайдер бере модель із запиту, інакше з `LLM_MODEL`.
2. `RunSettings.reasoning` — `off | low | medium | high`; `LLM_REASONING` — типове
   значення сервера. OpenRouter-провайдер шле `{"reasoning": {"enabled": false}}`
   або `{"reasoning": {"effort": …}}`.
3. `cache_control` у системному повідомленні — за полем списку моделей.
4. Прайс Gemini в `pricing.py`: $0,30 / $2,50 за млн, читання кешу ×0,1, запис
   ×0,3778. `pricing.cache_buckets` розкладає перетин Gemini
   (`read = cached − written`) на ексклюзивні відра; той самий розклад
   використовує `otel.usage_details`.
5. `validate_startup` вимагає ціну для кожної моделі зі списку.
6. Корінь трейсу несе `run.model` і `run.reasoning`; теги — модель і
   `reasoning:<рівень>`.

Відхилено:

- D30 (розрив менший за ×2);
- ціни моделей у `models.yaml` (друге джерело правди поруч із `pricing.py`);
- вільний `model` у заголовку (бюджет ключа не можна рахувати без ціни).

## Consequences

- Множник запису Gemini підігнано за одним виміром; розбіжність між
  `usage.cost_usd` і `usage.provider_cost_usd` на Gemini покаже, якщо він хибний.
- Anthropic-провайдер `reasoning` ігнорує.
- Нову модель додають двома змінами: рядок у `models.yaml` і прайс у `pricing.py`.
