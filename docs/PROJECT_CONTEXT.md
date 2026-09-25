# SRF — общий контекст ChatGPT и Codex
Версия: 2026-09-25.01. Общий снимок знаний; автоматическая синхронизация не настроена.

Проект: «Исследовательская платформа где всё есть плагин». Система: SRF / Meta-Harness.
Код: https://github.com/Petr111111110000568/neuromorph-agent-os
Проверенный baseline: 0.10.0, commit e558f7083924edd6047709074d931daf51e0e38d.
Основной чат: «Создание системы ускорения эволюции». Связанный: «Продолжение анализа материала». Задача Codex: «Объединить проект ChatGPT и Codex».
Локальный код: C:/Users/f-tuning/Documents/Codex/2026-09-25/https-chatgpt-com-share-6aad99b0-2358/outputs/neuromorph-agent-os
Локальные документы: outputs/PROJECT_CONTEXT.md, RESEARCH_INTEGRATION.md, ai-manual-index.csv, ai-manual-review.md. Этот путь не предоставляет облачному ChatGPT доступ к диску.

ФАКТИЧЕСКОЕ СОСТОЯНИЕ
История содержит 0.4–0.8; GitHub уже содержит 0.9 и 0.10. Продолжать от фактического кода. В нём есть Python-ядро, SQLite, workers, discovery, федерация, Brain с 11 программными ролями, KAN, кортикальный эксперимент, Unreal/Pi adapters, HTTP/MCP. Прежние 280 успешных тестов относятся к Linux; это не проверка Windows. Внешние AI-участники и авторизованные model calls прежним отчётом не подтверждены. Адаптеры Unreal/Pi требуют Linux/WSL. Текущая задача добавляет исследовательские документы и записи в реестр источников; новые исполняемые MDA/Deep Life Sci integrations не заявляются.

ПРИНЦИПЫ
Научные источники → утверждения с происхождением → методы/вычислительные эксперименты → независимая оценка → артефакты. Модели, harness, поиск, инструменты, память, UI и evaluators заменяемы. Небольшое ядро управляет правами, лимитами, очередью и журналом. MCP — интерфейс совместимости; оркестратор SRF находится над ним.
Разделять историю задачи, контекст, личную память, знания проекта, инструкции и секреты. У памяти — scope, источник, версия, удаление и тесты извлечения. Секреты не входят в общий контекст. Названия областей мозга — функциональная аналогия ролей; согласие агентов не является научной репликацией.

ВЫВОДЫ ИССЛЕДОВАНИЯ 25.09.2026
1. Unreal: адаптер уже есть в v0.10. Проверять asynchronous operations, steering, recovery/cancel и стоимость на одинаковых задачах; benchmark производителя не является гарантией. https://unreallabs.ai/blog/unreal-agent/
2. Managed Deep Agents 0.8: пример identity, credentials, HTTP channels и разделённой agent/user memory. Память opt-in и привязана к deployment/principal; она не объединяет память ChatGPT/Codex. Public beta. Кандидат адаптера, не обязательное облачное ядро. https://www.langchain.com/blog/langsmith-managed-deep-agents-whats-new и https://docs.langchain.com/langsmith/python/managed-deep-agents-memory
3. Deep Life Sci: кандидат отраслевого пакета sources/evidence/analysis/evals. Демонстрационный research/education проект с зависимостями LangSmith; подключение и клиническая валидация не выполнены. https://www.langchain.com/blog/agent-harness-life-sciences и https://github.com/langchain-samples/deep-life-sci
4. Flowise/Keelflow: sunset Flowise подтверждён первичными источниками. Форк требует аудита лицензии, зависимостей и enterprise функций; не считать готовым многопользовательским ядром. https://ai-manual.ru/article/flowise-zakryili-kak-ustroen-fork-keelflow-i-chto-meshaet-prosto-prodolzhit-open-source-proekt/
5. ELIZA→контекст: различать историю, RAG и память; продуктовые заявления сверять с официальной документацией. https://ai-manual.ru/article/ot-eliza-do-pamyati-mezhdu-chatami-kak-ai-chatyi-nauchilis-ponimat-kontekst/
6. Cognitive OS: имя неоднозначно. Статья AI-manual не указывает первичный репозиторий; характеристики, MIT и benchmarks неподтверждены. Использовать идеи как гипотезы. https://ai-manual.ru/article/cognitive-os-lokalnaya-ide-dlya-multiagentnyih-workflow-s-grafom-znanij/
7. Sitemap ai-manual.ru: 9508 статей + 7 статических страниц. Полный URL-реестр не равен прочтению. Углублённо прочитаны 9 статей, ещё 18 просмотрены выборочно. Закрытые/onion-источники в текущем исследовании не изучались.

ДАЛЬНЕЙШАЯ РАБОТА
Воспроизвести baseline на целевой ОС. Затем один проверяемый сценарий: вопрос → первичный источник → утверждение → анализ → результат с происхождением. Разделять reviewed, installed, protocol-tested и live-authenticated. Предложения: project/user memory scopes, переносимый evidence pack, manifests с версиями/лицензиями, сравнение harness, отраслевой адаптер. Это очередь работ, не завершённые функции.

ОБЩИЙ КОНТЕКСТ
GitHub хранит канонический код; каждое изменение связывать с commit. Этот документ хранится в Sources ChatGPT и docs/PROJECT_CONTEXT.md локального Codex-проекта. При обновлении повышать версию и заменять обе копии. В новой задаче читать его, README, AGENTS.md и фактический статус среды. Закрепление задач облегчает навигацию, но не объединяет transcript, память и файлы. Нативное слияние project_id ChatGPT с локальным Codex-проектом в этой задаче не выполнено. Официальное описание: https://learn.chatgpt.com/docs/projects

