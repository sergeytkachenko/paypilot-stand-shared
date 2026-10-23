# 0008. Трейс для Langfuse: справжній час, атрибути на кожному спані, вміст запиту, ціна провайдера

## Status

Accepted, 2026-10-23.

## Context

L08 переходить із Phoenix на Langfuse: студент читає дерево ходу з типами
вузлів, сесію як документ розмови, вхід і вихід кожного виклику моделі,
звіряє три ціни ходу. Стенд на 325e2dd цього не віддавав:

- `otel._emit` відтворював дерево після ходу через `start_as_current_span`,
  тому в будь-якому бекенді спани мали ~0 мс;
- `llm.call` ніс лише токени — ні запиту, ні відповіді;
- `agent.summarize` не ніс ні входу, ні токенів;
- `usage.cost` від OpenRouter провайдер викидав;
- заголовків для OTLP-ендпоінту з авторизацією не було.

Замір 23.10 (qa-lecture `content/dossier/l08/L08-v2-verify.md`) на Langfuse
4.69:

- v4 зберігає спостереження, а не трейси: `session.id`, `user.id` і середовище
  лише на корені лишаються на корені, а сесія без них на генераціях має
  порожню ціну;
- передана `gen_ai.usage.cost` перекриває визначення моделі;
- відра `usage_details` приймаються як є, відро без ціни у визначенні
  рахується як $0;
- haiku 5.5 через OpenRouter міркує за замовчуванням, і
  `completion_tokens` включає `reasoning_tokens`.

## Decision

1. `otel.export_tree` відкриває кожен спан зі `start_time` = `started_at` і
   закриває з `end_time` = `ended_at` дерева.
2. Два OTLP-експортери в одному `TracerProvider`:
   - `OTEL_EXPORTER_OTLP_ENDPOINT` (+ `OTEL_EXPORTER_OTLP_HEADERS`) для Phoenix
     або будь-якого бекенду;
   - `LANGFUSE_HOST` + `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` для
     Langfuse (Basic-авторизація, `x-langfuse-ingestion-version: 4`).

   `/health` показує `trace_backends`.
3. На кожен спан:
   - `langfuse.observation.type` за назвою;
   - `session.id`, `user.id`, `langfuse.environment` (профіль),
     `langfuse.release` (`STAND_RELEASE`; Helm передає тег образу),
     `langfuse.trace.tags` (профіль, дефекти, модель, `run.trace_tag`).

   `run.trace_tag` = `TRACE_TAG` або назва ключа `X-Stand-Key`.
4. На генераціях:
   - `langfuse.observation.usage_details` — ексклюзивні відра (`input` без
     кешу, `input_cached_tokens`, `input_cache_creation_tokens`, `output` без
     міркування, `output_reasoning_tokens`);
   - `stand.cost_usd` — ціна за `pricing.py`. `gen_ai.usage.cost` не
     передається, щоб ціну Langfuse давало визначення моделі;
   - на `llm.call` — `prompt.name` / `prompt.version` з `prompt.version`
     ходу, оверлеї — у метаданих.
5. `TRACE_CAPTURE_CONTENT=1` (типово вимкнено, compose вмикає). Що з'являється
   на спанах:
   - `llm.call`, разом із повторами D14: `gen_ai.prompt` (запит у форматі OpenAI:
     system після `prompt.stamp`, повідомлення після `_messages_for_model`,
     назви інструментів) і `gen_ai.completion` (відповідь у форматі OpenAI з
     `tool_calls` і `reasoning`). Формат OpenAI — щоб Langfuse показував
     генерацію і сесію як розмову;
   - `agent.summarize`: `summary.input`;
   - корінь: `request.message`, `response.answer`.

   Однаково в `traces.jsonl`, `/api/_test/traces` і OTLP.
6. `ModelResponse.provider_cost_usd`, `reasoning_tokens`, `reasoning_text`.
   OpenAI-формат читає:
   - `usage.cost`;
   - `completion_tokens_details.reasoning_tokens`;
   - `message.reasoning`, а за його відсутності — `reasoning_details`.

   Спани отримують `gen_ai.usage.reasoning_tokens` і `provider.cost_usd`.
7. `agent.summarize` несе `gen_ai.usage.*` свого виклику.
8. `/chat`:
   - `usage.provider_cost_usd` — сума `provider_cost_usd` усіх викликів ходу,
     разом із повторами D14 і стисненням; `null`, якщо хоч один виклик без
     рахунку провайдера;
   - `usage.cost_usd` не змінюється.

Відхилено:

- Langfuse SDK (друга модель трейсу поруч із власною);
- `gen_ai.usage.cost` (ламає крок «$0 → визначення моделі» і ховає
  розбіжність прайсу);
- вміст запиту увімкнений за замовчуванням (десятки кілобайтів на крок на
  спільному стенді).

## Consequences

- `usage.cost_usd` лишається сліпим до повторів D14 і стиснення. Це навмисно:
  поворот 1 L08 стоїть на розбіжності з `usage.provider_cost_usd`.
- `gen_ai.usage.total_*` кореня і ранер L03 токенів стиснення не бачать, як і
  раніше; бюджети кейсів не зсуваються.
- Вміст іде в усі налаштовані OTLP-бекенди (Phoenix теж) і в `/api/_test/traces`.
  Дані стенду синтетичні; на спільному стенді вміст вимкнений, і вмикати його
  там можна лише разом із закритим `/api/_test/*`.
- `/health`: `otlp` тепер означає «є хоч один бекенд трейсів», список — у
  `trace_backends`; UI показує список замість «Phoenix підключено».
- Провайдер трейсів стенду більше не глобальний: FastAPI 0.16x має вбудовану
  OTel-інструментацію і писав би службові спани (`fastapi.*`, `GET …`) у ті
  самі бекенди.
- `traces.jsonl` з вмістом росте приблизно в 9 разів: ~4 МБ на прогін кіта
  L08 B1–B3 × 5 проти 0,44 МБ.
- Відра для моделей, що звітують запис і читання кешу з перетином (Gemini
  через OpenRouter), рахуються з обрізанням `input` до нуля; точна модель
  відер для другої моделі — разом із вибором моделі на запит.
