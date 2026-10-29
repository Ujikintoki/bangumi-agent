const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function page(fetch) {
  const fields = new Map();
  const element = () => ({value: '', disabled: false, textContent: '', style: {}, dataset: {}, append() {}, replaceChildren() {}});
  const sandbox = {
    document: {getElementById(id) {if (!fields.has(id)) fields.set(id, element()); return fields.get(id);}, createElement: element},
    fetch, AbortController, setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms, 10)), clearTimeout,
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
