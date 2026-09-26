export const PROVIDERS = Object.freeze({
  qwen: {name:'Qwen', origins:['https://chat.qwen.ai/*']},
  deepseek: {name:'DeepSeek', origins:['https://chat.deepseek.com/*']},
  alice: {name:'Алиса / YandexGPT', origins:['https://alice.yandex.ru/*','https://yandex.ru/*']},
  kimi: {name:'Kimi', origins:['https://kimi.com/*','https://www.kimi.com/*','https://kimi.ai/*','https://www.kimi.ai/*']}
});
export function providerFor(raw) {
  try {
    const u=new URL(raw);
    if(u.protocol!=='https:' || u.username || u.password || (u.port && u.port!=='443') || [...u.searchParams.keys()].some(k=>/^(access_token|refresh_token|api_key|authorization|password|token)$/i.test(k))) return null;
    if(u.hostname==='chat.qwen.ai') return 'qwen';
    if(u.hostname==='chat.deepseek.com') return 'deepseek';
    if(u.hostname==='alice.yandex.ru' || (u.hostname==='yandex.ru' && /^\/alice(?:\/|$)/.test(u.pathname))) return 'alice';
    if(['kimi.com','www.kimi.com','kimi.ai','www.kimi.ai'].includes(u.hostname)) return 'kimi';
  } catch {}
  return null;
}
export function localPage(raw) {
  try { const u=new URL(raw); return ['http://127.0.0.1:8765','http://localhost:8765'].includes(u.origin) && u.pathname==='/assistants.html' && !u.username && !u.password; } catch {return false;}
}
export function validTask(id) {return typeof id==='string' && /^[a-zA-Z0-9_-]{8,100}$/.test(id);}
export function reserveAllowed(history, provider, now=Date.now()) {
  const recent=history.filter(x=>x.reserved_at>now-86400000);
  return recent.filter(x=>x.provider===provider).length<4 && !recent.some(x=>x.reserved_at>now-60000);
}
