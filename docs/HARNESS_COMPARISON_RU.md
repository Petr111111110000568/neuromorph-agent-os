# Unreal Agent и подключаемые альтернативы

Проверка первоисточников: **24 сентября 2026 года**. Для сравнения с Unreal Agent выбраны **Pi** как небольшой процессный harness и **OpenHands Software Agent SDK** как модульный исполнитель с готовой границей удалённого workspace. Codex SDK и Claude Agent SDK рассмотрены как дополнительные варианты. Выбор — инженерное решение для Meta-Harness, а не рейтинг качества моделей.

Обзор интерфейсов имеет статус `reviewed`. После него **Pi 0.87.1 установлен и его CLI проверен** в этом окружении, включая запуск через ограниченный process runner; доказательства — `examples/pi_validation.json`. OAuth-входы и запросы к внешним моделям в этой проверке не выполнялись. Статусы `protocol_tested` и `live_authenticated` для Pi остаются `false`. Обнаруженная платформа не получает автоматически права на репозиторий и не становится участником проекта.

## Сравнение

| Harness | Полезная роль в Meta-Harness | Точка интеграции | Runtime и доступ к модели | Изоляция |
|---|---|---|---|---|
| Unreal Agent, Unreal Labs | Проверка эффективности асинхронного исполнения разнородных инструментов | Go library; отдельный runner; версии операций и журнал сессии | Go-сборка/бинарный runner; провайдер и авторизация задаются отдельно | Архитектура допускает удалённый manager операций; наличие процесса само по себе не является sandbox |
| **Pi, earendil-works** | Лёгкий сменный worker; управляемая сессия и поток событий | CLI JSONL/RPC или TypeScript SDK | Node.js **22.19+**; API key или поддерживаемый OAuth/provider login | Унаследованные права OS; контейнер/VM задаёт хост |
| **OpenHands Software Agent SDK** | Исполнитель программных исследований в изолированном workspace | Python SDK; Agent Server REST/WebSocket; TypeScript client | Python **3.12+**; согласованные версии SDK/tools; выбранная авторизация модели | LocalWorkspace либо Docker/remote Agent Server; инфраструктура нужна отдельно |
| Codex SDK / app-server | Использование уже выбранной экосистемы Codex и её событий/ограничений | TypeScript SDK; Python SDK; app-server JSON-RPC | TypeScript Node.js **18+**, Python **3.10+**; Codex login/разрешённая авторизация | Настраиваемые sandbox/approval policies; конкретный режим необходимо проверять |
| Claude Agent SDK | Исполнитель с инструментами, skills, MCP, subagents и hooks Claude | Python/TypeScript SDK; CLI subprocess | Python **3.10+** или Node.js **18+**; API key/поддерживаемая облачная авторизация | Permissions и hooks не заменяют защищённую среду выполнения |

Источники: [H01–H18]. Pi и OpenHands SDK имеют MIT-лицензию по своим текущим источникам. Unreal Agent также опубликован как MIT. OpenAI описывает Codex CLI/app-server/SDK как открытые компоненты; модельные сервисы остаются отдельными. Claude Agent SDK регулируется Commercial Terms Anthropic, кроме компонентов с собственной лицензией; его нельзя автоматически считать MIT-эквивалентом [H01, H10, H14, H16, H18].

## Pi: точный процессный контракт

Старый адрес `badlogic/pi-mono` при проверке перенаправил на **`earendil-works/pi`**. Текущий npm-пакет — **`@earendil-works/pi-coding-agent`**. README требует Node 22.19 или новее. Изменения владельца и имени пакета нужно учитывать при закреплении зависимостей [H01].

Для долгоживущего worker:

```text
pi --mode rpc --no-session
```

Команды и ответы — JSONL через stdin/stdout, разделитель **LF**. Пример собственного сообщения Meta-Harness:

```json
{"id":"mh-001","type":"prompt","message":"Изучи код модели и верни перечень проверяемых ограничений."}
```

Ответ `{"id":"mh-001","type":"response","command":"prompt","success":true}` означает принятие сообщения. Завершение следует определять по **`agent_settled`**, а не `agent_end`: после последнего ещё возможны автоматические повторы, compaction или отложенная работа. Командные ответы сопоставляются по `id`, поток событий читается непрерывно, stderr не смешивается с протоколом. Закрытие stdin запрашивает штатное завершение [H02].

Команды `steer` и `follow_up` имеют разные моменты доставки. У Pi steering поступает после текущего assistant turn и выполнения его tool calls; это не тот же механизм, что заявленная Unreal немедленная обработка steering во время фоновых операций. Для полного прекращения очереди Pi документация рекомендует **`clear_queue` перед `abort`**: сам `abort` может продолжить оставшиеся сообщения [H03].

Для разового анализа удобнее `--mode json`, который запускает задание и завершает процесс. Составленный для Meta-Harness ограниченный argv-профиль:

```text
pi --mode json --no-session --no-tools --no-extensions --no-skills
   --no-prompt-templates --no-themes --no-context-files
   --no-approve --offline --provider PROVIDER --model MODEL
```

Это одна команда с массивом аргументов, показанная на нескольких строках для чтения. `PROVIDER`, `MODEL` — параметры адаптера, не готовые значения аккаунта. **Текст задания передаётся через stdin**: позиционный аргумент вида `@path` Pi трактует как вложение, даже после `--`. Реальный runner также задаёт постоянный system prompt и запускает CLI через отдельно закреплённый Node. Установленный `pi --help` подтверждает приведённые флаги. `--no-extensions` отключает найденные/configured расширения, но явно переданные `-e` всё равно загружаются; адаптер не добавляет их из входа задания [H04, H21].

Ограничение инструментов уменьшает доступные действия модели, но **не ограничивает файловую систему на уровне ОС**. Даже чтение может обратиться к доступному процессу пути за пределами cwd. Официальная документация Pi прямо отделяет project trust, наблюдение за логами и выбор папки от security boundary. Для write/bash-профиля нужен внешний контейнер/VM с явно переданными файлами, сетью и секретами [H05].

Авторизация: `/login` предлагает методы конкретного провайдера и сохраняет credentials в приватном `auth.json`. Для CI документированы, например, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `COPILOT_GITHUB_TOKEN`; Bedrock/Vertex допускают ambient cloud credentials. Наличие поддерживаемого OAuth не означает автоматически доступную подписку текущему процессу. Не передавать ключ через аргумент `--api-key`, если он может попасть в журнал или список процессов [H06].

Встроить Pi в TypeScript можно через `createAgentSession()` из указанного пакета, затем `session.prompt()`, `session.getLastAssistantText()` и `session.dispose()`. Для непостоянной истории предусмотрен `SessionManager.inMemory()`. В Python-ядре Meta-Harness предпочтительнее отдельный процесс: он сохраняет независимость версий Node и Python [H07].

## Подтверждённая установка Pi

В поставке закреплены npm-версия **0.87.1**, upstream revision `f07218c4d4bbc12bef056a7058c3dd49dfe41abe`, SRI основного архива и полный `scripts/pi-package-lock.json`. Пять внутренних пакетов в upstream shrinkwrap не содержали integrity; значения SHA-512 получены из официального npm registry и добавлены в наш lock. Все разрешённые URL зависимостей относятся к `registry.npmjs.org`; lifecycle scripts у `@google/genai`, `esbuild` и `protobufjs` подавлены. README Pi прямо допускает обычную установку с `--ignore-scripts` [H01, H21].

```text
python scripts/bootstrap_pi.py
python scripts/bootstrap_pi.py --install --pin-registry
python scripts/bootstrap_pi.py --verify --pin-registry
```

Первая команда показывает план. `--install` выполняет `npm ci --ignore-scripts --omit=dev --no-audit --no-fund` в отдельной папке; существующую установку не перезаписывает. `--verify` проверяет сохранённые hashes перед запуском CLI. Runtime и npm cache остаются в `runtime/` и не входят в Git. Скрипт не выполняет login, не регистрирует аккаунты и не вызывает модель.

В текущем окружении проверены **Node 24.19.0 / npm 11.9.0**. Реальные `--version` и `--help` завершились с кодом 0, включая запуск через `workbench.harnesses.runner.capture` со свежим каталогом Pi, минимальным окружением, ограничением CPU 30 секунд, виртуальной памяти 16 GiB и Node heap 256 MiB. Виртуальный лимит нужен для резервирования адресов V8 и не является выделенной физической памятью. Это подтверждает работоспособность CLI под выбранным runner; проверка JSONL с моделью отдельно не выполнена.

`runtime/harnesses/pi-build.json` содержит SHA-256 Node, entrypoint и всего установленного дерева. `config/harnesses.json` закрепляет эти значения для текущего окружения. При установке на другом компьютере явный флаг `--pin-registry` после успешной проверки обновляет только запись Pi — путь Node и hashes из нового receipt; записи Unreal/OpenHands остаются прежними. Без этого флага инсталлятор не меняет реестр разрешённых исполняемых файлов. Чужое дерево не считается доверенным из-за совпадения имени пакета.

## OpenHands: точный SDK-контракт

На PyPI проверена версия **openhands-sdk 1.49.5**, опубликованная **23.09.2026**, с требованием Python >=3.12. Официальные инструкции требуют устанавливать `openhands-sdk` и `openhands-tools` вместе с одинаковой версией. Workspace/server — отдельные пакеты, которые при использовании также нужно согласовать. Это optional environment, не обязательные зависимости stdlib-ядра Meta-Harness [H08, H09].

Основная последовательность вызовов:

```text
LLM(model=MODEL, api_key=CREDENTIAL)
Agent(llm=llm, tools=[Tool(name=...)])
Conversation(agent=agent, workspace=WORKSPACE, callbacks=[on_event])
conversation.send_message(PROMPT)
conversation.run()
```

`LLM`, `Agent`, `Conversation`, `Tool` экспортируются из `openhands.sdk`. Изученные built-in tools: `TerminalTool`, `FileEditorTool`, `TaskTrackerTool`. Строка пути как workspace использует локальную машину; она не создаёт контейнер. Callback получает события; проверять нужно итоговое состояние и результат, а не только отсутствие исключения в `run()` [H11, H12].

Официальный remote-пример использует:

```text
from openhands.workspace import DockerWorkspace
with DockerWorkspace(server_image=IMAGE, platform=PLATFORM) as workspace:
    conversation = Conversation(agent=agent, workspace=workspace, callbacks=[on_event])
    conversation.send_message(PROMPT)
    conversation.run()
```

Для production-адаптера IMAGE должен быть закреплён по версии/digest. Параметры `host_port` и platform предусмотрены примером. Agent Server хранит канонический REST/WebSocket-контракт; его TypeScript client — отдельный слой. Вместо ручного изобретения endpoint-ов первый адаптер Meta-Harness может использовать официальный Python SDK [H12, H11].

Доступ к модели имеет несколько вариантов: прямой API key, OpenHands Cloud credential или документированный OpenHands метод **`LLM.subscription_login(vendor="openai", model=...)`**. Последний запускает отдельный OAuth-вход, кэширует данные в `~/.openhands/auth/` и обновляет токены. Это утверждение документации OpenHands; доступность для аккаунта Meta-Harness и конкретной модели не проверена. Оно не даёт права переносить credentials из текущего чата, а приведённый в документации старый default model не следует выдавать за актуальный лучший выбор [H13].

Уже существующий CI-пример OpenHands, рассмотренный в `AI_PLATFORM_INTEGRATION_RU.md`, действительно требует `LLM_API_KEY`; это требование конкретного workflow, а не всех режимов SDK. Subscription login — отдельный путь, который нельзя считать включённым без проверки.

## Дополнительные варианты

**Codex:** актуальная документация предлагает `@openai/codex-sdk` для TypeScript: `new Codex() → startThread() → thread.run(prompt)`. Python-пакет `openai-codex` предоставляет `Codex`/`AsyncCodex`, `thread_start()` и `thread.run()` поверх локального app-server JSON-RPC, включая закреплённый runtime. Предусмотрены `Sandbox.read_only`, `workspace_write`, `full_access`. Старый `codex mcp-server` удалён — для интеграции требуется app-server [H15]. OpenAI документирует ChatGPT login для локального Codex и API-key path для программных CI workflows; фактическое разрешение текущего аккаунта необходимо проверять отдельно [H17].

**Claude:** Python `query(prompt=..., options=ClaudeAgentOptions(...))` возвращает асинхронный поток, TypeScript использует `query({prompt, options})`. SDK запускает Claude Code binary; официальный quickstart различает `allowed_tools`/`allowedTools` и `permission_mode`/`permissionMode`. Python и TypeScript пакеты обычно включают native binary, но некоторые способы установки его исключают. Документированы `ANTHROPIC_API_KEY` и облачная авторизация. Предлагать пользователям сторонней платформы claude.ai login/лимиты без предварительного разрешения Anthropic документация не допускает [H18, H19].

Для первого расширения Meta-Harness Pi даёт наиболее простой общий subprocess-контракт, а OpenHands — наиболее прямой переход к отдельно обслуживаемому sandbox workspace. Codex/Claude полезны при уже оформленных доступах соответствующего провайдера. Само подключение нескольких harness не создаёт независимых научных экспертов: нужно сохранять фактическую модель, данные и общий источник каждого ответа.

## Приёмка адаптеров Meta-Harness

Предлагаемые проверки — наш инженерный план. Они не являются уже пройденными испытаниями upstream:

1. Запуск зафиксированной версии и capability probe без ключа не должны обращаться к модели или раскрывать credentials.
2. Настроенный worker должен принимать ограниченное задание, выдавать поток/итог, корректно различать отказ авторизации, ошибку модели, отмену и завершение.
3. Отмена проверяется во время инструмента и при queued follow-up; преждевременное принятие `response.success` или `agent_end` не считается завершением.
4. Сохраняются версии harness/модели, параметры, хеш входа, ссылки на артефакты, exit status и реальные счётчики usage. Если стоимость не сообщается, записывается `unknown`, а не ноль.
5. Детерминированные проверки модели и тесты платформы выполняются независимо от текстового self-report агента. Изменения кода проходят через отдельную ветку/PR; результаты вычислительного исследования не смешиваются с доказательствами биологического эффекта.

## Как интерпретировать заявление Unreal о стоимости

В публикации Unreal Labs от **22.09.2026** заявлено снижение стоимости **до 40% против Codex** и до 20% против Pi. В приведённом Terminal-Bench 4.0 использована GPT-6 Astra с xhigh: Unreal и leaderboard baseline Codex показывают 57.9% при $1428 и $2350 соответственно; таблица Pi сообщает 55.0% и $1827. Сравнение связано с указанными задачами, моделями, настройками и методом расчёта автора. В других таблицах различаются и стоимость, и pass rate. **Это vendor-reported benchmark, не измеренная экономия Meta-Harness и не гарантия 40% для всех исследований.** Мы не воспроизводили эти расходы или полные benchmark traces [H20].

## Первоисточники

- **H01:** [Pi README, runtime, package, MIT](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md).
- **H02:** [Pi RPC framing и lifecycle](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md).
- **H03:** [Pi RPC commands](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-commands.md).
- **H04:** [Pi CLI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md).
- **H05:** [Pi security model](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md).
- **H06:** [Pi provider authentication](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md).
- **H07:** [Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md).
- **H08:** [OpenHands SDK package metadata](https://pypi.org/project/openhands-sdk/).
- **H09:** [OpenHands getting started](https://docs.openhands.dev/sdk/getting-started).
- **H10:** [OpenHands Software Agent SDK MIT license](https://github.com/OpenHands/software-agent-sdk/blob/main/LICENSE).
- **H11:** [OpenHands SDK README и границы компонентов](https://github.com/OpenHands/software-agent-sdk/blob/main/README.md).
- **H12:** [OpenHands DockerWorkspace example](https://github.com/OpenHands/software-agent-sdk/blob/main/examples/02_remote_agent_server/02_convo_with_docker_sandboxed_server.py).
- **H13:** [OpenHands LLM subscriptions](https://docs.openhands.dev/sdk/guides/llm-subscriptions).
- **H14:** [Unreal Agent repository](https://github.com/unreallabsai/unreal-agent).
- **H15:** [Codex SDK, official OpenAI documentation](https://learn.chatgpt.com/docs/codex-sdk).
- **H16:** [OpenAI: Codex as a platform](https://developers.openai.com/blog/codex-as-a-platform).
- **H17:** [OpenAI: Authentication](https://learn.chatgpt.com/docs/auth).
- **H18:** [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview).
- **H19:** [Claude Agent SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart).
- **H20:** [Unreal Labs: Unreal Agent, 22.09.2026](https://unreallabs.ai/blog/unreal-agent/).
- **H21:** [Официальные npm metadata Pi 0.87.1](https://registry.npmjs.org/@earendil-works/pi-coding-agent/0.87.1), поставленный README и CLI docs из архива с проверенным SRI. Фактическая локальная проверка: `examples/pi_validation.json`.
