# Instinct: проверенный код и границы интеграции

Проверено 26.09.2026 по первичным источникам. Дополнительный Exa-поиск: `sources_reviewed=5` (запрошенные результаты, не число полностью прочитанных уникальных страниц). Реальный [облачный запуск36221054614](https://github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/36221054614) на commit `9dd335856dc130842df31047a0af940ae57b1fcc` завершился успешно. [Полная квитанция](contributions/hermes-instinct-2026-09-26.json). Локально код и тесты не запускались.

На шести заданных примерах LocalEngine совпал с ожидаемой меткой в **3/6**, простой exact-token reference также в **3/6**. Эвристика распознала синоним `git commit`, но выбрала `code` для запрета исполнения, русского поиска научных статей и приветствия без признаков задачи. Поэтому она включена в исследовательский стенд, а не в доверенную маршрутизацию рабочих задач. Это измерение на шести примерах, не общая оценка качества.

## Два разных продукта

[Instinct](https://instinct.com/) — персональный облачный помощник Spear Street Technology. Его [условия](https://instinct.com/terms) и [политика данных](https://instinct.com/privacy-policy) описывают действия через подключённые приложения. В проверенных официальных страницах и поиске **публичный исходный код, developer API/SDK и гарантированная бесплатная квота не найдены**. Это ограниченный результат проверки, не доказательство отсутствия закрытого API. В эту интеграцию его аккаунты и приложения не подключены.

Отдельный [bhavikprit/instinct-ai](https://github.com/bhavikprit/instinct-ai/tree/7d123c27b29384298082b4e46bab1d517f99414a) — открытая Python-библиотека. Связь автора с облачным Instinct не установлена. [LICENSE](https://github.com/bhavikprit/instinct-ai/blob/7d123c27b29384298082b4e46bab1d517f99414a/LICENSE) объявляет Apache-2.0; [pyproject](https://github.com/bhavikprit/instinct-ai/blob/7d123c27b29384298082b4e46bab1d517f99414a/pyproject.toml) задаёт версию 0.2.0, Python>=3.9 и пустые обязательные dependencies. GitHub badge «Other» не заменяет чтение лицензии. Это **не установленный официальный SDK instinct.com**.

## Сравнение с выбранными системами

| Система | Реальная роль | Что требуется для вычислений | Текущая граница |
|---|---|---|---|
| Instinct cloud | Персональный сервис приложений | Собственный доступ; API и zero-cost не подтверждены | Не маршрутизируется автоматически |
| Instinct AI LocalEngine 0.2.0 | Regex/словари, фиксированные оценки, выбор категории | CPU; API-модель не вызывается | Изолированная проверка трёх модулей |
| Hermes | Agent harness с SDK, инструментами и model adapter | Отдельная модель/compute | Наш профиль отключает инструменты; Qwen использует уже сохранённую попытку continuous |
| smolagents | Agent framework с явными tools и model | Отдельная модель/compute | Сравнение архитектуры; новой установки здесь нет |
| DeepSeek DSH | Agent harness/плагины | Отдельная модель/compute | Существующий ограниченный protocol fixture; наличие CLI не доказывает доступность провайдера |

Первичные ссылки для сравнения: [Hermes](https://github.com/NousResearch/hermes-agent), [smolagents](https://github.com/huggingface/smolagents), [DSH](https://www.deepseek.com/harness/en/). Ни лицензия framework, ни локальная эвристика не предоставляют бесплатный бесконечный облачный LLM runtime.

## Что интегрировано

`scripts/instinct_local_probe.py --execute --output-dir runtime/instinct-probe` разрешён только в Linux GitHub Actions/Colab. Без `--execute` выводится plan без установки и запуска стороннего кода. Workflow передаёт очищенное окружение; probe сам создаёт отдельный child без ключей. Вычисления ограничены шестью публичными заранее заданными примерами, child timeout 20 секунд.

Из [PyPI 0.2.0](https://pypi.org/pypi/instinct-ai/0.2.0/json) загружается фиксированный wheel (317385 bytes), SHA256 `b0f84e23a3525cddc04afb43bdccd204d6b6354410b4892e8cd86d4b8d220a4c`. До загрузки кода проверяется digest; HTTP redirects запрещены. Pip/install hooks не запускаются. Загружаются только `reflex.primitives`, `reflex.backends.base`, `reflex.backends.local`; широкие package initializers, auto/client/fallback/ONNX/telemetry не импортируются. Источник алгоритма: [LocalEngine](https://github.com/bhavikprit/instinct-ai/blob/7d123c27b29384298082b4e46bab1d517f99414a/reflex/backends/local.py).

Это **subset fixture, а не полная установка SDK**. Child audit hook запрещает socket и запуск subprocess; он служит дополнительным ограничением и не объявляется полноценной OS sandbox для произвольного вредоносного кода. Здесь допускаются только точные проверенные модули из pinned wheel. Учётные данные, автоопределение backend и платные fallback исключены из пути исполнения.

Выход: `runtime/instinct-probe/receipt.json` и строка `INSTINCT_RECEIPT=...`. Сравниваются выбор LocalEngine и простой exact-token reference с явно заданными fixture labels. Есть синонимы, отрицание, русский запрос, отсутствие признаков. Числа совпадений описывают только эти шесть случаев: это не научная оценка, калибровка вероятностей, безопасность или доказательство превосходства. Сохранённая квитанция не подключает библиотеку к production routing и не влияет на лимит четырёх Qwen-попыток в сутки.

Для повторяемого research cycle полезность такого модуля — изучение дешёвой категоризации с обязательным abstain/validation. Автоматически доверять его security/probability labels нельзя: проверенный код назначает их правилами. Следующее решение о применении должно опираться на отдельные размеченные данные и измерение ошибок, включая отрицания и русский язык.
