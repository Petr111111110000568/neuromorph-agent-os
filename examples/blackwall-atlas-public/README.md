# Atlas public demo

Atlas is a small, static task-board demonstration. Its six cards, labels, statuses and evidence statements are entirely fictional. They are not a project snapshot, research findings or records of real people or agents.

## What is included

- Original HTML, CSS and browser JavaScript written for this sanitized package
- One explicitly synthetic dataset
- A proposal-and-review participation guide
- Task and result templates
- Local checks for the file allowlist, synthetic data and links

The interface has search, a status filter and read-only detail dialogs. It uses system fonts and has no external assets, analytics, package dependencies or remote service requests. It fetches only the adjacent synthetic JSON file. Nothing is saved in browser storage.

## Honest boundaries

There is no backend, functioning MCP server or MCP connection, authentication, live synchronization, work queue, scheduler, assignment mechanism or automatic execution. A status on the page is an example, not a permission or a completion claim.

Opening the page, downloading the package or submitting a proposal does not launch an agent or grant rights. This package does not request administrator access, credentials, API keys or access to a private environment. It does not perform self-replication, persistence, autonomous resource acquisition or deployments.

Participation is by proposals and human review. Execution, access and publication require separate authorization for the specific scope. See [CONTRIBUTING](CONTRIBUTING.md).

## View locally

Run these commands from this package directory using an existing Python installation:

```sh
python3 checks/check_public.py
python3 -m http.server 8000 --bind 127.0.0.1 --directory .
```

Open the loopback address on port 8000 in your browser. Stop the server with Ctrl+C. The bind address intentionally restricts this temporary preview to your own computer. Do not serve a parent directory or private working tree. Opening the HTML directly as a file may block the JSON request.

No install script or automatic startup is provided. Do not add credentials to this package.

## Checks

Required checks use Python's standard library only:

```sh
python3 checks/check_public.py
python3 checks/test_check_public.py
```

Optional JavaScript and real-browser checks use an already installed Node.js, Playwright and Chromium. Playwright is a test-only tool; it is not bundled or loaded by the demo. No dependency installation is automatic.

```sh
node --check app.js
node --check checks/test_ui.cjs
node checks/test_ui.cjs
```

The browser check starts a temporary loopback server, tests desktop and narrow layouts, filters, repeated resets, dialogs and load failures, and stops the server when finished. It rejects non-local requests. If Chromium is not on the executable search path, set `ATLAS_TEST_BROWSER` to your existing browser executable. A missing test tool means the browser check was not run; do not report it as passed.

`allowlist.txt` is the complete release file list. The validator rejects extra files, symlinks, hidden deployment metadata, many credential/identity patterns, external links and non-synthetic dataset structure. It also verifies local documentation targets and HTML fragments. The tests deliberately corrupt temporary copies to ensure the guard fails.

The parent repository core test suite and its doctor command were not run for this standalone package. No complete parent checkout was used. Package checks do not establish parent-system integration, installation or operational readiness.

These are screening checks, not a proof that arbitrary future edits contain no sensitive material. Review every change manually and rerun checks before any release. Future automation or data imports require a new security and privacy review.

## Validation of this prepared snapshot

The allowlist/synthetic-data/link validator, its 18 regression tests and both Node.js syntax checks passed during preparation. The optional real-browser suite was attempted but could not start Chromium because the execution environment blocked browser startup; the supported elevated attempt also failed before startup. Browser behavior and visual layout therefore remain unverified for this snapshot. No passing browser result is claimed. The parent repository core tests and doctor were not run.

## Data and contributions

- [Synthetic dataset](demo-data.json)
- [Task proposal template](templates/task.md)
- [Result template](templates/result.md)
- [Contribution rules](CONTRIBUTING.md)
- [License and provenance status](LICENSE_STATUS.md)

## Publication and license status

This is a prepared local package. These instructions do not publish it or create a repository. No deployment configuration, repository metadata, real reports or research data are included.

No open-source license has been selected or granted in this package. Public visibility alone would not provide reuse rights. The rights holder must verify provenance and approve a license before presenting this as a reusable open-source release. See [LICENSE_STATUS](LICENSE_STATUS.md) for the limited provenance review and remaining decisions.
