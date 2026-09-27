# M02-STUDIO-001: закреплённые результаты облачного эксперимента в Studio

Родитель — PR18; исходный commit `ceae671452365a84dcff2787b4ac66d7372a4f13`. Модуль `workbench/m02_receipts.py` добавляет библиотеку **двух исторических квитанций** и их неизменяемые связи с проектами владельца в Studio. Импорт не запускает диспетчер, не восстанавливает SQLite checkpoint и не вызывает сеть или модель.

Принятые источники:

- `m02-seed-36277624672`: [облачный run A](https://github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/36277624672), резерв 4, фактическая попытка 1, принята одна задача.
- `m02-resume-36277674850`: [облачный run B](https://github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/36277674850), резерв 6, фактических попыток 3, приняты две задачи; сохранён предыдущий run ID и отдельный SHA256 checkpoint manifest.

Это законченная синтетическая инженерная проверка. Она не является научной валидацией или новым подтверждением доступности провайдера/GitHub. Обе квитанции сообщают о 0 модельных вызовов и дополнительных расходах 0.

## Корень доверия и представление

`config/m02_receipt_registry.json` в принятом checkout отдельно закрепляет identity и **SHA256 канонического полного JSON** каждого файла `data/m02_receipts/{seed,resume}.json`. Эти файлы скопированы из публичных облачных receipts без ручного изменения результатов. Runtime проверяет exact repository/workflow/main/commit/run, схему, ограничения размера/глубины/числа полей, отсутствие duplicate keys и нечисловых/неограниченных значений. Проверяется связь `resume.initial_state == seed.state` и общий input hash. Отсутствие/изменение файла, неизвестная схема или неподходящий registry дают `receipt_library_unavailable`/503; новый бюджет или новые данные автоматически не создаются.

Операторский `root` задаётся приложением при создании библиотеки. HTTP-клиент не может задавать root, path, URL, owner, receipt body, registry или доверенный digest. Собственный корректный SHA произвольного JSON не предоставляет допуска. Хеш доказывает совпадение с принятым registry, а не подлинность неизвестного автора и не истинность научного утверждения.

Каталог выдаёт `canonical_sha256` исходного полного файла и ограниченную публичную **проекцию** в `receipt`, помеченную `receipt_representation: bounded_projection`. Проекция содержит identity, lineage, input hash, бюджет, статусы задач и решения review. В ней нет SQLite file hashes, transitions/outbox или внутренних lease/token/digest. Поэтому SHA проекции **не должен** сравниваться с `canonical_sha256` полного источника. `previous_manifest_sha256` — ещё один отдельный digest, относящийся к checkpoint manifest, а не к JSON квитанции. Summary содержит только счётчики, без hash/lease полей.

## API и хранение

`M02ReceiptLibrary(store, root=None)` создаёт только таблицу связей. Файлы проверяются лениво при `snapshot()` или `import_receipt()`, включая повторный запрос; создание общего Service с отдельным пустым fixture root не требует этих файлов.

`snapshot(owner='local')` возвращает `{catalog: [...], imports: [...], capabilities: {...}}`. Источники публичны, но `imports` ограничены владельцем и действительным Studio-проектом. `historical=true`, `live_verification=false`, `execution=false`, `network_calls=false`, `model_calls=false`, `scientific_validation=false` задают честную границу результата.

`import_receipt(data, owner='local')` принимает **ровно** `{project_id, receipt_id, idempotency_key}`. Owner приходит от текущей серверной сессии. В одной `BEGIN IMMEDIATE` транзакции проверяются project owner и `kind='project'`, затем уникальные ограничения `(owner,request_key)` и `(owner,project_id,receipt_id)`; записывается неизменяемая association в `studio_m02_imports`. Обычные `studio_records`/artifacts и их события не изменяются. Импорт не закрывает научную задачу и не повышает её статус evidence.

Повтор идентичного request/key возвращает прежнюю запись, в том числе после restart. Другой project или receipt с тем же key даёт `idempotency_conflict`/409. Та же association с **новым** key даёт `receipt_already_imported`/409: клиент обновляет imports вместо создания незафиксированного alias. Чужой project либо task/artifact вместо project дают `not_found`/404. До 4000 associations на owner; immutable records не удаляются для освобождения квоты. Edit endpoint отсутствует.

Проверки покрывают два независимых SQLite connections, повторы после reopen, чужого владельца, неправильный kind, изменённый registry/file, самоподписанный пользовательский POST, malformed/oversized JSON, symlinks и отсутствие изменения обычных artifacts. Все тесты запускаются только в разрешённом облаке; наличие этого описания не является утверждением об их успешном выполнении.
