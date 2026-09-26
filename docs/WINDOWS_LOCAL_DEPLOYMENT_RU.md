# Локальное развёртывание Windows

Для Windows 10/11 нужны отдельный проверенный checkout, существующий Python 3.12 venv и SQLite state вне исходников. Скрипты ничего не скачивают и не устанавливают. Venv изолирует зависимости, а Windows Job Object управляет жизненным циклом процессов. Это не контейнер и не ОС-песочница.

Структура абсолютного RuntimeRoot: `repo/` с обычным каталогом `.git`, `venv/Scripts/python.exe`, `state/` и `service/`. Последние два каталога создаются при отсутствии. Symlink/junction/reparse points, `..`, неверный Git HEAD, нарушенные builtin SHA256 и не нулевая resource policy отвергаются. Оператор заранее проверяет весь checkout: Git HEAD сам по себе не доказывает чистоту всех файлов. Pinned builtin проверяются непосредственно перед запуском.

Из канонического репозитория:

```powershell
.\scripts\start_local.ps1 -RuntimeRoot 'C:\absolute\neuromorph-local' -SourceCommit '<полный проверенный SHA: 40 hex символов>'
.\scripts\stop_local.ps1 -RuntimeRoot 'C:\absolute\neuromorph-local'
```

Start-Process скрывает служебное окно. Wrapper запускается с `-I -X utf8`. Команда child фиксирована: `python -X utf8 -m workbench --data-dir <RuntimeRoot>/state local-network --workers 2 --port 8765 --hub-port 8766 --federation-port 8767`. Произвольные команды, модели, ключи или адреса не принимаются.

Windows venv/python.exe может быть переадресатором, который создаёт реальный процесс интерпретатора. Поэтому PID Start-Process не обязан совпадать с `root_pid`, а `main_child_pid` может принадлежать переадресатору перед фактическим сервером. Start передаёт свежий `launch_id` и принимает только квитанцию с этим идентификатором; дополнительно проверяет существование root PID и точное время его создания. В вывод start добавляются отдельные `start_process_pid` и `start_process_creation_filetime`. Остановка по-прежнему использует фактический root PID/creation time и nonce, а весь процессный граф остаётся в Job.

GUI: **http://127.0.0.1:8765/brain.html**. Readiness проверяет `/api/status`, целостность builtin и нулевую финансовую policy. Локальный UI не имеет отдельного login; доверительная граница — пользователь Windows. Hub8766 использует штатный случайный bearer, gateway8767 остаётся на loopback. Запуск не подключает реальные модели/аккаунты.

Окружение child строится заново: Windows system path, временная HOME, UTF-8. Provider credentials, proxy и прочие значения пользовательского environment не копируются. Свежий state предпочтителен: local-network продолжает прежние сохранённые задания выбранной базы.

Stop читает только `service/receipt.json`, сверяет PID и время создания supervisor, атомарно записывает фиксированный stop.json с nonce текущей сессии. Supervisor опрашивает stop каждые 0,5 секунды. Посторонние PID не затрагиваются. Одновременный запуск одного runtime предотвращается OS byte-range lock.

Перед ResumeThread child приостановлен и уже назначен в Job. Job без breakaway использует KILL_ON_JOB_CLOSE. Завершение supervisor закрывает его Job handle и завершает связанные процессы, включая обычные subprocess workers. Штатный stop явно завершает Job и проверяет active-process count. Это может оборвать текущее вычисление; следует дождаться нужного результата перед остановкой. SQLite допускает аварийное завершение, но прерванная задача не получает обещания успешного результата.

`service/receipt.json` содержит root/child PID и creation time, source commit, SHA256 wrapper, URL, workers, статус и после stop `job_active_processes_after_stop`, если счётчик успешно прочитан. `running` — готовность API, а не завершение исследования. `stopped` — принятый stop; при приёмке дополнительно ожидаются active count=0 и свободные три порта. При аварийной смерти supervisor receipt может остаться running: проверять существование PID и точное время создания.

Логи: `service/child.log`, `supervisor.stdout.log`, `supervisor.stderr.log`. Ошибка назначения Job приводит к отказу, а suspended child уничтожается по его собственному handle. Права ОС не изменяются; обход Job не предусмотрен.

Приёмка оператором:

1. Doctor: actual win32, семь builtin, integrity=true, model_calls_enabled=false, audit.valid=true. Сохранённый environment_status.json прежней Linux среды не является измерением Windows.
2. HTTP200 для `/api/status`, `/brain.html`, `/brain.js`, `/brain.css`; реальные два worker PID. Небольшой quantum_circuit run завершён, provenance присутствует, сумма counts равна shots.
3. Stop: root/child и workers отсутствуют, active count=0, порты8765/8766/8767 свободны. Повторный start/stop проверяет отсутствие зависших процессов и ложного stop предыдущей сессии.
4. Общий unittest suite и doctor сохраняются отдельно. Этот документ и статическая проверка не являются свидетельством состоявшегося запуска.

Внешний harness runner остаётся POSIX-only. CPU/memory/FSIZE лимиты plugin_worker применяются только POSIX; timeout и проверка результата Windows не образуют песочницу для произвольного кода.

Первоисточники: [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects), [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject), [CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw). Они описывают управление процессами, а не разграничение доступа пользователя Windows.
