# Конечная установка CLI в облаке

`bootstrap_cloud_harnesses.py` устанавливает четыре CLI и проверяет только
`--version` и `--help`. По умолчанию он печатает plan без network, subprocess и
создания каталогов. Это не подключение аккаунтов, не запуск моделей и не
настройка самостоятельного coding agent.

```text
python scripts/bootstrap_cloud_harnesses.py
python scripts/bootstrap_cloud_harnesses.py --execute --output-dir runtime/cloud-harnesses
```

`--execute` разрешён только в Linux x64 с `GITHUB_ACTIONS=true` либо непустым
`COLAB_RELEASE_TAG`; marker является проверкой среды, не механизмом авторизации.
Runner заранее предоставляет Node22>=22.19 либо Node>=24 и npm. Основной cloud
workflow использует Node24.8.0. Node23 не входит в поддерживаемый DSH диапазон.
В Colab это конечная notebook-задача, без обхода idle-limit, сервера или daemon.

## Закреплённые пакеты

[config/cloud_harnesses.json](../config/cloud_harnesses.json) содержит exact
version, registry tarball URL, SHA512 integrity и единственный entry point.
Metadata прочитаны напрямую из npm registry 26.09.2026; никакой пакет при
подготовке локально не устанавливался или исполнялся.

| CLI | Exact npm package | Entry внутри output directory |
|---|---|---|
| DSH | `@deepseek-ai/dsh@0.1.7-rc.2` | `packages/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js` |
| Qwen Code | `@qwen-code/qwen-code@0.23.0` | `packages/qwen/node_modules/@qwen-code/qwen-code/cli-entry.js` |
| Gemini CLI | `@google/gemini-cli@0.58.0` | `packages/gemini/node_modules/@google/gemini-cli/bundle/gemini.js` |
| OpenCode | `opencode-linux-x64@1.18.27` | `packages/opencode/node_modules/opencode-linux-x64/bin/opencode` |

DSH — выбранный prerelease, не stable. OpenCode использует официальный Linux
platform package, на который ссылается `opencode-ai@1.18.27` как exact optional
dependency. Это позволяет запускать binary без postinstall оболочки opencode-ai.
Первые три entry запускаются явно через Node, последний — непосредственно.

Первичные metadata: [DSH](https://registry.npmjs.org/@deepseek-ai/dsh/0.1.7-rc.2),
[Qwen](https://registry.npmjs.org/@qwen-code/qwen-code/0.23.0),
[Gemini](https://registry.npmjs.org/@google/gemini-cli/0.58.0),
[OpenCode platform](https://registry.npmjs.org/opencode-linux-x64/1.18.27).

## Границы исполнения

Output должен быть свежим каталогом `runtime/cloud-harnesses` либо
`runtime/cloud-harnesses-<suffix>` непосредственно внутри repository checkout.
Существующий каталог не перезаписывается. Входные symlink/выход за разрешённый
путь отклоняются; root directory создаётся атомарно. Не запускайте bootstrap
из checkout, над которым одновременно работает недоверенный процесс.

Перед npm проверяются SHA512 скачанных bytes, package name/version и структура
root tar: не допускаются traversal, symlink/hardlink и превышение лимитов.
На пакет: до128MiB compressed, до512MiB unpacked, до5000 members. Download имеет
deadline до90s и socket timeout до30s, npm до180s, каждый probe до20s, суммарный
budget900s. Перехваченный stdout/stderr ограничен64KiB; в receipt хранится до8192
символов каждого шага и hash перехваченных bytes. Таймаут/overflow завершают группу
процессов. Остатки install файлов сохраняются для диагностики, автоматического
retry или повторной установки с ослабленными правилами нет.

npm использует `--ignore-scripts --no-audit --no-fund`, fixed registry, отдельные
prefix/cache/tmp/userconfig/globalconfig. Нет global install. `HOME`/XDG и cwd
создаются отдельно для каждого CLI; старые API keys, proxies, NODE_OPTIONS,
токены GitHub и пользовательские npm settings в дочерний env не переходят.
Предки cwd проверяются на `.env` и основные auto-loaded client configs.

Это ограничение собственных путей installer и fresh environment, **не OS
sandbox**. npm разрешает transitive packages; исполняемый vendor CLI может
делать больше, чем обещает help. Поэтому нужны disposable runner без secrets,
least-privilege token, hard workflow timeout и независимая рецензия pins.
Внутренние npm `.bin` symlinks могут существовать; bootstrap их не запускает.

Root tarball pin не закрепляет весь transitive graph. После install проверяется
root entry в lockfile, сохраняются сам `packages/<id>/package-lock.json`, его
SHA256, количество resolved entries и hash entry point. При следующем bootstrap
semver-зависимости могут разрешиться иначе; reproducible dependency closure
требует отдельно принятого lock и режима npm ci. Сейчас такого обещания нет.

## Квитанция и значение статусов

`runtime/cloud-harnesses/receipt.json` обновляется атомарно после каждого шага.
CLI печатает её в stdout. Для каждого пакета есть `entry_path`, `lock_sha256`,
фактические help/version outputs, archive metrics и состояние:

- `installed_smoke_passed`: install завершён, root pin совпал, оба probes успешны,
  непусты, version output содержит закреплённую версию;
- `failed`: фиксированная причина и доступные результаты предыдущих шагов.

Итог `completed` означает все smoke checks; `completed_with_failures` или
`failed` возвращают exit1; rejected config/environment/path — exit2. Plan — exit0.
`model_calls=0`, `authentication_performed=false`, `package_scripts_enabled=false`
описывают действия этого bootstrap. Это не утверждение, что provider доступен,
аккаунт аутентифицирован, inference бесплатен или все native extensions работают.
`--ignore-scripts` может оставить необязательные native features неготовыми;
bootstrap не включает scripts для исправления такого результата.

В artifact сохраняйте receipt и lockfiles, без node_modules/cache/archive,
маленький retention и проверенный0budget. Standard GitHub-hosted runners для
public repo бесплатны; storage имеет отдельные лимиты
([официальные условия](https://docs.github.com/en/billing/concepts/product-billing/github-actions)).
Colab не гарантирует24/7 lifetime или ресурсы
([FAQ](https://research.google.com/colaboratory/faq.html)).

## Бесплатный inference — отдельный допуск

[OpenCode Console guide](https://opencode.ai/console/guides) документирует free
chat models без Authorization на фиксированном chat/completions endpoint.
Это основание исследовать отдельный tokenless adapter, не включать paid catalog
или billing автоматически. [Gemini quotas](https://geminicli.com/docs/resources/quota-and-pricing/)
зависят от auth/tier. Бесплатность открытых DSH/Qwen/Gemini/OpenCode CLI сама по
себе не делает каждый provider бесплатным. Bootstrap не обращается к моделям,
не создаёт OAuth sessions и не переносит пользовательские cookies.
