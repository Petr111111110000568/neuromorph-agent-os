# Hermes и Instinct AI: облачная интеграция

Дата проверки: 26.09.2026. Лимит дополнительных расходов: 0.

## Hermes в существующем исследовательском цикле

Источник — официальный [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
релиз v2026.9.24 / версия 0.21.5, commit
`f97608f178d1ffeca59860195ab7da295f7c8e5f`.
Hermes — оболочка агента; в этом профиле его моделью служит уже принятый публичный
Qwen/Qwen3-Demo. Добавление Hermes не создаёт второго независимого провайдера.

`bootstrap_hermes.py` ставит закреплённый исходный код и core dependencies по
uv.lock в отдельное cloud окружение. Обычный wheel самого Hermes не подходит:
официальная упаковка требует editable source tree с runtime assets.
Все установки и исполнения выполняются в GitHub Actions или конечной Colab-сессии.

`hermes_no_tools.py` вызывает SDK с пустым набором инструментов, одной итерацией,
отключёнными памятью, сжатием, фоновыми обзорами и вспомогательными моделями.
Секреты GitHub и пользователя процессу не передаются. Ограничение Python I/O
дополняет профиль, но не заявляется полноценной изоляцией ОС.

Loopback-шлюз принимает только один запрос к фиксированному Qwen model alias.
Единственное user-сообщение должно совпадать с каноническим публичным заданием
побайтно. Системные инструкции оболочки не пересылаются в Qwen Space.
Повторный запрос, инструменты, история другого задания и смена модели отклоняются.
Успех требует реального ответа провайдера, успешного завершения SDK и точного
совпадения полученного им текста; ошибка/429 не заменяется шаблонным ответом.

`continuous.yml` сохраняет прежний durable reserve/finalize, общий cooldown
не менее 6 часов и не более 4 попыток за скользящие 24 часа. Установка происходит
до резервирования; её отказ не отправляет модельный запрос. Ошибка после
резервирования сохраняет израсходованную попытку. Роли author/reviewer/reviser
передают реальный предыдущий результат через ветку `autonomy/continuous`.
Предложенный код проверяется синтаксически и не запускается автоматически.

Расписание: `02:17, 08:17, 14:17, 20:17 UTC` (05:17, 11:17, 17:17, 23:17 MSK).
GitHub может задерживать/пропускать schedule; наличие cron не доказывает живого
цикла. Фактические результаты смотреть в
[Actions](https://github.com/Petr111111110000568/neuromorph-agent-os/actions/workflows/continuous.yml)
и [журнале](https://github.com/Petr111111110000568/neuromorph-agent-os/tree/autonomy/continuous).
Остановка: repository variable `CONTINUOUS_ENABLED=false` или Disable workflow.

Отдельный `hermes-harness.yml` устанавливает настоящий SDK и проверяет протокол
на явно обозначенном fixture, с **нулём внешних генераций**. Такая проверка
не считается исследовательским вкладом модели. Живой запуск выполняется только
через общий continuous reserve; отдельного `--live` с новым счётчиком нет.

## Instinct AI

[Instinct.ru](https://instinct.ru/) — креативное агентство. После уточнения
названия найден отдельный персональный агент [Instinct](https://instinct.com/),
оператор Spear Street Technology, Inc. В актуальном
[интерфейсе входа](https://app.instinct.com/login) требуется телефон, код и
принятие Terms of Service / SMS Terms. Доступ текущего аккаунта не подтверждён.
В просмотренных официальных материалах не установлены публичный API для
исследовательского worker, бесплатная квота и возможность unattended запуска.
Статус: `account_access_required; automation_and_zero_cost_unverified`.
Instinct не включён в автоматическую маршрутизацию по одному названию.

Одноимённый [bhavikprit/instinct-ai](https://github.com/bhavikprit/instinct-ai)
не установлен как SDK облачного Instinct: связь этих проектов не подтверждена.
В проверенном commit `7d123c27b29384298082b4e46bab1d517f99414a` бесплатный
`LocalEngine` — словарные/regex эвристики. Режим auto и fallback при наличии
ключей могут обращаться к внешним платным API. Это не дополнительная бесплатная
рассуждающая модель и не основание автоматически выдавать ему credentials.

## Другие проверенные пути

| Система | Практическая роль | Текущее условие |
|---|---|---|
| DSH, Qwen Code, Gemini CLI, OpenCode | Уже установленные опциональные cloud harnesses | См. прежнюю квитанцию PR12. Реальный DSH→OpenCode free запрос вернул403; успешного inference для этого пути нет |
| [HF smolagents](https://huggingface.co/docs/smolagents/index) | Возможный ограниченный reviewer | Библиотека бесплатна; вычисления и модель нужны отдельно. CodeAgent выполняет Python даже с tools=[]; профиль ещё не установлен |
| [HF ZeroGPU](https://huggingface.co/docs/hub/spaces-zerogpu) | Возможный конечный GPU-run | Подходящий Free personal account с verified email, возрастом >30 дней и good standing может создать до2Spaces; текущая eligibility аккаунта не проверена |
| [Google ADK](https://google.github.io/adk-docs/get-started/python/) | Будущий отдельный API-agent | Нужны Gemini Free Tier credentials и eligibility. Hosted Agent Runtime требуетbilling и не включён |

Условия [HF Spaces](https://huggingface.co/docs/hub/spaces-overview) различают
нулевую почасовую цену CPU Basic и право создания compute Space. Обычный
Gradio/Docker Space требует платного плана; ZeroGPU имеет отдельное исключение.
Ни PRO, ни платные кредиты не приобретались.

Интеграция Hermes создаёт облачные записи в GitHub. Она не создаёт автоматически
переписку в нативных аккаунтах Qwen, DeepSeek, Instinct или Claude. Такие сеансы
по-прежнему требуют отдельного разрешённого контроллера платформы.
