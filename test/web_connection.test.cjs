const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function page(fetch, {missing = []} = {}) {
  const ids = new Set([...fs.readFileSync("web/index.html", "utf8").matchAll(/id="([^"]+)"/g)].map(match => match[1]));
  const fields = new Map();
  const element = () => ({value: '', disabled: false, textContent: '', style: {}, dataset: {}, append() {}, replaceChildren() {}});
  const sandbox = {
    document: {getElementById(id) {if (!ids.has(id) || missing.includes(id)) return null; if (!fields.has(id)) fields.set(id, element()); return fields.get(id);}, createElement: element},
    fetch, AbortController, crypto:webcrypto, setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms, 10)), clearTimeout,
    localStorage: {getItem() {return null;}, setItem() {}, removeItem() {}},
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync('web/chat.js', 'utf8'), sandbox);
  return {sandbox, fields};
}
const settle = () => new Promise(resolve => setTimeout(resolve, 25));

test('disconnected page keeps a reconnect action available', async () => {
  const {fields} = page(async () => {throw new TypeError('Failed to fetch');});
  await settle();
  assert.equal(fields.get('check').disabled, false);
  assert.equal(fields.get('send').disabled, true);
});

test('refresh without a loaded session does not claim successful history retrieval', async () => {
  const {fields} = page(async () => {throw new TypeError('Failed to fetch');});
  await settle();
  await fields.get('refresh').onclick();
  assert.notEqual(fields.get('status').textContent, '已读取服务器保存的状态。');
});

test('an unresponsive connection has a bounded wait', async () => {
  const {sandbox} = page((url, options) => new Promise((resolve, reject) => {
    options.signal?.addEventListener('abort', () => reject(new Error('aborted')), {once:true});
  }));
  const result = await Promise.race([
    vm.runInContext("request('/health').then(() => 'unexpected', () => 'timeout')", sandbox),
    new Promise(resolve => setTimeout(() => resolve('hung'), 100)),
  ]);
  assert.equal(result, 'timeout');
});

test('refresh preserves unsaved context edits as well as the message draft', async () => {
  const {fields, sandbox} = page(async path => ({ok:true, json:async () => {
    if (path === '/api/readiness') return {ready:true, storage_path:'test.sqlite3'};
    if (path === '/api/sessions') return [{id:'saved',title:'saved'}];
    return {id:'saved',context:{object_url:'https://bgm.tv/subject/1'},turns:[]};
  }}));
  await settle();
  fields.get('object_url').value = 'https://bangumi.tv/subject/554779';
  sandbox.document.getElementById('message').value = 'unsent draft';
  await fields.get('refresh').onclick();
  assert.equal(fields.get('object_url').value, 'https://bangumi.tv/subject/554779');
  assert.equal(fields.get('message').value, 'unsent draft');
});

test('restored running turn is polled to completion without posting again', async () => {
  let polls = 0, posts = 0;
  const {fields} = page(async (path, options) => {
    if (options.method === 'POST') posts++;
    if (path.endsWith('/turns/r')) polls++;
    return {ok:true, json:async () => {
      if (path === '/api/readiness') return {ready:true, storage_path:'test.sqlite3'};
      if (path === '/api/sessions') return [{id:'saved',title:'saved'}];
      const turn = {request_id:'r', message:'saved input', status:polls ? 'completed' : 'running', response:polls ? {reply:'saved output'} : null};
      if (path.endsWith('/turns/r')) return turn;
      return {id:'saved', context:{}, turns:[turn]};
    }};
  });
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.ok(polls >= 1);
  assert.equal(posts, 0);
  assert.equal(fields.get('cancel').disabled, true);
  assert.equal(fields.get('send').disabled, false);
});


test('a previously opened page without the cancel button remains usable', async () => {
  let context;
  assert.doesNotThrow(() => {
    context = page(async () => {throw new TypeError('Failed to fetch');}, {missing:['cancel']});
  });
  await settle();
  assert.equal(context.fields.get('check').disabled, false);
});

test('a rejected send preserves input and the actual rejection reason', async () => {
  const {fields,sandbox} = page(async (path, options) => {
    if (options.method === 'POST') return {ok:false,status:409};
    if (path.includes('/turns/')) return {ok:false,status:404};
    return {ok:true,json:async () => {
      if (path === '/api/readiness') return {ready:true,storage_path:'test.sqlite3'};
      if (path === '/api/sessions') return [{id:'saved',title:'saved'}];
      return {id:'saved',version:0,context:{},turns:[]};
    }};
  });
  await settle();
  sandbox.document.getElementById('message').value = 'do not lose this input';
  await fields.get('chat').onsubmit({preventDefault(){}});
  assert.equal(fields.get('message').value, 'do not lose this input');
  assert.match(fields.get('status').textContent, /状态已变化/);
});

test('late session reads cannot replace a more recently selected session', async () => {
  let release;
  const {fields,sandbox} = page(async path => {
    if (path === '/api/sessions/old') await new Promise(resolve => {release=resolve;});
    return {ok:true,json:async () => {
      if (path === '/api/readiness') return {ready:true,storage_path:'test.sqlite3'};
      if (path === '/api/sessions') return [{id:'saved',title:'saved'}];
      return {id:path.split('/').pop(),context:{corrections:path},turns:[]};
    }};
  });
  await settle();
  const stale = vm.runInContext("load('old')", sandbox);
  await vm.runInContext("load('new')", sandbox);
  release();
  await stale;
  assert.equal(fields.get('corrections').value, '/api/sessions/new');
});

test('acceptance clears only the submitted message and keeps background edits', async () => {
  let acknowledge;
  const {fields,sandbox} = page(async (path, options) => {
    if (options.method === 'POST') {
      await new Promise(resolve => {acknowledge=resolve;});
      return {ok:true,json:async () => ({status:'completed',response:{reply:'saved'}})};
    }
    return {ok:true,json:async () => {
      if (path === '/api/readiness') return {ready:true,storage_path:'test.sqlite3'};
      if (path === '/api/sessions') return [{id:'saved',title:'saved'}];
      return {id:'saved',version:0,context:{corrections:'saved background'},turns:[]};
    }};
  });
  await settle();
  sandbox.document.getElementById('message').value = 'submitted';
  fields.get('corrections').value = 'unsaved edit';
  const sending = fields.get('chat').onsubmit({preventDefault(){}});
  fields.get('message').value = 'next question drafted while waiting';
  acknowledge();
  await sending;
  assert.equal(fields.get('message').value, 'next question drafted while waiting');
  assert.equal(fields.get('corrections').value, 'unsaved edit');
});

test('refresh after another tab deletes a session does not copy its background to another session', async () => {
  let removed = false;
  const {fields} = page(async path => ({ok:true,json:async () => {
    if (path === '/api/readiness') return {ready:true,storage_path:'test.sqlite3'};
    if (path === '/api/sessions') return [{id:removed?'other':'saved',title:'session'}];
    return {id:removed?'other':'saved',version:0,context:{corrections:removed?'other background':'original background'},turns:[]};
  }}));
  await settle();
  fields.get('corrections').value = 'old unsaved correction';
  removed = true;
  await fields.get('refresh').onclick();
  assert.equal(fields.get('corrections').value, 'other background');
});
