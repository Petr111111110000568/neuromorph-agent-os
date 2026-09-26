# M02: конечный диспетчер двух проектов

Родитель: CHATGPT-REVIEW-001 / CLAUDE-MOD-005. Исходный commit: `2b731fbba92fc8427b7296f80579e9d6a8e02e9c`. Реализация следует [контракту](M02_DISPATCHER_CONTRACT_RU.md). Это инженерный эксперимент с фиксированными JSON-результатами: **0 сетевых, модельных и платных вызовов**. Он не является подключением реальных научных исполнителей или непрерывной сетью всех чатов.

## Интерфейс

`ProjectDispatcher(control_path, queue, failpoint=None)` получает существующую `workbench.network.queue.Queue`. Очередь должна быть отдельной для M02: её fixture payload не предназначен для production `network.worker`. Закрывает очередь её владелец; `dispatcher.close()` закрывает только control DB.

- `create_project(spec)` / `create_task(spec)` принимают закрытые схемы из `config/m02_fixture.json`, до 16 KiB канонического UTF-8 JSON каждая. `allowed_artifacts` должен явно разрешать фиксированный класс `synthetic-evidence`; иначе проект отклоняется до создания/dispatch. Идентичный повтор возвращает ту же карточку; другой вход с прежним ID отклоняется. Полный проектный и task-контракт входят в SHA256, а `handoff_id` навсегда связан с конкретной revision и input hash. Зависимость должна уже существовать: порядок создания образует DAG. Project spec и его revision=1 в этой версии неизменяемы: CAS project revision не заявляется как API редактирования проекта.
- `tick()` сверяет cancellation/dispatch intents и завершённые Queue jobs, затем резервирует и отправляет не более одной новой готовой задачи. Возвращает `dispatched`, `progressed` или `waiting` и конкретную причину. Исполнитель внутри tick не запускается.
- `run_fixture_once(queue, worker_id="m02-fixture", failpoint=None)` делает не более одного настоящего `Queue.claim/finish`. Допущены только три fixture ID, закрытая схема и точное соответствие immutable contract. Ни команд, ни динамических handlers, ни URLs для загрузки кода.
- `review(task_id, expected_revision=..., result_sha256=..., decision=..., note=...)` привязывает решение к точному текущему artifact. `accepted` закрывает задачу. `rejected` / `inconclusive` требуют замечания и создают детерминированный handoff revision 2 с родительской ссылкой. Повтор review идемпотентен; противоречащее повторное решение отклоняется. Повторный отказ на R2 оставляет `exhausted`, без дальнейшего rework.
- `revise(task_id, expected_revision=..., handoff_id=..., reason=..., goal=None, fixture_id=None)` — явное действие координатора, ограниченное переходом R1→R2. Ресурсы и зависимости оно не изменяет.
- `cancel(task_id, expected_revision=..., reason=...)` сначала фиксирует cancellation intent. После crash очередной tick завершает отмену. Возможное начатое вычисление не получает право публикации.
- `set_channel_available(project_id, bool)` меняет доступность synthetic канала по действию оператора. Результат worker не может её менять. Заблокированный проект не препятствует независимому готовому проекту.
- `deliver_outbox()` доставляет конечные неизменяемые события в локальный ledger с уникальным event ID. Это не публикация в Git/HTTP; внешний mutable pointer требует отдельного CAS-протокола. Повтор уже доставленного события не создаёт второй эффект. Устаревшее pending событие получает `superseded`.
- `snapshot()` возвращает публичные карточки, intents, artifact hashes/contents, reviews, outbox, cancellation intents, общий резерв и фактические Queue attempts. `events` хранит причины реального выбора/резерва и переходы queue binding, promotion/stale, review, revision, cancellation и delivery. Не более 256 уникальных событий; ожидание и повтор уже совершённого действия не добавляют записи. При достижении предела дальнейший переход отклоняется, история не удаляется. Lease tokens и digests не экспортируются.

## Границы атомарности

До `Queue.submit` control transaction сохраняет неизменяемый intent и резервирует **сразу 2 попытки**. Общий лимит — **8**; максимум 2 проекта, 8 задач, один исполняющийся/ожидающий review handoff на проект. Резерв вычисляется по сохранённым intents и никогда не возвращается при ошибке, отмене, stale result или crash. Отдельная Queue учитывает каждую claim, включая потерянные аренды. Диспетчер не создаёт новых leases и не меняет Qwen ledger.

Две базы не образуют общей транзакции. Crash после enqueue восстанавливается по тому же idempotency key и проверке полного Queue payload/capabilities/max_attempts. Crash после finish обнаруживает тот же completed job; нового вычисления для восстановления не требуется.

Артефакт допускается только при полном совпадении результата с закреплённым fixture. Следующая control transaction одновременно проверяет project/task/handoff/input hash/revision/status, выполняет условный UPDATE текущего указателя и создаёт publication outbox. Если между чтением Queue result и этой транзакцией записана R2, R1 сохраняется в истории Queue и получает `stale_for_publication`, не меняя текущий указатель и не возвращая резерв.

Для проверки доступны доверенные callback-точки `after_intent_reserved`, `after_queue_submit`, `after_queue_finish` (worker), `before_promotion`, `after_control_promotion`, `after_cancel_intent`, `after_queue_cancel`, `before_outbox_delivery`. Они не загружаются из spec или ответа агента. Callback с `os._exit` проверяет настоящий разрыв процесса; `before_promotion` позволяет воспроизвести конкурентный R1→R2.

Отдельно проверяются первый ожидаемый бизнес-эффект и `B(S2)=B(S1)` после повтора. Полное состояние SQLite не обязано совпадать: Queue может выполнить reap, а диагностический duplicate counter измениться. `B` включает immutable job contract/ID, попытки и резерв, текущие artifact pointers и уникальные outbox event ID/hash; timestamps и кеш привязки job ID не являются новым исполнением.

## Воспроизведение

Полный suite и focused integration tests запускаются только в GitHub Actions/Colab. Разработка этого модуля не исполняла локально Queue-сценарий или модель. Для двух cloud run необходимы exact producer run ID, workflow/repo/branch/commit и SHA manifest; непроверенный или отсутствующий checkpoint должен завершать resume отказом, без bootstrap с новым лимитом. Перед копированием обеих SQLite DB следует закрыть соединения либо использовать SQLite backup API; основной файл при живом WAL не является корректным checkpoint.

Облачные receipts отдельно фиксируют проверенный commit, исходный run и manifest, actual attempts/reservations, review outcomes и ограничения. Успех synthetic fixture не доказывает истинности научного утверждения, доступности внешнего провайдера или исполнения произвольного подпроекта.
