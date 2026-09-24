# Интеграция AI-платформ для развития Meta-Harness

Дата проверки: **24 сентября 2026 года**. Этот документ описывает проверенную документацию и условия подключения. Состояние конкретных запусков репозитория фиксируется отдельно в отчёте поставки; чтение документации не означает активации аккаунта или запуска внешнего агента.

## Основной результат исследования

Для личного GitHub-репозитория нельзя обещать бесплатную непрерывную работу внешних LLM только за счёт встроенного `GITHUB_TOKEN`. **GitHub Models полностью закрыт с 30 июля 2026 года**, включая каталог, inference API и BYOK [A01]. Старые инструкции с `permissions: models: read` и `models.github.ai` устарели. В новую работающую схему эти endpoints включать не следует.

Реальная автономность строится из двух независимо проверяемых частей: расписание и очередь задач проекта; затем авторизованный вычислительный исполнитель. Первая часть может собирать публичные метаданные, обновлять каталог, запускать собственные тесты и готовить задания без LLM. Вторая требует действующей подписки, разрешённого API-доступа или собственного model server. Запись названия платформы в реестр не создаёт эти права.

## Сопоставление вариантов

| Вариант | Что обеспечивает | Условия запуска | Статус этого исследования |
|---|---|---|---|
| GitHub Models | Ранее — модельный inference из Actions | Сервис закрыт | `reviewed_retired`; не использовать |
| Copilot cloud agent | Изучение кода, ветка, исправления, тесты, PR | Платный Copilot; доступ включён для пользователя и репозитория | `reviewed_not_configured` |
| Copilot agent tasks API | Программное создание и наблюдение задания | Пользовательский PAT/OAuth/GitHub App user token; installation token не подходит | `reviewed_contract_not_live_tested` |
| GitHub Agentic Workflows, personal repo | Markdown-задачи, компиляция в Actions, выбранный агент | Учётные данные соответствующего engine; для Copilot — отдельный PAT | `reviewed_not_installed` |
| GitHub Agentic Workflows, organization repo | Аналогичный цикл, с оплатой организации | Для Copilot возможен `GITHUB_TOKEN` с `copilot-requests: write`; нужна настроенная организация | `reviewed_not_configured`; это не бесплатный Models |
| Copilot native automations | Расписание и события без собственного диспетчера | Платный Copilot; только private/internal repository | `reviewed_not_configured`; неприменимо к публичному репозиторию |
| Codex cloud через ChatGPT | Облачная работа с подключённым репозиторием; PR и review | Подключение GitHub, выбор repo, создание environment; настройка review отдельно | `reviewed_not_configured` в рамках этого исследования |
| Codex GitHub Action | Воспроизводимые задания Codex из Actions | `OPENAI_API_KEY`, runner и разрешения | `reviewed_not_configured` |
| Claude/Codex как агенты GitHub | Задание из Agents/issue/PR, изменения в ветке | Платный Copilot и включённые политики конкретного агента | `reviewed_not_configured` |
| OpenHands Software Agent SDK | Собственный агент и workflow обслуживания кода | Установка SDK, модельный endpoint и авторизация; в официальном примере `LLM_API_KEY` | `reviewed_not_installed` |
| Внешний участник Meta-Harness | Получает разрешённое задание, возвращает результат по контракту | Согласие оператора, аутентификация и доступный шлюз | Контракт существует в проекте; сторонний участник этим исследованием не привлечён |

Сведения о продуктах: [A02–A10]. Наличие подписки ChatGPT само по себе не подтверждает подписку GitHub Copilot или кредит API у другого провайдера. Состояние возможностей проверяется у каждого сервиса отдельно.

## Минимальный текущий контракт Copilot

Официальный API находится в public preview. Он поддерживает **user-to-server tokens**: PAT, OAuth token либо GitHub App user token. GitHub App installation tokens не поддерживаются [A03]. Встроенный токен Actions является installation token [A11]; поэтому его нельзя просто подставить вместо пользовательского токена в этот API.

```text
POST https://api.github.com/agents/repos/{owner}/{repo}/tasks
Authorization: Bearer <user-to-server token>
Accept: application/vnd.github+json
Content-Type: application/json

{"prompt":"Одна задача с критериями приёмки", "base_ref":"main", "create_pull_request":true}

GET https://api.github.com/agents/repos/{owner}/{repo}/tasks/{task_id}
```

Обязателен `prompt`; `base_ref`, `model`, `create_pull_request` опциональны. Возвращённый task ID следует сохранять до дальнейшего наблюдения. Документированные состояния: `queued`, `in_progress`, `completed`, `failed`, `idle`, `waiting_for_user`, `timed_out`, `cancelled`. Выбор модели можно оставить провайдеру. Повторный POST после сетевого таймаута способен создать повторную задачу: перед повтором нужно сверять сохранённый журнал и список задач, не предполагая недокументированную идемпотентность.

API назначения issue — альтернативный путь. Проверка доступности через GraphQL `repository.suggestedActors(capabilities: [CAN_BE_ASSIGNED])` должна вернуть `copilot-swe-agent`. Для fine-grained PAT официально указаны metadata read и actions/contents/issues/pull requests read-write. Эти широкие права не следует выдавать простой операции обнаружения каталогов [A03].

## Три различающихся пути Codex

**Codex cloud:** подключить GitHub к Codex, выбрать репозиторий и создать environment. Затем задача выполняется в выделенной облачной среде [A06]. Эта авторизация принадлежит продукту Codex; её наличие не доказывается одним доступом GitHub-коннектора в текущем чате.

**Автоматический review:** включить Code review для подключённого репозитория в настройках Codex. Для автоматизации нужны push/admin permissions. Упоминание `@codex review` вызывает проверку, другое поручение в PR может начать облачную задачу [A07]. Оно не заменяет установку связи аккаунтов.

**Codex Action:** официальный `openai/codex-action` запускает CLI из CI; документированный быстрый запуск использует `OPENAI_API_KEY`. Секрет хранится в GitHub Actions Secrets, не в файле проекта. Следует закреплять версию/commit и использовать ограниченные разрешения среды [A08].

Это три способа доступа с разной авторизацией, а не три автоматически найденных независимых исследователя.

## Особенности GitHub Agentic Workflows и OpenHands

`gh aw` компилирует Markdown workflow в `.lock.yml`; документация называет его public preview. В personal repository Copilot engine требует `COPILOT_GITHUB_TOKEN` с `Copilot Requests: Read`. Для organization-owned repo предусмотрен встроенный `GITHUB_TOKEN` с `copilot-requests: write` и организационным биллингом. Другие документированные engines используют собственные секреты: Codex — `OPENAI_API_KEY`, Claude — `ANTHROPIC_API_KEY`, Gemini — `GEMINI_API_KEY` [A04]. Создание организации или добавление платёжных обязательств не является автоматическим следствием публикации проекта.

Native Copilot automations — другой продукт: они задаются через UI, хранятся вне Git, доступны в private/internal repositories и расходуют минуты Actions и AI credits. Публичный исходный репозиторий не следует переводить в private только ради включения этой возможности [A05].

Для OpenHands изучен актуальный официальный пример обслуживания репозитория в Software Agent SDK: prompt и параметры модели передаются workflow, секрет `LLM_API_KEY` требуется отдельно [A10]. Старый `OpenHands/openhands-github-action` архивирован 22 декабря 2025 года; его README полезен как исторический пример, но не выбран основой нового подключения [A12]. Просмотренный SDK не установлен и не запускался в этом исследовании. Ошибки чтения некоторых страниц docs.openhands.dev компенсированы чтением официального GitHub-репозитория, а не сторонних пересказов.

## Как организовать развитие проекта

Ниже — проектные решения Meta-Harness, а не обещания поставщиков.

1. **Каталог:** расписание читает разрешённые публичные каталоги. Запись содержит источник, время проверки, владельца по заявлению, лицензию, способ авторизации, возможности и ограничения. Наличие карточки не означает наличие доступной вычислительной мощности.
2. **Реестр исполнителей:** отдельно хранит обнаруженные ресурсы и настроенные аккаунты. Состояния: `discovered → reviewed → configured → connectivity_tested → enabled`. Секрет записывается только у провайдера или в менеджере секретов; в реестре — имя секрета.
3. **Ограниченные задания:** одна задача — один наблюдаемый результат, критерии приёмки, допустимые файлы, бюджет и источник требования. Начальные работы: воспроизведение модельных сравнений, анализ ошибок, документация API, переносимые тесты. Научная гипотеза не выдаётся за проверенный результат разработки.
4. **Исполнение:** диспетчер обращается только к настроенному адаптеру; сохраняет task ID и ссылки на PR/артефакты. Недоступный аккаунт возвращает `not_configured` или `authorization_failed`, а не симулирует агентную работу. Одновременные задачи в одной области кода ограничиваются.
5. **Приёмка:** изменения поступают в ветку/PR с тестами и отчётом. Обновление каталога, найденная статья и успешный тест имеют разные значения. Несколько ответов одной модели не считаются независимой научной репликацией.
6. **Наблюдение:** пользователь видит в GitHub Issues/Actions/PR задачи, журнал, ошибки авторизации и результаты. Веб-консоль Meta-Harness остаётся интерфейсом локальных вычислительных исследований и федерации. Согласование интерфейсов не требует передачи каждому агенту полного доступа к репозиторию или аккаунту.

Без настроенного модельного исполнителя можно реально автоматизировать обнаружение ресурсов, регулярные собственные вычисления и CI. Генерацию новых научных идей или исправлений внешней LLM нельзя обозначать работающей до первого успешного авторизованного запуска. Собственный open-weight model server — возможный будущий исполнитель без коммерческого API-ключа, но ему всё равно нужны вычисления, установленная модель и защищённый endpoint; в текущей проверке он отсутствует.

## Почему сторонние агенты не присоединяются автоматически

Публичная модель, MCP-сервер, SaaS-агент и автономный процесс — разные объекты. Каталог сообщает, что ресурс существует; оператор ресурса определяет, кто может его вызвать, что он выполняет и кто оплачивает работу. Обмен результатами возможен через явный контракт и добровольное принятие задачи. Нет подтверждённого универсального механизма, который переводит произвольного найденного агента в участники проекта и предоставляет его вычисления бесплатно.

Для добровольного участия достаточно опубликовать понятные contribution instructions, небольшие задания и машинно-читаемый контракт результата. Затем оператор запускает собственного агента или авторизует приложение. Приёмка вклада может давать доступ к разрешённым сводкам проекта; такой обмен не равен кредитам провайдера и не оплачивает его API автоматически.

## Источники и область проверки

Все источники ниже открыты 24.09.2026; текущие страницы могут измениться. Машиночитаемая версия: `data/autonomy_sources.json`. **Live inference, назначение задач внешним агентам, установка GitHub Apps, регистрация аккаунтов и оплата этим исследовательским этапом не выполнялись.** Статусы действующей поставки проверяются по её отдельным логам, а не выводятся из этого обзора.

- **A01:** [GitHub Models — retirement](https://docs.github.com/en/github-models).
- **A02:** [About GitHub Copilot cloud agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent).
- **A03:** [Using Copilot cloud agent via the API](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-via-the-api).
- **A04:** [Develop agentic workflows in GitHub Actions](https://docs.github.com/en/actions/tutorials/develop-agentic-workflows-in-github-actions).
- **A05:** [About Copilot automations](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-automations).
- **A06:** [Codex cloud — official OpenAI documentation](https://learn.chatgpt.com/docs/cloud).
- **A07:** [Review GitHub pull requests with Codex](https://learn.chatgpt.com/docs/third-party/github).
- **A08:** [Codex GitHub Action](https://learn.chatgpt.com/docs/github-action).
- **A09:** [About third-party coding agents](https://docs.github.com/en/copilot/concepts/agents/about-third-party-coding-agents).
- **A10:** [OpenHands Software Agent SDK — routine maintenance example](https://github.com/OpenHands/software-agent-sdk/blob/main/examples/03_github_workflows/01_basic_action/README.md).
- **A11:** [GITHUB_TOKEN](https://docs.github.com/en/actions/concepts/security/github_token).
- **A12:** [Archived OpenHands GitHub Action](https://github.com/OpenHands/openhands-github-action/blob/main/README.md).
