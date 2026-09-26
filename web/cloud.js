'use strict';
const byId = id => document.getElementById(id);
const date = value => Number.isInteger(value) ? new Date(value * 1000).toLocaleString('ru-RU') : 'не установлено';
const labels = {response_received: 'Ответ получен', provider_unavailable: 'Провайдер недоступен', rate_limited: 'Лимит провайдера', access_denied: 'Доступ отклонён', contract_changed: 'Изменился контракт провайдера', no_result: 'Нет результата', failed: 'Попытка не завершена', invalid_model_json: 'Ответ не прошёл проверку формата'};
let observation = null;
function text(id, value) { byId(id).textContent = value; }
function freshness() {
  if (!observation?.snapshot) return;
  const age = Math.max(0, Math.floor(Date.now() / 1000) - observation.snapshot.fetched_at);
  text('snapshot-time', 'Журнал прочитан: ' + date(observation.snapshot.fetched_at) + ' · возраст снимка: ' + age + ' с.');
  if (observation.stale || age >= 300) text('notice', 'Снимок устарел. Обновите наблюдение; текущая доступность модели не подтверждена.');
}
function render(data) {
  observation = data;
  text('notice', data.stale ? 'Обновление не удалось. Ниже архивный снимок; текущая доступность не подтверждена.' : 'Прочитан публичный журнал. Успех рабочего процесса и ответ модели проверяются отдельно.');
  const s = data.snapshot;
  if (!s) { text('notice', 'Публичный журнал сейчас недоступен. Данных для утверждения об успехе нет.'); return; }
  const metrics = byId('metrics'); metrics.replaceChildren();
  for (const [label, value] of [['Попыток всего', s.attempts], ['Принятых ответов', s.successes], ['Активная попытка', s.pending ? 'Есть' : 'Нет'], ['Дополнительные расходы', '0']]) {
    const card = document.createElement('div'); card.className = 'stat-card';
    const caption = document.createElement('span'); caption.textContent = label;
    const number = document.createElement('strong'); number.textContent = String(value);
    card.append(caption, number); metrics.append(card);
  }
  const last = s.last_attempt;
  text('last-status', last ? (labels[last.status] || last.status) : 'Попыток пока нет');
  byId('last-status').className = last?.status === 'response_received' ? '' : 'warning';
  text('last-time', 'Время результата попытки: ' + date(last?.at));
  text('last-role', last ? 'Роль: ' + last.role + ' · попытка ' + last.attempt : '');
  const link = byId('run-link'); link.hidden = true;
  if (last && /^\d+$/.test(String(last.run_id))) { link.href = 'https://github.com/Petr111111110000568/neuromorph-agent-os/actions/runs/' + last.run_id; link.hidden = false; }
  text('next-due', s.provider_blocked ? 'Провайдер помещён в карантин; автоматический допуск закрыт.' : 'Следующий допуск не раньше: ' + date(s.next_due));
  text('pending', s.pending ? 'Есть зарезервированная попытка. Результат ещё не подтверждён.' : 'Незавершённой попытки в снимке нет.');
  text('answer-title', s.retained_success_text?.summary || 'Подтверждённого ответа нет');
  text('answer-time', s.last_success_in_retained_journal ? 'Последний успех в сохранённом журнале: ' + date(s.last_success_in_retained_journal.at) : 'Время ответа не найдено в ограниченном журнале.');
  text('answer-text', s.retained_success_text?.research || '');
  text('snapshot-time', 'Журнал прочитан: ' + date(s.fetched_at));
  text('commit', 'Commit снимка: ' + s.ledger_commit);
  text('cache-time', 'Повторное сетевое чтение возможно после: ' + date(data.refresh_after) + '. Публичные чтения кэшируются 5 минут.');
  freshness();
}
async function refresh() {
  const button = byId('refresh'); button.disabled = true;
  try { const response = await fetch('/api/cloud-status', {cache: 'no-store'}); if (!response.ok) throw new Error(); render(await response.json()); }
  catch { text('notice', 'Не удалось прочитать журнал. Показанные ранее значения не являются свежим наблюдением.'); }
  finally { button.disabled = false; }
}
byId('refresh').addEventListener('click', refresh);
setInterval(freshness, 30000);
refresh();
