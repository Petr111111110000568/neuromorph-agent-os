# Публичный API официального Qwen Space

Дата проверки контракта: 25 сентября 2026. Модуль `workbench.autonomy.qwen_space` делает один запрос к официальному [Qwen/Qwen3-Demo](https://huggingface.co/spaces/Qwen/Qwen3-Demo). Это отдельный анонимный Gradio API: браузерный вход, cookies, Hugging Face token и пользовательский DashScope key не используются.

**Уровень проверки:** публичные метаданные, исходник и API schema прочитаны; GET-only preflight адаптера прошёл. Инференс при разработке адаптера не выполнялся. Поэтому работающий upstream и получение реального ответа ещё требуют облачной проверки. Demo может остановиться, отклонить запрос или исчерпать собственную квоту.

## Закреплённый контракт

| Свойство | Подтверждение |
|---|---|
| Space и приложение | `Qwen/Qwen3-Demo`, `https://qwen-qwen3-demo.hf.space` |
| Репозиторий и runtime revision | `60e1db0778067d36b8a2793c350bf85cd461a298` |
| Gradio | `5.27.0`; `api_prefix=/gradio_api`; `protocol=sse_v3` |
| Модель | `qwen3-235b-a22b`, серверный DashScope alias. Точный checkpoint модели не раскрыт. |
| Thinking | Новый `gr.State` включает thinking. Адаптер задаёт `thinking_budget=1`, сервер умножает его на 1024. |
| Генерация | `add_message()` напрямую итерирует `submit()`, который вызывает `Generation.call(...stream=True, enable_thinking=..., thinking_budget=...)`. |
| Веб-поиск | В проверенном вызове отсутствует; адаптер его не заявляет и не включает. |
| Системный prompt | Поле передаётся ради совместимости формы, но в этой версии `format_history()` не добавляет system message. Нельзя полагаться на это поле как на действующую инструкцию. |
| Квота | Не опубликована в проверенном контракте. `cpu-basic` здесь обслуживает proxy к серверному DashScope; пользовательская HF ZeroGPU квота к нему не относится. |

Исходники: [app.py](https://huggingface.co/spaces/Qwen/Qwen3-Demo/blob/60e1db0778067d36b8a2793c350bf85cd461a298/app.py), [config.py](https://huggingface.co/spaces/Qwen/Qwen3-Demo/blob/60e1db0778067d36b8a2793c350bf85cd461a298/config.py), [thinking_button.py](https://huggingface.co/spaces/Qwen/Qwen3-Demo/blob/60e1db0778067d36b8a2793c350bf85cd461a298/ui_components/thinking_button.py). Перед POST адаптер проверяет metadata/runtime revision, SHA256 этих трёх файлов и фактическую конфигурацию endpoint. Исходники читаются как байты и никогда не исполняются.

## Почему в HTTP четыре input и семь output

`/gradio_api/info` показывает два пользовательских параметра и два возвращаемых компонента. Однако сырой API Gradio 5.27 сохраняет позиции компонентов состояния и UI. Проверенный `/config` определяет input IDs `[33,38,60,1]`, output IDs `[33,56,22,15,20,29,1]`. В `data` добавляются два `null`; `gr.State` сервер заполняет из новой сессии. История находится в output с индексом **5**.

Это следует из [Gradio 5.27 blocks.py](https://github.com/gradio-app/gradio/blob/gradio%405.27.0/gradio/blocks.py): `validate_inputs` проверяет полное число input, `preprocess_data` подставляет state, `postprocess_data` сохраняет полное число output. [routes.py](https://github.com/gradio-app/gradio/blob/gradio%405.27.0/gradio/routes.py) передаёт сырой список в SSE. Общий POST → event ID → SSE GET описан в [официальном руководстве](https://gradio.app/guides/querying-gradio-apps-with-curl).

Ниже **пример контракта, а не запись выполненного inference**:

```http
POST https://qwen-qwen3-demo.hf.space/gradio_api/call/add_message
Content-Type: application/json
```

```json
{
  "data": [
    "Summarize the public metadata supplied here; label uncertain claims.",
    {
      "model": "qwen3-235b-a22b",
      "sys_prompt": "You are a helpful and harmless assistant.",
      "thinking_budget": 1
    },
    null,
    null
  ],
  "session_hash": "77777777777777777777777777777777"
}
```

Первый ответ имеет форму `{"event_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`. Следующий GET: `https://qwen-qwen3-demo.hf.space/gradio_api/call/add_message/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`. Встречаются SSE события `generating`, `heartbeat`, `error`, `complete`. Минимальный пример структуры завершения, с опущенными необязательными UI-полями:

```text
event: complete
data: [{"__type__":"update"},{"__type__":"update"},{"__type__":"update"},{"__type__":"update"},{"__type__":"update"},{"__type__":"update","value":[{"role":"user","content":"Public prompt"},{"role":"assistant","status":"done","loading":false,"content":[{"type":"text","content":"Unverified answer."}]}]},null]

```

Адаптер принимает только `complete`, извлекает текст последнего assistant message, не возвращает thinking UI-блоки, не открывает ссылки из ответа и ничего не исполняет. Ответ сохраняется как `unverified=true`, с hash ответа, prompt и provenance. Завершение SSE само по себе не подтверждает факты модели.

## Ограничения клиента

- `call_qwen_space(prompt, transport=None)` принимает только явно публичный текст до 4000 символов. Простая проверка известных форматов ключей — дополнительная защита, а не универсальный классификатор секретов. Не передавайте личные данные, репозиторные secrets или непубличные документы.
- В обычном режиме сеть работает в дочернем процессе: 119 секунд на работу и до одной секунды на завершение/очистку. Это охватывает DNS и медленные сетевые чтения. Нет фонового продолжения бесконечного запроса.
- Socket timeout SSE — до 30 секунд, чтобы дождаться Gradio heartbeat с периодом 15 секунд; preflight/POST имеют предел 10 секунд на операцию. Общий предел дочернего процесса сохраняется.
- До 192 KiB суммарно для preflight, до 256 KiB для SSE, до 64 KiB извлечённого ответа. Запрос генерации только один, его результат читается одним GET; retries/fallback отсутствуют. Redirects, cookies, proxy из окружения и авторизация отключены.
- `requests` и `request_count` — число попыток inference POST, 0 или 1. `http_requests` включает чтения preflight. Неизвестное состояние убитого дочернего процесса считается одной попыткой; повтор такого запроса автоматически не выполняется.
- `max_tokens` сервер в этой версии не передаёт. Ограничения клиента не являются жёстким лимитом количества серверных output tokens.
- Публичный `/call/cancel` вызывает UI-функцию; её `cancels` привязан к отдельному скрытому dependency. Она не доказана как самостоятельная удалённая отмена. Адаптер её не вызывает и не использует недокументированный `/cancel`. Завершение локального процесса **не гарантирует** прекращение уже начавшегося upstream inference.
- Ограничение «не чаще одного запроса в 6 часов, до 28 попыток» относится к отдельному сохраняемому scheduler. Прямой вызов функции не реализует эту периодическую квоту.

Модельные aliases и endpoint фиксированы; пользователь не может подставить произвольный URL или credential через параметры. Цена пользователю не передаётся через этот анонимный запрос; это не обещание неограниченного бесплатного сервиса. При изменении источника или schema требуется новый review, а не автоматическое обновление pin.

## Проверка и облачный запуск

GET-only preflight: **5 GET, 114931 bytes, 0 inference POST**; SHA256 `/config` — `67d6139f20128ed7bf6d33dc5862236ebbce8efd2152df1fe3f2e01d0b419e67`.

До последнего уточнения пользователя 11 offline tests прошли. Затем добавлены тесты parser для реальной глубины Gradio schema и ожидания SSE heartbeat: сейчас 13 тестов адаптера, новое состояние локально не запускалось. Следующий полный прогон и live inference выполняются в Colab/GitHub по поручению пользователя. Тесты не используют действующие credentials и не обращаются к модели.

Production-функция использует `multiprocessing` spawn; в собственном исполняемом Python-скрипте вызывайте её внутри `if __name__ == "__main__":`. Сам импорт модели не запускает. Публиковать/исполнять полученный текст как код эта функция не умеет.
