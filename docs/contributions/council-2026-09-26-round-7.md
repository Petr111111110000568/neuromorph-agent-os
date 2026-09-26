# Hermes, исследования платформ и наблюдаемое локальное развёртывание

Base: main `15e88f45603bb5324335f341d48584203df28c99`, после PR13. Проверенный PR13 head `a8b4c8f3a562891795b8c532e929e3338a3207ff`: Core [36221230896](https://github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/36221230896), 601 тест за 39.143s, doctor и bootstrap success; Hermes/Instinct и четыре CLI проверки также success. Fixture является проверкой протокола с нулём внешних модельных вызовов.

## Фактический бесплатный вызов

Один допущенный [Hermes → Qwen run36221395344](https://github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/36221395344) завершил учёт ошибки корректно, но модель **не ответила**: `provider_unavailable`, HTTP503, response_received=false, requests=1, harness_requests=1, tools=[], paid_fallback=false. Успех workflow означает выполнение контроллера. Он не означает успех inference.

Сохранённый ledger `37b7678a51c0593ea8cfdbe9ad20415516904c14`: attempts=2, successes=1, pending=null, следующая допустимая попытка не ранее **2026-09-27T05:38:55Z**. Старый ответ 25 сентября сохранён отдельно. Счётчики не сбрасывались; повтор для демонстрации не выполнялся. Artifact10899436338 digest `8d71bf7c9eb110d933493a9dd9c8602874b936985b5d23dcfb7064994f709889` содержит настоящие receipt/result/harness log. Временная ссылка скачивания не публикуется.

Cron теперь проверяет допуск каждый час; ограничения >=6ч и <=4 попыток за rolling24h и суточный backoff сохранены. Допуск не гарантирует доступность бесплатной демонстрации или точное время GitHub cron.

## DEEPSEEK-013 — реальный нативный ответ

Parent: DEEPSEEK-012 correction / SOURCECRAFT-REVIEW-003 / PR13. В чат переданы только публичные commit/run и конкретный отказ 503. Ответ получен: DeepSeek предложил раздельно показывать последнюю неудачу, сохранённый успех, свежесть snapshot и next_due. Негативный сценарий: зелёный workflow при response_received=false ошибочно скрывает 503 старым ответом.

Приняты четыре критерия: новый отказ не перекрывается старым успехом; снимок имеет время/commit/возраст и отметку устаревания; next_due не становится обещанием запуска; чтение панели не инициирует модель и не обходит policy. HTTP-код доступен по ссылке реального run, а не выдумывается из сокращённого ledger. Это рецензия, не независимое исполнение тестов.

Уточнение к ответу DeepSeek: Windows Job Object может поддерживать разные лимиты, но наш supervisor включает **только управление жизненным циклом KILL_ON_JOB_CLOSE**. CPU/memory limits здесь не заявлены. Venv и Job Object не ограничивают доступ пользователя к файловой системе/сети и не являются полноценной ОС-песочницей.

## Реализация

Read-only CloudObserver читает один фиксированный публичный repository/ref, затем конкретный immutable SHA; размер/схема/redirect проверяются. Один общий cache на 5 минут, без credentials и proxy. При ошибке старый снимок сохраняется с явным stale. UI выводит модельный текст через textContent, без исполнения.

Исторический environment_status Linux отделён от actual runtime. Windows-скрипты используют выделенный checkout/venv/state, скрытый supervisor, suspended child с назначением Job до resume, очистку environment и адреса loopback. Новый пользовательский запрос прямо разрешает локальное развёртывание; обычные cloud CI сохраняются.

Сравнение: [OpenAI, Claude, Open WebUI, Open Deep Research](../RESEARCH_PLATFORMS_AND_UI_RU.md). Развёртывание: [Windows](../WINDOWS_LOCAL_DEPLOYMENT_RU.md). Интеграция [Hermes и Instinct](../HERMES_AND_INSTINCT_RU.md) не означает подключение закрытого облачного Instinct: независимая открытая heuristic-библиотека дала только 3/6 совпадений и не используется production-router.

Нативные чаты не стали непрерывным общим облачным транспортом. HF ждёт пользовательский write-secret, Jules — подтверждение новых GitHub App прав; ограничения не заменяются обещаниями автономности.
