# Research UI: что использовать в SRF

Проверка 26.09.2026. Только первичные официальные страницы, исходники текущего SRF и публичная GitHub metadata. Использованы OpenAI Docs и Exa Search. `sources_reviewed=15`: три поиска Exa по 5 результатов; прямые fetch не добавляются в этот счётчик. Код, контейнеры и модели в этом исследовании не запускались; права, аккаунты и настройки не менялись. Статический обзор интерфейса по исходникам не является визуальным UX-тестом.

## Конкретное решение сейчас

Использовать **уже существующую исследовательскую консоль SRF**. В `web/index.html`, `app.js`, `styles.css` есть база источников, советники, моделирование, исследовательский цикл, окружение, журнал/экспорт. Отдельные страницы показывают brain/network/society/federation/harnesses. `workbench/server.py` предоставляет GET состояния/источников/результатов и POST добавления источника/запуска вычислений/цикла; HTML не является нарисованным макетом без API.

Сервер **принудительно привязан к 127.0.0.1**, проверяет Host/Origin и CSP `connect-src 'self'`. Это разумный существующий путь для разрешённого пользователем локального запуска без новых покупок. Он зависит от включённого ПК; запуск, порты и actual-response проверяет координатор. Нельзя объявлять его постоянным публичным cloud backend или снимать ограничения ради демонстрации.

Следующая минимальная интеграция UI: read-only панель **«Облачные исследования»** в этой консоли, с которой видно реальный workflow/run/commit, состояние model call, admission, попытки/успехи, next_due и причины ошибок. Данные берутся из фиксированных публичных GitHub ledger/receipt, показываются как текст, без исполнения generated candidate. Обязательны возраст снимка и явное `unknown/stale` при недоступности. Запуск нового исследования не должен обходить reserve/rolling quota.

Если нужен доступ при выключенном ПК, можно отдельно публиковать **публичную read-only копию этой панели** на [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/about-github-pages): это статический HTML/CSS/JS hosting. Вычисления продолжаются отдельными уже настроенными GitHub jobs. Pages не исполняет Python API и не хранит приватные токены; существующие формы записи нельзя просто скопировать туда как якобы рабочие. Публикация в этой подзадаче не выполнялась, availability Pages для репозитория не проверялась.

## Четыре внешних варианта

| Вариант | UI и работа с проектом | Машинная интеграция | Цена и решение для текущих 0 расходов |
|---|---|---|---|
| ChatGPT / Codex / Work | Проекты, инструкции, файлы и подключённые источники; local project даёт доступ к выбранным папкам | Skills/MCP для инструментов; UI при MCP при необходимости; отдельные API для моделей | Использовать существующий доступ в пределах квоты. Не считать consumer login бесплатным API или облачным доступом к локальным файлам |
| Claude | Project knowledge/instructions, диалоги, загрузка файлов; отдельный Research UI | Коннекторы и API требуют своего поддержанного пути и прав | Projects доступны Free до 5; Research — платные планы. Бесплатный проект подходит для рецензии, не подтверждает бесконечный автономный research service |
| Open WebUI | Свой chat UI, knowledge/RAG, модели, tools/functions | Bearer API, `/api/chat/completions`, upload/knowledge endpoints; Python plugins | ПО можно self-host бесплатно с соблюдением лицензии; отдельные compute/model/storage остаются нужны. После SRF, как необязательный фасад, а не новое ядро |
| Open Deep Research + Studio/OAP | Конфигурация исследователя, messages/Submit, LangGraph Studio, OAP GUI | Собственный LangGraph server, API, модели/поиск/MCP | Архивирован; default OpenAI/Tavily и публичный demo просит API keys. Не устанавливать как обещание бесплатного работающего research backend |

### OpenAI: применимые границы

Официальная [Projects and chats](https://learn.chatgpt.com/docs/projects) объединяет представление ChatGPT/local projects, но различает общий uploaded context и доступ к выбранным локальным папкам. Из этого не следует автоматическая синхронизация существующего SRF каталога со всеми облачными чатами. Нужно явно хранить версию project brief/commit и переносить её поддержанным способом.

Документация [plugin use cases](https://developers.openai.com/apps-sdk/concepts/user-interaction/) рекомендует skill для инструкций, MCP для live data/actions/auth и дополнительный UI только при полезной визуальной работе. Для SRF это будущий небольшой MCP-интерфейс `sources`, `runs`, `read_result`, а не экспорт всего диска/браузерного аккаунта. Write-actions требуют своих полномочий; MCP не превращает plugin в постоянный вычислительный сервер.

[Deep Research API](https://developers.openai.com/api/docs/guides/deep-research) использует Responses, допускает background, web/file search, remote MCP и code interpreter; обычный function calling для специальных deep research моделей не поддержан. [API pricing](https://developers.openai.com/api/docs/pricing) описывает отдельные оплачиваемые модели/инструменты. Нулевой неограниченный API из открытого ChatGPT аккаунта не подтверждён; в этом проекте новый OpenAI API вызов не включаем. Исходники полного proprietary UI/service ChatGPT в просмотренных материалах не опубликованы; доступные SDK/plugins не равны коду сервиса и не дают оснований обещать клон.

### Claude: что реально доступно без покупки

[Официальная инструкция Projects](https://support.claude.com/en/articles/9519177-how-can-i-create-and-manage-projects) подтверждает Free до пяти проектов, project knowledge и инструкции. В ней отдельно сказано, что информация не разделяется между чатами сама по себе, если не добавлена в knowledge. Для shared Team/Enterprise есть view/edit роли; это права внутри продукта, а не GitHub write credentials.

[Research](https://support.claude.com/en/articles/11088861-using-research-on-claude) доступен Pro/Max/Team/Enterprise и расходует общие лимиты быстрее обычного диалога. Не называть Free Web Search полноценным paid Research mode. [Claude API pricing](https://platform.claude.com/docs/en/about-claude/pricing) отдельно тарифицирует токены и часть server tools. Даже free code-execution allowance не делает inference бесплатным. Proprietary cloud UI/Research backend нельзя считать опубликованным кодом; в этой проверке такого официального репозитория не найдено.

### Open WebUI: реальный открытый код, но свой hosting

[Quick Start](https://docs.openwebui.com/getting-started/quick-start/) предлагает Docker или Python, persistent data volume и отдельный admin; работает с собственным backend и подключёнными моделями. [API](https://docs.openwebui.com/getting-started/api-endpoints/) требует Bearer credential и предоставляет модели/chat/files/knowledge. [Tools](https://docs.openwebui.com/features/plugin/tools/) — исполняемые Python toolkit, поэтому добавлять только собственный небольшой проверенный read-only adapter; не импортировать массово community plugins.

[Лицензия](https://docs.openwebui.com/license/) для v0.6.6+ содержит дополнительные требования к branding и исключения; это не безусловная MIT/BSD лицензия текущей версии. Сохранить Open WebUI branding — простой применимый путь. Не брать старый v0.6.5 только ради брендинга, игнорируя последующие исправления. Версия/digest образа для установки в этом исследовании не выбирались: готового нулевого постоянного хоста для дополнительного Python сервиса не установлено.

### Open Deep Research: первичный код и препятствия

Live GET [GitHub metadata](https://api.github.com/repos/langchain-ai/open_deep_research) вернул `archived=true`, default branch `main`, `pushed_at=2026-08-10T18:13:37Z`. Это не предположение по старой статье. [README](https://github.com/langchain-ai/open_deep_research/blob/main/README.md) документирует локальный API `127.0.0.1:2024`, Studio UI, OAP demo с собственными API keys и отдельные LLM для нескольких этапов. Модели должны поддерживать structured outputs и tools; бесплатный plain-text Qwen adapter SRF не объявляется совместимым без отдельной проверки. Open source здесь не означает предоставленную бесплатную инфраструктуру.

## Контракт для внедрения без дублирования оркестратора

1. SRF остаётся владельцем источников, provenance, Queue, admission и лимита; внешний UI не получает собственные дополнительные попытки модели.
2. Read-only cloud view принимает только фиксированный публичный repository, конкретный branch/head и ограниченный schema/size. Private account links/credentials в экспорт не входят.
3. Запись проекта — существующий локальный API/контрольный backend с авторизацией; статический сайт не получает GitHub token и не вызывает provider напрямую.
4. UI различает `configured`, `installed`, `protocol_tested`, `response_received`, `failed`; только квитанция текущего запуска подтверждает фактическую работу.
5. При будущем Open WebUI adapter сначала read-only результат SRF через проверенный endpoint, затем отдельно согласованный write scope. Model API, consumer UI и полномочия на проект остаются отдельными границами.

Проверяемый первый результат: существующая консоль открывается, читает API, показывает одну реальную cloud-квитанцию с SHA и свежестью; локальная форма сохраняет разрешённый источник; ни загрузка страницы, ни обновление статуса не создают inference calls. До такой проверки это предложение, а не объявленный деплой.
