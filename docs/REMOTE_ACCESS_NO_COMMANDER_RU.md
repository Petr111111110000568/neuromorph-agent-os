# Альтернативное управление openKylin без Remote Desktop Commander

**Текущий статус:** протокол и read-only probe подготовлены, но прямое подключение ChatGPT к пользовательскому ПК не установлено. GitHub Connector умеет читать/изменять репозитории и проверять GitHub Actions, однако он не является SSH-клиентом. При исчерпанной квоте Remote Desktop Commander установку на ПК из чата выполнить нельзя.

## Что работает без удалённого агента

- Проект: [Petr111111110000568/neuromorph-agent-os](https://github.com/Petr111111110000568/neuromorph-agent-os).
- GitHub Connector: публикация, независимая проверка исходников/тестов, Issues/PR и получение доступных артефактов Actions.
- GitHub-hosted runners: облачные процессы, которые **не** дают прямого доступа к вашей видеокарте или локальным файлам.
- Сохранённый статус среды в data/environment_status.json исторический, не текущая проверка openKylin.

## Рекомендуемый следующий этап — приватный канал

**Не привязывайте персональный компьютер к self-hosted GitHub runner публичного репозитория!** Для публичных репозиториев GitHub указывает риск запуска недоверенного кода на self-hosted runner. Вместо этого:

1. Создайте **отдельный PRIVATE** GitHub-репозиторий, например neuromorph-control-private, и дайте уже установленному GitHub Connector доступ только к нему.
2. Разместите runner на **выделенной openKylin VM** с отдельной непривилегированной учётной записью, без доступа к секретам или персональным каталогам. В новом приватном репозитории выберите **Settings → Actions → Runners → New self-hosted runner**; выполните предоставленные GitHub команды регистрации именно в VM, без запуска процесса runner от root. Метка: neuromorph-openkylin.
3. После независимой проверки и принятия этого PR скопируйте templates/private-openkylin-health.yml в новый **приватный** репозиторий по пути .github/workflows/openkylin-health.yml. В шаблоне замените APPROVED_PUBLIC_COMMIT_SHA на **полный проверенный SHA** коммита, содержащего scripts/openkylin_health.py, а не на движущуюся ветку main. Защитите main приватного репозитория от непроверенных изменений. Workflow не принимает произвольные shell-команды.
4. Для одного диагностического прохода создайте новый файл requests/health-20261016-001.json в приватном репозитории и отправьте commit в main. Содержимое файла:

~~~json
{"schema_version":1,"request_id":"health-20261016-001","op":"health_snapshot"}
~~~

Новая задача использует новый ID. Статический workflow принимает ровно один новый JSON за push, проверяет schema/op/request ID и запускает только закреплённую read-only диагностику. Результат сохраняется как закрытый GitHub Actions artifact.
5. GitHub Connector сможет ставить следующие ограниченные задачи через создание файлов в приватной очереди после установки runner и предоставления доступа. Отдельно проверяйте runner online, workflow success, artifact present и probe reviewed. Эти статусы не подтверждают inference или работу биологической модели.

Локальная диагностика (только после получения проекта на вашей openKylin/WSL машине, без root):

~~~bash
python3 scripts/openkylin_health.py --output "$HOME/openkylin-health.json"
~~~

Скрипт использует stdlib Python, только читает CPU/RAM, наличие GPU-инструментов, несколько статусов служб и фиксированный перечень имён процессов; не читает аргументы/переменные среды процессов, hostname, IP, пароли, токены и cookies. Сетевых обращений, установок, управления службами и платных API нет. Вывод храните **только локально или в приватном репозитории**.

## Альтернатива для интерактивных команд

Tailscale SSH обеспечивает защищённый доступ внутри tailnet без открытия порта 22 в Интернет; используйте ограничения ACL и повторную аутентификацию. Но установленный Tailscale **сам по себе не даёт ChatGPT SSH-доступа** — для этого необходим дополнительно авторизованный SSH/MCP-инструмент, который действительно подключён к этому диалогу. Такой инструмент пока не обнаружен.

## Стоп/откат

Удалите или остановите runner в приватном репозитории, отключите его workflow и отзовите GitHub Connector permissions при необходимости. Не создавайте исполнителя произвольных команд, не публикуйте приватные диагностические данные и не раскрывайте GitHub registration token.

Официальная документация: [GitHub Actions security](https://docs.github.com/en/actions/reference/security/secure-use), [self-hosted runners](https://docs.github.com/en/actions/reference/runners/self-hosted-runners), [Tailscale Linux](https://tailscale.com/docs/install/linux), [Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh).
