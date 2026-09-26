# Подключения harness: фактические возможности и ограничения

26.09.2026. Исходный проект: `9b11d3a55d6072d9c516c0d485794074b91a9d36`. Эти выводы основаны на прочитанных первичных источниках и отдельных наблюдениях интерфейса; установки и model calls подтверждаются только квитанциями облачного запуска.

## Что добавлено в проект

- Изолированный bootstrap DeepSeek Harness, Qwen Code, Gemini CLI и OpenCode: точные версии, SHA512 архивов, запрет npm lifecycle scripts, проверка version/help. Он не выполняет вход или модельные запросы. Подробнее [CLOUD_HARNESSES_RU.md](CLOUD_HARNESSES_RU.md).
- Для DeepSeek Harness подготовлен отдельный профиль из явного списка плагинов, без наследования Standard/Minimal: нет shell, web, subagents, MCP, title inference и retry. Пустой реестр инструментов и одноразовый localhost-шлюз дополняют друг друга. Процесс не является полноценной OS sandbox.
- По умолчанию DSH работает с явно помеченным тестовым ответом. Отдельный `--live` запрашивает только `mimo-v2.5-free` по официальному бесплатному маршруту OpenCode, без Authorization, cookies, аккаунта или платного fallback. Один зарезервированный исходящий POST, ограничение времени/размера, без автоматического повторения. HTTP429/ошибка/неполный ответ сохраняются как отказ, не как старый либо тестовый результат.
- Реальный вход и расход квоты Qwen Code/Gemini CLI этим не заявлены. Нативные веб-аккаунты и CLI credentials — разные каналы.

## Подтверждённая облачная проверка

[GitHub Actions36218892892](https://github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/36218892892) установил все четыре CLI и проверил их реальные version/help. Actual checkout: `99f9674e5af8ac0b05ce8283442d7f948c99552a`, source head `73d5819a0d72efa94d6d1f611710e801eb24bd48`. DSH0.1.7-rc.2 обработал ровно один loopback fixture, завершился exit0, внешних модельных запросов0. [Core36218892834](https://github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/36218892834):567 тестов за38.147s, doctor и bootstrap дважды success. [Квитанции](contributions/cloud-harnesses-2026-09-26.json) сохраняют hashes, resolved dependency counts и реальные результаты probes.

## Исходные статьи и сверка

Colab CPU дополнительно подтвердил все четыре установки и реальный DSH fixture (exit0,1.418s). Единственная live-попытка DSH-FREE-REVIEW-001 к OpenCode вернула HTTP403: `upstream_requests=1`, `response_received=false`, DSH exit1,1.911s. Причина403 по одной квитанции не установлена. Повтор не выполнялся, cookie/API-key/proxy/платная модель не использовались. Этот маршрут не объявлен подключённым inference-провайдером. Сохранённый пользовательский блокнот содержит фактические outputs; его приватный адрес хранится только в локальной панели.

One-shot reservation относится к одному checkout/runtime. Создание другого Colab runtime не переносит этот счётчик; скрипт не ставится в автоматическое расписание. Глобальный durable бюджет существующего Qwen worker остаётся отдельным механизмом и не сбрасывается.

[DeepSeek Harness](https://www.deepseek.com/harness/en/) и [официальный репозиторий](https://github.com/deepseek-ai/deepseek-harness) подтверждают MIT, Cordis и сменные плагины. Выбран закреплённый preview0.1.7-rc.2, а не плавающий latest. Бесплатность исходников не означает бесплатность [DeepSeek API](https://api-docs.deepseek.com/quick_start/pricing). Встроенные headless-профили способны обращаться к title model, выполнять инструменты и повторы, поэтому для конечной рецензии используется отдельная конфигурация.

[Статья о LangSmith Custom Apps](https://ai-manual.ru/article/langsmith-custom-apps-kak-sozdat-sobstvennyij-interfejs-dlya-dannyih-ai-agentov/) сопоставлена с [анонсом LangChain](https://www.langchain.com/blog/langsmith-custom-apps): функция доступна Plus/Enterprise. В бюджет0 платное подключение не включено. Полезный принцип — интерфейс рецензирования traces, datasets и экспериментов — применим к публичным файлам Git без покупки Custom Apps.

[Статья об открытых Supabase](https://ai-manual.ru/article/vibe-coding-i-utechki-dannyih-16-000-otkryityih-baz-supabase-i-chto-delat-razrabotchiku/) сверена с [UpGuard](https://www.upguard.com/blog/everything-everywhere-systemic-data-exposure-in-supabase-apps) и [официальной защитой данных](https://supabase.com/docs/guides/database/secure-data). Число16326 относится к обнаруженным базам с читаемыми таблицами, не к доказанным утечкам персональных записей в каждой базе. Для текущего public Git ledger новая БД не нужна. Secret/service-role keys не размещаются в браузерном интерфейсе; будущая приватная БД потребует grants/RLS и негативных проверок чужого пользователя. Чужие базы нами не сканировались.

[Обзор бесплатных coding agents](https://ai-manual.ru/article/besplatnyie-ai-agentyi-dlya-programmirovaniya-v-2026-sravnenie-limitov-modelej-i-sposobov-podklyucheniya/) использован для отбора, а квоты сверены с поставщиками. [Qwen auth guide](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/) сообщает о завершении бесплатного OAuth15.04.2026; старую универсальную квоту аккаунта нельзя переносить в новый cloud runner. [Gemini CLI](https://geminicli.com/docs/resources/quota-and-pricing/) имеет бесплатный режим личного Google account с ограничениями; вход и фактическую доступность надо проверять отдельно. [OpenCode Console guide](https://opencode.ai/console/guides) разрешает free chat без Authorization, а [Zen pricing](https://opencode.ai/docs/zen/) перечисляет MiMo-V2.5 Free. Бесплатные предложения могут прекратиться; ошибка не вызывает подмены платной моделью. Текст публичного задания может обрабатываться поставщиком по его правилам; приватные данные не передаются.

[Статья SourceCraft](https://yandex.cloud/ru/blog/sourcecraft-ai-agents-scale-2026) и действующий аккаунт не означают, что все модели можно назначить нативным AI-коллегам. Два прежних AI-job остановились на запуске, причина пока не установлена. В этой задаче роли/права не повышались, повтор AI-job не делался. Code Assistant завершил SOURCECRAFT-REVIEW-003 в нативном чате: это реальная текстовая рецензия, не успешное автономное выполнение AI-job.

Официальная статья SourceCraft24.09.2026 дополнительно прочитана в открытой вкладке браузера: производитель описывает поручения через GitLab, мессенджер, веб-интерфейс, VSCode и CLI, отдельную учётную запись агента и возврат изменений на проверку. Это описание возможностей платформы; оно не снимает фактическую ошибку двух AI-job этого аккаунта и не создаёт подключение внешних моделей автоматически.

## Обмен результатами

Дополнительная статья о двух агентах и SDD разобрана в [контракте изменения](SDD_HARNESS_CONTRACT_RU.md). Он связывает критерии с проверенным кодом и облачными квитанциями; числа о замене команды не приняты как доказательство эффективности.

SOURCECRAFT-REVIEW-003 получил публичные результаты M03 и замечания DeepSeek/Claude. Его конкретный контрпример: после429 нельзя подставить cached/template answer и назвать это свежим model response. Root проверил этот отказ в тестах шлюза. Предложение SourceCraft считать уникальный id и created доказательством происхождения исправлено: метаданные/nonce/hash не удостоверяют веса модели или истинность. Ответ модели, когда он получен, остаётся непроверенным предложением. Обновлённый публичный пакет передаётся DSH-FREE-REVIEW-001 для конечного контрпримера к M02, а не бесконечного разговора.

[Stitch](https://stitch.google.com/) открыт в пользовательском аккаунте; UI показал Gemini3.8Flash/«Сбалансированный» и Gemini3.5Flash-Lite/«Быстрый». Одна отправка публичного задания интерфейса NeuroMorf вернула «Не удалось создать проект. Повторите попытку позже». Работающий проект/экспорт/MCP этим не создан. Новые credentials, платёж или обход региональной проверки не выполнялись.

Colab используется для конечного notebook запуска на CPU, без Drive mount, anti-idle или постоянного веб-сервера. [Ограничения Colab](https://research.google.com/colaboratory/faq.html) сохраняются. GitHub Actions запускает установку и протокольную проверку без модельного запроса. Постоянная переписка всех авторизованных нативных чатов при выключенном ПК остаётся отдельной нерешённой интеграцией.
