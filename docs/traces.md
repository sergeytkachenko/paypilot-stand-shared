# Довідник по трейсах

Кожен хід діалогу породжує одне дерево спанів. Перевірки пишуться поверх них,
тому формат стабільний і задокументований.

**Найважливіше:** відповідь `/chat` не містить трейсу. Візьміть `request_id` із
відповіді й заберіть дерево окремо. Багато з того, що вам потрібно побачити, у
тексті відповіді не з'являється взагалі.

## Як отримати

| Спосіб | Де |
|---|---|
| API, одне дерево | `GET /api/_test/traces/{request_id}` — `request_id` повертає `/chat` |
| API, останні N | `GET /api/_test/traces?limit=20` |
| Файл | `traces/traces.jsonl` — по одному JSON-дереву на рядок |
| Phoenix UI | `http://localhost:6006` (піднімається разом зі стендом) |
| Langfuse UI | `http://localhost:3000`, вхід `student@paypilot.local` / `paypilot-local` (піднімається разом зі стендом) |

API і файл працюють завжди; Phoenix і Langfuse — додаткові поверхні, які отримують те саме дерево через OTLP. Трейси переживають
перезапуск стенду: якщо в пам'яті ходу вже немає, він читається з файлу.

## Структура дерева

```
agent.request                     ← корінь, один на хід діалогу
├── agent.summarize               ← лише коли спрацювала згортка історії
├── llm.call                      ← крок циклу оркестрації
├── tool.<name>                   ← виклик інструмента
├── llm.call
└── ...
```

Форма дерева сама по собі інформативна: скільки було кроків, які інструменти
викликані, у якому порядку, чи повторювалися виклики, чи спрацювала згортка.

## Атрибути

### `agent.request`

| Атрибут | Значення |
|---|---|
| `session.id` | ідентифікатор сесії |
| `session.customer_id` | клієнт, що увійшов у сесію (`customer_id` запиту); порожньо — клієнта немає |
| `dialog.step_number` | номер репліки в діалозі — щоб вибрати спани конкретного кроку |
| `run.profile` | активний профіль на момент ходу |
| `run.active_defects` | список активних дефектів |
| `prompt.version` | версія промпту, з накладеними оверлеями |
| `llm.provider` | `mock` / `anthropic` / `openai` / `openrouter` |
| `context.replay_active` | `true`, коли до контексту домішується історія |
| `gen_ai.usage.total_input_tokens` / `...output_tokens` | сумарні токени ходу |
| `gen_ai.usage.total_cache_read_input_tokens` | скільки вхідних токенів ходу прочитано з кешу провайдера |
| `run.trace_tag` | `TRACE_TAG` стенду або назва ключа `X-Stand-Key`; порожньо — ні того, ні того |
| `request.message` / `response.answer` | репліка клієнта і відповідь; лише з `TRACE_CAPTURE_CONTENT=1` |

Перші п'ять — це те, що робить прогін відтворюваним. Запис результату без них
неможливо повторити через тиждень.

### `llm.call`

`gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read_input_tokens`,
`gen_ai.usage.cache_creation_input_tokens`, `gen_ai.usage.reasoning_tokens`,
`provider.cost_usd`, `agent.loop_step`.

`output_tokens` — усі вихідні токени виклику, разом із міркуванням моделі;
`reasoning_tokens` — їхня частина, яку модель витратила на міркування і якої
немає у видимій відповіді. `provider.cost_usd` — ціна виклику з рахунку
провайдера (`usage.cost` від OpenRouter); у `mock`, `anthropic` і `openai`
його немає.

З `TRACE_CAPTURE_CONTENT=1` спан несе ще два атрибути:

| Атрибут | Що в ньому |
|---|---|
| `gen_ai.prompt` | JSON запиту до моделі: `system` (після підстановки `Request ref`), `messages` (саме те, що пішло в модель, разом із `(context replay)`), `tools` (назви інструментів) |
| `gen_ai.completion` | JSON відповіді: `text`, `tool_calls` з аргументами, `reasoning` (текст міркування, якщо провайдер його віддав) |

Запит на сьомому кроці діалогу — десятки кілобайтів, тому на спільному стенді
вміст типово вимкнений; локальний compose його вмикає.

`input_tokens` — усі вхідні токени виклику, разом із кешованими.
`cache_read_input_tokens` — їхня частина, прочитана з кешу провайдера;
`cache_creation_input_tokens` — частина, яку провайдер цим викликом записав у
кеш. Стенд позначає system prompt як кешований (`cache_control` для
`anthropic` і `openrouter`; `openai` кешує сам). Якщо префікс промпту однаковий,
з другого виклику `cache_read_input_tokens` дорівнює розміру промпту з
інструментами, а виклик коштує в кілька разів дешевше. Нуль на кожному виклику
означає, що щось змінює початок промпту між викликами.

Якщо оркестратор повторює крок, повтори видно як окремі спани `llm.call` із
власним номером у `agent.loop_step`. Повторюється **повний** оберт «модель →
інструмент», тому кожен повтор коштує токенів — це видно в сумі по ходу.

### `tool.<name>`

`tool.name`, `tool.arguments`, `tool.result`.

Аргументи й результат читаються **окремо**, і це принципово. Агент може
викликати правильний інструмент із правильними аргументами й отримати
результат, який не відповідає доменній істині. Може бути й навпаки. Перевірка
на текст відповіді не бачить ні того, ні того.

Для `tool.search_knowledge_base` додатково: `retrieval.query`,
`retrieval.index`, `retrieval.fragments` (id + score).

### `agent.summarize`

`summary.text` — текст зведення, яким замінено ранню частину діалогу;
`summary.replaced_messages` — скільки повідомлень згорнуто; `summary.input` —
транскрипт, який пішов у модель на стиснення (лише з
`TRACE_CAPTURE_CONTENT=1`). Стиснення — окремий виклик моделі, тому спан несе
ті самі `gen_ai.usage.*` і `provider.cost_usd`, що й `llm.call`. У
`gen_ai.usage.total_*` кореня і в `usage.cost_usd` відповіді `/chat` ці токени
не входять; у `usage.provider_cost_usd` — входять.

Цей спан з'являється лише коли досягнуто поріг згортки
(`GET/PUT /api/_test/summarize_after`).

## Ціна ходу: три числа

| Де | Що рахує |
|---|---|
| `usage.cost_usd` у відповіді `/chat` | основні звернення до моделі за прайсом `app/agent/pricing.py`; повтори D14 і стиснення не враховані |
| сума `llm.call` за прайсом | те, що порахує кожен, хто читає трейс: усі звернення, разом із повторами |
| `usage.provider_cost_usd` у відповіді `/chat` | сума `provider.cost_usd` усіх викликів ходу, разом із повторами і стисненням: рахунок провайдера, незалежний від стенду |

## Що бачить Langfuse

Стенд надсилає в Langfuse те саме дерево, що й у `traces.jsonl`, і додає
атрибути, які Langfuse розуміє. Нового SDK немає: це OTLP-ендпоінт
`<LANGFUSE_HOST>/api/public/otel/v1/traces` з ключами проєкту.

| У Langfuse | Звідки |
|---|---|
| тип вузла (agent, generation, retriever, tool) | `langfuse.observation.type` за назвою спана: `agent.request` → agent; `llm.call`, `agent.summarize`, `router.jev` → generation; `tool.search_knowledge_base` → retriever; інші `tool.*` → tool |
| тривалість | справжні `started_at` / `ended_at` спана |
| сесія, користувач, середовище, реліз, теги | `session.id`, `user.id` (= `session.customer_id`), `langfuse.environment` (= профіль), `langfuse.release` (= `STAND_RELEASE`), `langfuse.trace.tags` (профіль, дефекти, модель, `run.trace_tag`) — **на кожному спані**: Langfuse v4 зберігає спостереження, а не трейси, і без цього в генерацій порожня сесія |
| Input / Output генерації | `gen_ai.prompt` / `gen_ai.completion`; на `agent.summarize` — `summary.input` / `summary.text`; на корені — `request.message` / `response.answer` |
| токени за відрами | `langfuse.observation.usage_details`: `input` (без кешу), `input_cached_tokens`, `input_cache_creation_tokens`, `output` (без міркування), `output_reasoning_tokens`; сума вхідних відер = `gen_ai.usage.input_tokens` |
| ціна | з визначення моделі в Settings → Models. Стенд свою ціну пише в `stand.cost_usd` (метадані), а не в `gen_ai.usage.cost`: передану ціну Langfuse показав би замість своєї, і визначення моделі ні на що б не впливало |
| підсумки ходу | `gen_ai.usage.total_*` кореня йдуть в OTLP як `stand.usage.total_*`: інакше Langfuse прочитав би їх як токени кореня і порахував удруге поверх генерацій. У `traces.jsonl` і API назви ті самі |
| промпт | `langfuse.observation.prompt.name` = `base`, `...prompt.version` = номер базового промпту; оверлеї — у метаданих `prompt_version` |

Поки в проєкті немає визначення моделі, ціна генерацій у Langfuse — $0. Ціна
потрібна на кожне відро: відро без ціни Langfuse рахує як $0, і ціна генерації
виходить заниженою.

## Приклад: assertion поверх спанів

```python
import httpx

r = httpx.post("http://localhost:8000/chat",
               json={"session_id": "demo", "customer_id": "CUS-0004",
                     "message": "Open a dispute for TX-0401, duplicate charge"})
rid = r.json()["request_id"]
tree = httpx.get(f"http://localhost:8000/api/_test/traces/{rid}").json()

calls = [c for c in tree["children"] if c["name"].startswith("tool.")]

# дію виконано
assert any(c["name"] == "tool.create_dispute" for c in calls)

# без повторних однакових викликів
seen = [(c["attributes"]["tool.name"], str(c["attributes"]["tool.arguments"]))
        for c in calls]
assert len(seen) == len(set(seen))

# бюджет ходу
assert tree["attributes"]["gen_ai.usage.total_input_tokens"] < 20000
```

## Три класи перевірок, які видно лише в трейсі

**Розбіжність результату інструмента з доменною істиною.** Візьміть
`tool.result` і порахуйте те саме значення напряму через `app/engines/`.
Відповідь у тексті може бути правильною, бо агент перерахував її з інших полів
результату — а система при цьому в хибному стані.

**Порядок і склад викликів.** Що викликано, що не викликано, у якому порядку,
скільки разів. Формулюйте через **однакові виклики**, а не через точний список:
перевірка «жодних двох ідентичних викликів» ловить повтори й не червоніє від
безневинного зайвого спана.

**Відсутність спана.** Найважчий клас: дефект, який не лишає ні тексту, ні
рядка в базі. Якщо вимога каже «за таких умов має бути ескалація», то
перевірка — це `assert` на **наявність** відповідного спана, і вона червоніє
саме тоді, коли нічого не сталося.
