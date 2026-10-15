# «ИскИн Чёрного Заслона» — интеграционный пакет openKylin 3.0

**Статус: staging / диагностическая интеграция; на пользовательском ПК не установлено и не испытано.** Устройство Desktop Commander DESKTOP-HKC9DPM во время подготовки было офлайн. Содержимое локальных папок Spaces и действующие модели на ПК не доступны.

## Архитектура и ограничения

Главный координатор — существующий NeuroMorf/Meta-Harness со своим SQLite, lease-очередью, worker-процессами и ограниченным MCP. Компоненты KylinBot, ClawTeam, SwarmClaw, OpenClaw, Hermes, Qwen, DeepSeek, Kimi и локальные inference-сервисы регистрируются только после проверки реальных установок. Полный состав и статусы в components.json. Не запускать второй независимый координатор поверх тех же заданий.

Базовая политика config/resource_policy.json **не меняется**: лимит облачных расходов $0, платные LLM запрещены. Установка сторонних навыков, права ОС и сетевые соединения не передаются агентам автоматически. P2P и управление удалёнными устройствами выключены.

## Диагностика на openKylin

Требуется Python 3.11+. Из корня репозитория:

~~~bash
cd integrations/blackwall_openkylin
bash bootstrap-openkylin.sh --check --spaces "$HOME/Spaces" --output "$HOME/blackwall-doctor.json"
python3 blackwall.py doctor --project "$HOME/neuromorph-agent-os" --spaces "$HOME/Spaces" --offline
~~~

Если checkout или Spaces находится в другом каталоге, укажите настоящий путь. Можно передать несколько --spaces. Утилита получает только метаданные файлов и наличие CLI, не читает веса моделей, не выполняет найденные программы и не скачивает модели. Без --offline она опрашивает только фиксированные 127.0.0.1 endpoints. Наличие GGUF/SafeTensors и HTTP-ответа не доказывает загрузку модели.

Генерация **неактивного** шаблона offline-MCP:

~~~bash
python3 blackwall.py mcp-config --project "$HOME/neuromorph-agent-os" --output "$HOME/blackwall-mcp.json"
~~~

MCP-шаблон использует реальный скрипт scripts/run_offline_mcp.py и только инструменты offline_council_status, offline_council_start, offline_council_stop. JSON не активируется автоматически.

Существующее ядро запускается отдельно после проверки:

~~~bash
cd "$HOME/neuromorph-agent-os"
python3 -m workbench local-network --workers 2
~~~

Один конечный локальный запрос только на планирование:

~~~bash
python3 integrations/blackwall_openkylin/blackwall.py plan --question "Составь проверяемый план исследования"
python3 integrations/blackwall_openkylin/blackwall.py plan --question "Составь проверяемый план исследования" --execute
~~~

Первый вызов dry-run. Второй выполняет POST только на 127.0.0.1:8765/api/plan с явным --execute. Не исполняет команд модели.

## Необязательные установки

~~~bash
bash bootstrap-openkylin.sh --install-base
bash bootstrap-openkylin.sh --install-clawteam
~~~

Первая требует openKylin и sudo, устанавливает только git, tmux, python3-venv. Вторая без sudo создаёт отдельный venv, скачивает wheel clawteam==0.2.0 и проверяет SHA256. Транзитивные зависимости pip отдельно не закреплены: перед производственным использованием нужен lockfile и аудит. Агентные программы не запускаются автоматически. При будущей настройке ClawTeam обязательно CLAWTEAM_SKIP_PERMISSIONS=false и CLAWTEAM_TRANSPORT=file; это не полноценная ОС-песочница.

SwarmClaw/OpenClaw/Hermes не устанавливаются этим пакетом до проверки на целевом хосте версий, лицензий, протоколов и границ прав.

## Приёмка

~~~bash
python3 -m unittest discover -s integrations/blackwall_openkylin/tests -v
bash -n integrations/blackwall_openkylin/bootstrap-openkylin.sh
python3 -m unittest discover -s tests -v
~~~

Первые две проверки прошли в отдельном Linux-контейнере; это не openKylin и не live swarm. После подключения ПК требуется отдельно проверить фактические папки Spaces и окружение, исходные тесты NeuroMorf и один авторизованный ограниченный round-trip модели с независимой оценкой. Разделяйте source_reviewed, installed, protocol_tested, live_authenticated, accepted.

Не публикуйте blackwall-doctor.json, ключи, историю чатов или частные файлы Spaces в репозитории.
