# Научные платформы и протоколы NeuroMorf — 26.09.2026

## Результат исследования

35 записей объединяют научные сервисы, открытые harness, модели, интерфейсы и учебные материалы. В карточках раздельно указаны лицензия, доступ, ограничения и следующий шаг. Обзор не означает установку, авторизацию или научную валидацию.

В студию добавлены /science.html и сохраняемый протокол: вопрос → гипотеза → метод → контроль → критерии. Три сценария: обзор доказательств, воспроизведение метода, сравнение подходов. Протокол сохраняется как задача существующего проекта с изоляцией владельца, CSRF и идемпотентным повтором. Экспорт JSON позволяет передать план ассистенту. Само сохранение не вызывает модель и не исполняет предложенный код.

## Существенные выводы

- Rosalind Workbench и GPT-Rosalind — разные уровни доступа. Research требует допуска; наличие ссылки или выбранного плагина его не подтверждает. В текущем наборе инструментов исполняемого коннектора Rosalind нет.
- Claude for Science — научная среда, не отдельная модель. Условия бета-доступа и академических предложений проверяются для аккаунта; бесплатный API из них не следует.
- AutoGen переведён в режим сопровождения; AutoGen Studio прямо обозначен как прототип. Это не готовая защищённая многопользовательская облачная студия.
- Deep Life Sci имеет открытый код, но требует инфраструктуры и ключей. Открытая лицензия не делает вычисления бесплатными.
- CAS Newton предупреждает о возможных неточностях. Утверждение «не выдумывает факты» не принято как гарантия.
- Agents-A1 опубликован на Hugging Face, но карточка не показывает развёрнутого Inference Provider. Наличие весов не означает доступный бесплатный endpoint.
- aporb/agentic-os, modimihir07/agentic-os, ANOLISA и коммерческие сайты с похожими названиями — разные проекты. Hermes в нашем контуре остаётся на ранее закреплённой версии.
- В руководстве DEV обнаружен pytest || true: такая команда маскирует провал тестов. Она не перенесена в проверку проекта. Fuzzing и сторонние методы требуют отдельной одноразовой среды.
- В документации URSA описано отсутствие изоляции сессий MCP. LangGraph checkpoint/replay не даёт exactly-once для внешних действий без идемпотентности.

## Сопоставление

| Система / материал | Категория | Проверенный статус | Доступ и затраты |
|---|---|---|---|
| [Deep Life Sci](https://github.com/langchain-samples/deep-life-sci) | Научный harness | Изучен; не установлен | Код открыт; LangSmith Sandbox требует карту даже на бесплатном Developer, включает 5 LCU/месяц; ключи моделей отдельные. |
| [Claude Science (beta)](https://claude.com/product/claude-science) | Научная среда | Изучен; доступ не подтверждён | Бета на Pro/Max/Team/Enterprise; академическое предложение требует проверки принадлежности и условий. |
| [Gemini for Science / Co-Scientist](https://ai.google/gemini-for-science/) | Научная программа | Изучен; доступ не подтверждён | Google Labs описывает экспериментальные инструменты; доступ аккаунта, квоты и цена не подтверждены. |
| [Beaver — scientific curation harness](https://www.microsoft.com/en-us/research/publication/building-agent-harnesses-for-scientific-curation-from-multimodal-sources/) | Исследовательский материал | Статья изучена; код не проверен | Статья открыта; готовый официальный сервис и бесплатные вычисления не подтверждены. |
| [CAS Newton](https://www.cas.org/solutions/cas-newton) | Научный сервис | Изучен; доступ не подтверждён | Доступ через продукты CAS; бесплатное право использования API не установлено. |
| [Causaly Agentic Research](https://www.causaly.com/products/agentic-research) | Научный сервис | Изучен; доступ не подтверждён | Корпоративный сервис; бесплатные API и непрерывные вычисления не подтверждены. |
| [Paper2Agent](https://github.com/jmiao24/Paper2Agent) | Научный harness | Изучен; не установлен | Код открыт; штатный процесс использует Claude Code и модельные вызовы; сложная сборка оценивается авторами примерно в $15. |
| [URSA — Universal Research and Scientific Agent](https://github.com/lanl/ursa) | Научный harness | Изучен; не установлен | Код ursa-ai открыт; модели и вычисления подключаются отдельно; рекомендован Python 3.11–3.12. |
| [S1-NexusAgent](https://github.com/CASIA-LM/S1-NexusAgent) | Научный harness | Изучен; не установлен | Код открыт; требуются ключи модели и эмбеддингов, рекомендован DeepSeek; бесплатное исполнение не подтверждено. |
| [InternScience Agents-A1](https://huggingface.co/InternScience/Agents-A1) | Модель с открытыми весами | Изучен; веса не загружены | Открыты веса 35B MoE BF16; HF сообщает об отсутствии Inference Provider; бесплатный сервер не найден. |
| [Awesome Agent Scientists / Agentic Science survey](https://github.com/AgenticScience/Awesome-Agent-Scientists) | Каталог исследований | Справочный материал изучен | Открытая библиография; не предоставляет модель, API или вычисления. |
| [AI, agentic models and lab automation for scientific discovery](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1649155/full) | Научный обзор | Справочный материал изучен | Открытая обзорная статья от 29 августа 2025 года; исполняемой платформы нет. |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Исполнение агентов | Изучен; сохранена закреплённая версия | Открытый код; модель и вычисления предоставляются отдельно. |
| [Agentic OS / aporb](https://github.com/aporb/agentic-os) | Интерфейс | Изучен; не установлен | Локальное приложение Next.js 15; Hermes API и модель нужны отдельно. |
| [Agentic OS / modimihir07](https://github.com/modimihir07/agentic-os) | Интерфейс | Изучен; не установлен | Локальный FastAPI/SPA, внешние CLI и provider credentials. |
| [Harnessing Agentic AI for Research and Practice](https://github.com/drfittri/harnessing-agentic-ai) | Учебный пример | Изучен; не установлен | Открытые слайды и синтетический demo kit. |
| [Agentik {OS}](https://www.agentik-os.com/) | Консалтинг | Коммерческий сайт изучен | Chief AI Officer as a Service, сайт услуг. |
| [ANOLISA / agentic-os.sh](https://agentic-os.sh/) | Изоляция исполнения | Кандидат для Linux; не установлен | Открытый Linux runtime/security/observability; часть macOS, Windows не общая цель. |
| [agentic-os.com](https://agentic-os.com/) | Неподтверждённый ресурс | Содержимое не подтверждено | Корневая страница возвращает JS redirect /lander; /lander не прочитан. |
| [MindStudio: Agentic Operating System](https://www.mindstudio.ai/blog/what-is-agentic-operating-system) | Инженерный материал | Справочный материал изучен | Публичная статья поставщика. |
| [Agentic Engineering OS](https://pypi.org/project/agentic-engineering-os/) | Инструменты разработки | Изучен; не установлен | PyPI v1.1.0; Python >=3.10; стандартная библиотека по README. |
| [ECC Agentic OS skill](https://mcpmarket.com/tools/skills/agentic-os) | Инструкции агента | Первоисточник найден и изучен | Открытый Markdown skill; нужен работающий harness и отдельно доступная модель. |
| [Sai Tarrun: Building an Agentic OS](https://dev.to/saitarrun/building-an-agentic-os-a-complete-end-to-end-guide-for-autonomous-ai-engineering-workflows-25dk) | Учебный пример | Изучен; найден дефект проверки | Публичное руководство; AI CLI в примерах placeholders. |
| [exAI Agentic OS](https://exai.cloud/) | Коммерческая платформа | Изучено описание поставщика | Enterprise IDE/Builder/Runner; публичные технические обещания. |
| [OpenGrowth.AI](https://opengrowth.ai/) | Консалтинг | Изучено описание поставщика | Fractional experts + agents; бесплатная консультация не free compute. |
| [Irreplaceable AI Engineer](https://irreplaceable-ai.ru/products/ai-engineer) | Обучение | Учебная программа изучена | Практический курс TypeScript/JS об Agentic OS. |
| [GitHub Security Lab Taskflow Fuzzing](https://github.blog/security/application-security/ai-powered-fuzzing-with-the-github-security-lab-taskflow-agent/) | Проверка безопасности | Изучен; нужна изолированная среда | Linux, AFL++/clang/lcov, LLM provider; вычисления и LLM отдельно. |
| [Rosalind Workbench / GPT-Rosalind](https://openai.com/ru-RU/rosalind/) | Научная среда | Изучен; доступ не подтверждён | Explore использует доступные пользователю модели ChatGPT; Research требует допуска организации. Наличие плагина не подтверждает доступ к GPT-Rosalind. |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/functional-api) | Оркестрация | Документация изучена | Документация открыта; хранение, модели и инфраструктура подключаются отдельно. |
| [AutoGen](https://github.com/microsoft/autogen) | Оркестрация | Изучен; не установлен | Репозиторий открыт; модельные API и ключи предоставляются отдельно. |
| [AutoGen Studio](https://github.com/microsoft/autogen) | Интерфейс | Изучен; не установлен | Открытый прототип; расходы моделей и инфраструктуры отдельно. |
| [LangSmith Custom Apps](https://ai-manual.ru/article/langsmith-custom-apps-kak-sozdat-sobstvennyij-interfejs-dlya-dannyih-ai-agentov/) | Интерфейс | Вторичный источник изучен | Статья указывает Plus/Enterprise для создания через чат; актуальные права аккаунта и тариф в этом проходе не проверены. |
| [Карта мозга мухи — исследовательский пример](https://habr.com/ru/articles/1086792/) | Исследовательский материал | Вторичный источник изучен | Статья публична; первичные данные и научная работа в этом проходе не проверены. |
| [Advanced Agentic Harness — разбор Островка](https://habr.com/ru/companies/ostrovok/articles/1084568/) | Инженерный материал | Материал изучен | Текст публичен; mock-примеры отделены автором от реального LLM. |
| [Agentic OS — видео о SEED, PAUL и Hermes](https://www.youtube.com/watch?v=01mYICuCI_Q) | Инженерный материал | Текстовая расшифровка просмотрена | В описанном процессе есть VPS и hosting; постоянные бесплатные вычисления не подтверждены. |

## Источники, полнота и ограничения

Первоисточники, лицензии и ограничения каждой записи сохранены в config/science_catalog.json и доступны в интерфейсе. Сначала проверялись официальные страницы и README; статьи Habr, AI-manual, DEV и текстовая расшифровка YouTube служат вторичными материалами. Содержимое agentic-os.com подтвердить не удалось. Маркетинговые заявления не заменяют независимых испытаний.

Использованы Exa (пять поисков, суммарный запрошенный объём 31 результат; это не 31 независимо проверенный источник), чтение страниц и Context7 для LangGraph. Учётные записи Gmail/Drive и их документы для публичного исследования не читались. Установка неизвестных пакетов, доступ к секретам, платные вызовы и автоматическое исполнение стороннего кода не выполнялись.

Каталог — снимок на дату обзора, а не мониторинг текущей доступности. Формулировки о поддержке, тарифах и версиях могут измениться. Дополнительные расходы текущей работы: 0.

## Проверка реализации

Облачные результаты CI и фактическая проверка локального интерфейса фиксируются отдельно в outputs/SCIENTIFIC_SYSTEMS_RELEASE.md после завершения выпуска. Инженерные тесты приложения не являются научной валидацией перечисленных платформ.