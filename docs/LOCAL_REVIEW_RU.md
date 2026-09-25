# Конечный цикл локального LLM review

`python -m workbench.autonomy.local_review` работает отдельным Python-процессом без продолжающегося диалога Codex. Это чтение публичного discovery-каталога и формирование **непроверенных текстовых предложений**. Код модели, предложения и команды из входных данных не исполняются. Discovery daemon не изменён; его сетевые обращения остаются отдельным процессом. Облачные inference-вызовы по-прежнему запрещены `resource_policy.json`.

Нужны уже существующие файлы в `runtime/local-model`: `llama-cli.exe`, все DLL его bundle и полный GGUF. Manifest создаётся после проверки загрузки; он не скачивается и не формируется этим модулем. Пути внутри manifest задаются от корня репозитория с `/`:

```json
{
  "schema_version": 1,
  "runtime": "llama.cpp",
  "executable": {"path": "runtime/local-model/llama-b11146-vulkan/llama-cli.exe", "sha256": "<64 lowercase hex>"},
  "model": {"path": "runtime/local-model/Qwen3-14B-Q4_K_M.gguf", "sha256": "<64 lowercase hex>"},
  "dependencies": [{"path": "runtime/local-model/llama-b11146-vulkan/llama-cli-impl.dll", "sha256": "<64 lowercase hex>"}]
}
```

Пример показывает одну DLL; рабочий `dependencies` обязан перечислить **все** DLL в дереве каталога exe. Перед первым запуском процесса сверяются SHA-256 всех файлов, непустой GGUF и его заголовок; затем проверяются изменения размера/mtime перед каждой попыткой. Manifest — локальное решение доверия оператора, не криптографическая подпись издателя. Неизвестные поля, ссылки/reparse points, неподходящие пути и незакреплённые DLL блокируют запуск.

Профиль зафиксирован по фактическому `--help` llama.cpp **b11146**, Vulkan0: `--offline`, `-m`, `-f`, `-st`, `--simple-io`, `--no-display-prompt`, `-n`, `-c 4096`, `-ngl 99`, `--device Vulkan0`, `-fa on` и фиксированные параметры sampling. Prompt заканчивается `/no_think`. Произвольные CLI-флаги и другие executable names не принимаются. `--no-webui` не добавлен: используется single-turn CLI, а не сервер. На компьютере без Vulkan0 этот профиль нужно отдельно адаптировать и проверить; скрытого CPU/облачного fallback нет.

Из корня репозитория, после создания каталога `runtime/local-review`, можно явно запустить ограниченный job:

```powershell
python -m workbench.autonomy.local_review --executable runtime/local-model/llama-b11146-vulkan/llama-cli.exe --model runtime/local-model/Qwen3-14B-Q4_K_M.gguf --manifest runtime/local-model/manifest.json --intake runtime/continuous-discovery/cycle.json --output-dir runtime/local-review --state-file runtime/local-review/state.json --stop-file runtime/local-review/STOP --max-runs 24 --interval 3600 --timeout 300 --max-tokens 192
```

`--once` выполняет не более одной уже наступившей попытки; повторный запуск с теми же аргументами продолжает тот же job. Верхние пределы: 168 попыток, интервал 3600–86400 секунд, timeout 1–300 секунд, генерация 1–512 токенов. Два OS-lock защищают checkpoint и каталог результатов. Контрольная запись сохраняется атомарно **до** вызова модели: авария может потерять результат, но не переигрывает ту же попытку. Смена модели, pin, лимитов или путей требует нового state и нового каталога результатов. Существующие `review-NNN.json` не перезаписываются. Состояние хранится на диске, запуск после перезагрузки ОС этим модулем не настраивается.

Создание `runtime/local-review/STOP` останавливает ожидание за ≤5 секунд; работающий дочерний CLI проверяется примерно каждые 0,1 секунды и завершается. Таймаут/ошибка также останавливают текущий цикл без автоматического retry; попытка уже учтена. Дочерний процесс запускается без shell, stdin закрыт, API-ключи и `LLAMA_ARG_*` из окружения не наследуются. HTTP-кода и инструментов в модуле нет; llama.cpp получает offline-флаг и локальные файлы. Это не OS firewall/контейнерная песочница; доверие закреплённому executable и DLL остаётся необходимым.

Intake принимает только `runtime/continuous-discovery/cycle.json`: максимум 192 KiB JSON. В prompt попадают лишь `id`, `name`, `source_url`, `provider` для до 12 записей с `public_catalog_metadata_only`, допустимым provider/публичным HTTPS-доменом, без URL credentials/query/fragment; metadata ограничены 8000 UTF-8 bytes. Явно непубличные записи и прочие поля (credentials, private paths, instructions, проекты, тела документов) отбрасываются. Текст каталога всё равно недоверенный; prompt-инструкция не делает его независимым доказательством.

Ответ ограничен 32 KiB, сохраняется атомарно как `unverified_proposal` с `validated:false`, `execution_allowed:false`, digest модели/exe/input и использованной metadata. Код предложения не устанавливается и не исполняется. stdout/stderr дочернего процесса контролируются по размеру и времени; stderr не переносится в отчёты. Локальная работа не расходует API-кредиты, но использует электричество, GPU/RAM и время ПК.

Проверка реализации: `python -m unittest discover -s tests -p test_local_review.py -v`. Тесты используют миниатюрные pin-fixtures/fake runner, проверяют утечку приватных полей, bounds, timeout, stop, resume/crash, digest/DLL mismatch, сохранение прежнего результата и реальную OS-lock конкуренцию. Они не заменяют отдельную проверку настоящей модели; модуль во время разработки не запускал inference.
