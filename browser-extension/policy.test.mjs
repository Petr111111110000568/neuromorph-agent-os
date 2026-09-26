import test from 'node:test';
import assert from 'node:assert/strict';
import {providerFor,localPage,reserveAllowed,validTask} from './policy.mjs';
test('exact host and path boundary rejects lookalikes, credentials, foreign ports',()=>{
  for(const value of ['https://chat.deepseek.com.evil.test/','https://evil.test/?u=https://chat.qwen.ai','http://chat.qwen.ai/','https://u:p@chat.qwen.ai/','https://yandex.ru/mail/','https://yandex.ru/aliceevil/','https://chat.qwen.ai:444/','https://chat.qwen.ai/?access_token=secret','https://chat.qwen.ai/#access_token=secret'])assert.equal(providerFor(value),null,value);
  assert.equal(providerFor('https://yandex.ru/alice/chat/abc/'),'alice');assert.equal(providerFor('https://chat.deepseek.com/a/chat/s/abc'),'deepseek');assert.equal(providerFor('https://www.kimi.com/'),'kimi');
});
test('local UI authority excludes other pages and ports',()=>{
  assert.equal(localPage('http://127.0.0.1:8765/assistants.html'),true);
  for(const value of ['http://127.0.0.1:8765/brain.html','http://localhost:8766/assistants.html','https://localhost:8765/assistants.html','http://x@localhost:8765/assistants.html','http://127.0.0.1.evil.test:8765/assistants.html'])assert.equal(localPage(value),false);
});
test('rolling budget and minimum interval never refund failed reservations',()=>{
  const now=200000000;const history=[0,1,2,3].map(i=>({provider:'deepseek',reserved_at:now-120000-i*60000,status:'failed'}));
  assert.equal(reserveAllowed(history,'deepseek',now),false);assert.equal(reserveAllowed(history,'kimi',now),true);
  assert.equal(reserveAllowed([{provider:'kimi',reserved_at:now-1000}],'deepseek',now),false);
  assert.equal(reserveAllowed(history,'deepseek',now+86400000),true);
});
test('task IDs are bounded structured identifiers',()=>{assert.equal(validTask('task-12345'),true);for(const id of ['x','<script>','x'.repeat(101),null])assert.equal(validTask(id),false);});
