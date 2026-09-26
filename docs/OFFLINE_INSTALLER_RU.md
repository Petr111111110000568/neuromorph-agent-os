# Переносимый offline-пакет

`scripts/offline_bundle.py` собирает исходники платформы и, при явном указании, локальную модель с её движком и Windows Python. Установка проверяет архив, записывает файлы в новый каталог и **ничего из пакета не запускает**. Запуск выполняется отдельно пользователем.

## Что входит

- `app/`: только чистые Git-tracked файлы из заданных в скрипте корней; личные state, runtime, credentials, .env и ключи исключены.
- `app/runtime/local-model/`: только явно перечисленные в component manifest файлы модели, llama.cpp, DLL, внутренний manifest, лицензии и карточка модели.
- `python/`: опциональная официальная Windows embeddable-сборка CPython с лицензией.
- `Launch.py`, `Start.cmd`, `start.sh` и `BUNDLE_MANIFEST.json`.

При первом запуске состояние создаётся в `state/` рядом с `app/`; существующая SQLite другой установки автоматически не копируется. Обновление устанавливается в отдельный новый каталог. Объединение личных данных — отдельная операция.

Текущий подготовленный model-компонент — **Qwen3-14B-Q4_K_M**, revision `530227a7d994db8eca5ab5ced2fb692b614357fd`, и **llama.cpp b11146, Windows x64 Vulkan**. Это не Kimi K3. Наличие GGUF не делает включённые Windows EXE/DLL исполняемыми на Linux/macOS. Для этих ОС нужен отдельно проверенный нативный движок и новый manifest. Исходное Python-ядро переносимо; `start.sh` требует установленный Python 3.11+.

Локальные component manifests находятся в `work/offline-model-component.json` и `work/offline-python-component.json` рабочего пространства. Они не публикуются автоматически вместе с личными файлами.

## Сборка

Используйте чистый checkout проверенного commit. Каталог исходников `outputs/neuromorph-agent-os` может содержать незакоммиченные изменения; сборщик отклоняет такой checkout. Каталог модели можно указать отдельно, поэтому переносить 9 GB в source checkout не требуется.

Пример PowerShell из корня рабочего пространства:
```powershell
python CLEAN_SOURCE/scripts/offline_bundle.py build --source CLEAN_SOURCE --output work/NeuroMorf-offline.zip --model-root outputs/neuromorph-agent-os --model-manifest work/offline-model-component.json --python-root work/python-embed-3.13.15 --python-manifest work/offline-python-component.json
```

`CLEAN_SOURCE` замените реальным путём к чистому checkout. Для пакета без модели/Python опустите соответствующие пары параметров. Исполняемый Git должен быть в PATH; альтернативно задайте `NEUROMORPH_GIT` полным путём к проверенному Git.

Сборщик выводит JSON с SHA-256 архива, размером, количеством файлов и commit. Сохраните эту квитанцию отдельно от ZIP. ZIP64 пишется потоково, файлы модели не читаются целиком в память. Время записей фиксировано; одинаковые входные байты и component manifests дают одинаковый архив. Хеши каждого model/runtime файла перепроверяются при сборке.

## Проверка и установка

Для Windows-комплекта с включённым Python отдельный Python на целевом ПК не нужен. Скопируйте также `scripts/install_windows_bundle.ps1` и независимо полученный SHA-256 архива:

```powershell
.\install_windows_bundle.ps1 -Bundle .\NeuroMorf-offline.zip -Destination C:\NeuroMorf-release -ExpectedSha256 ARCHIVE_SHA256
```

Bootstrap сначала проверяет SHA-256 всего архива, удерживая файл открытым без разрешения записи, затем извлекает только закреплённый Python и проверяющий установщик во временный каталог. Установщик проверяет все файлы перед установкой; приложение автоматически не запускается. Политики безопасности PowerShell не изменяются.

Скопируйте проверенный `offline_bundle.py`, архив и отдельно полученный SHA-256 на целевой ПК. Для проверки и установки нужен Python 3.11+; включённый внутрь ZIP Python не исполняется установщиком до проверки.

```powershell
python offline_bundle.py verify NeuroMorf-offline.zip --expected-sha256 ARCHIVE_SHA256
python offline_bundle.py install NeuroMorf-offline.zip C:/NeuroMorf-release --expected-sha256 ARCHIVE_SHA256
```

Каталог назначения должен **не существовать**, а его родитель должен существовать. Даже существующий пустой каталог отклоняется. Проверяются точный состав архива, размеры и SHA-256 всех файлов; traversal, symlinks/reparse points, case-collisions, спецфайлы и лишние файлы запрещены. Установка использует staging и эксклюзивное создание файлов. На файловых системах без hardlinks работает потоковое копирование. Предварительно требуется место для двух распакованных копий плюс небольшой запас; ZIP занимает место отдельно.

На Windows откройте `Start.cmd`. Он использует включённый Python с `-I`, а при его отсутствии — установленный `py -3`. В bundled `python313._pth` сборщик задаёт только stdlib ZIP, каталог Python и `../app`; site отключён. Это нужно также дочерним worker-процессам. На Linux/macOS запустите `sh start.sh`; Windows model-runtime там не подтверждён.

## Формат component manifest

```json
{
  "identity": "Имя компонента и вариант сборки",
  "revision": "40-64 шестнадцатеричных символа immutable commit или digest",
  "files": [
    {"path": "runtime/local-model/example.gguf", "bytes": 123, "sha256": "64 шестнадцатеричных символа"}
  ],
  "license_files": ["runtime/local-model/LICENSE.txt"]
}
```

Это схема, не готовый валидный пример: все указанные файлы лицензий тоже должны присутствовать в `files`. Model paths начинаются `runtime/local-model/` относительно `--model-root`. Python paths указываются относительно `--python-root`, без префикса `python/`. Для Python обязательны `python.exe`, соответствующие `pythonNNN.zip` / `pythonNNN._pth` и лицензия. Не добавляйте download-кеши, snapshots с секретами и непроверенные плагины.

## Границы проверки

SHA-256 подтверждает соответствие выбранным байтам, а не подлинность неизвестного издателя и не безопасность произвольного кода. Origin, лицензии и доверенные pins принимаются отдельно оператором. Этот установщик не подтверждает «безопасность всех навыков», не устанавливает OpenClaw skills/MCP из сети, не включает произвольное выполнение модельного текста и не открывает LAN-порты. Модельные и научные результаты требуют отдельной оценки.

Offline здесь означает, что проверенный локальный runtime может работать без сетевого API. Это не самообучение весов, не гарантированный непрерывный исследовательский цикл и не доступ к нативным чатам облачных провайдеров. Проверки установщика с маленькими fixtures, реальная установка большого пакета и live-генерация модели должны иметь отдельные квитанции.

