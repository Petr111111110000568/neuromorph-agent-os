# Хранение кода, формул и исследовательских итераций

Канонический публичный репозиторий — GitHub. Скрипт `scripts/sync_research_storage.py` готовит ограниченный снимок выбранных публичных Git-объектов и может записать его в dataset `Kto-to/neuromorph-agent-contributions`. Исполнение — только GitHub Actions. Наличие адаптера и dataset не доказывает действующую автоматическую запись: для неё требуется отдельный секрет `HF_WRITE_TOKEN` с правом записи именно в этот dataset. Текущий токен NeuroMorfEvalutions показан в интерфейсе как WRITE, но его значение не передано в Actions. Подготовлена форма отдельного fine-grained токена для одного датасета.

## Контракт облачного запуска

```sh
python3 scripts/sync_research_storage.py --source-ref main --output runtime/research-storage/main/receipt.json
python3 scripts/sync_research_storage.py --source-ref autonomy/continuous --repo-dir ledger-checkout --output runtime/research-storage/continuous/receipt.json
```

Во втором случае запускается доверенный скрипт из checkout `main`; `ledger-checkout` используется только как источник Git-объектов. Ни скрипты из ветки агента, ни экспортированный Python не импортируются и не исполняются. Требуются `GITHUB_ACTIONS=true` и правильное `GITHUB_REPOSITORY`. Секрет задаётся только для шага синхронизации; Git subprocess получает очищенное окружение.

Без `HF_WRITE_TOKEN` сохраняются manifest и receipt со статусом `credentials_required`; HTTP-запросов нет, завершение — 0. Неверные права дают `credentials_rejected`; конкурирующая запись — `concurrent_update`; недоступный транспорт — `storage_unavailable`. Они не должны останавливать отдельный исследовательский workflow. Некорректный исходный снимок даёт `snapshot_rejected` и код 1.

## Состав и версии

Отбор закреплён в коде: `README.md`; Markdown/TeX из `docs/research/`, `docs/research-YYYY-MM-DD/`, `docs/contributions/`; `continuous-state.json`; верхнеуровневые документы с RESEARCH, MODEL или SCIENCE_ROADMAP в имени; `.py` непосредственно в `workbench/experiments/`. Остальные пути, runtime, секреты, cookies, история частных чатов, окружения, большие данные и бинарные файлы не экспортируются. Git symlink и submodule в разрешённом пути запрещены. Читаются только закоммиченные blobs, а не произвольные файлы рабочей папки.

Лимиты снимка: 64 исходных файла, 5 MiB суммарно, 256 KiB на файл, плюс небольшой manifest. Проводится проверка UTF-8, SHA Git blob, известных форматов ключей и текущего write-токена. Такой сканер не является универсальным доказательством отсутствия персональных данных; источник должен оставаться разрешённым публичным репозиторием.

Назначения — `snapshots/main/` и `snapshots/continuous/`. Manifest связывает `source_commit`, `source_ref`, относительные пути, размеры и SHA-256; его digest зависит от содержимого, поэтому идентичный снимок повторно не коммитится. Устаревшие файлы не удаляются: актуальный состав определяется manifest, предыдущие версии сохраняются в истории HF. Полная история GitHub, PR, issues и бинарные датасеты этим адаптером не зеркалируются.

Запись использует стандартный [HF create_commit с parent_commit](https://huggingface.co/docs/huggingface_hub/en/package_reference/hf_api#huggingface_hub.HfApi.create_commit). Wire-формат проверен по [официальному исходному коду huggingface_hub](https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/_commit_api.py): preupload, затем NDJSON header и base64 file records. При режиме LFS/Xet или ignored file операция прекращается. Внешние библиотеки не требуются. Назначение и hostname фиксированы; redirects, повтор POST после ошибки и изменение тарифов не выполняются. CAS не позволяет незаметно затереть параллельный commit.

## Российское зеркало

25 сентября 2026 года создано публичное [зеркало SourceCraft](https://sourcecraft.dev/sasha1000000000002020/neuromorph-agent-os); первоначальный импорт завершён. Во встроенной синхронизации выбраны ветки `main` и `autonomy/continuous`, период — два часа. Это подтверждено интерфейсом аккаунта при настройке. Успешность будущих запусков и совпадение конкретных commit проверяются отдельно по времени последней синхронизации и SHA веток.

[Документация SourceCraft](https://sourcecraft.dev/portal/docs/en/sourcecraft/operations/migration) описывает одностороннее зеркало: исходный GitHub остаётся источником истины. Правки непосредственно в зеркальных ветках могут быть перезаписаны очередной синхронизацией. PR, issues и комментарии не синхронизируются после первоначального импорта. Поэтому обсуждения или предложения агентов, созданные только в SourceCraft, требуют отдельного явного переноса в канонический репозиторий.

Этот слой хранит материалы, но не предоставляет моделям доступ к аккаунтам и не подтверждает научные выводы. Бесплатные квоты ограничены; платные услуги и автоматический переход на оплату не подключаются.
