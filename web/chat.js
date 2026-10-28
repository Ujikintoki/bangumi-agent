const $ = id => document.getElementById(id);
let current = null, busy = false, ready = false, connected = false;
const contextFields = ['object_url', 'progress', 'corrections', 'preferences', 'materials'];
const headers = () => ({'Content-Type': 'application/json', ...($('token').value ? {Authorization: `Bearer ${$('token').value}`} : {})});
const status = text => { $('status').textContent = text; };
function controls() {
  const pending = current?.turns.some(turn => turn.status === 'pending');
  for (const id of ['send', 'save-context']) $(id).disabled = !ready || busy || !current || pending;
  for (const id of ['new', 'delete', 'sessions', 'check', 'refresh']) $(id).disabled = busy || !connected;
}
async function request(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers: headers(), ...(body ? {body: JSON.stringify(body)} : {})});
  if (!response.ok) {
    const messages = {401: '请填写有效的实例访问凭据。', 404: '会话已不存在，请新建对话。', 409: '会话状态已变化或正在执行，请刷新状态。', 422: '输入不符合要求；请检查条目链接、长度和必填内容（背景总长最多8000字）。', 429: '发送较频繁，请稍后重试。', 503: '会话存储不可用，未确认保存；请检查磁盘空间和数据目录。'};
    throw new Error(messages[response.status] || '请求未完成，请检查服务后刷新状态。');
  }
  return response.json();
}
function show(role, text) {
  const article = document.createElement('article');
  article.dataset.role = role;
  article.textContent = text;
  $('messages').append(article);
}
function display() {
  $('messages').replaceChildren();
  for (const turn of current?.turns || []) {
    show('user', turn.message);
    if (turn.response) {
      show(turn.status === 'failed' ? 'failure' : 'assistant', turn.response.reply);
      const sources = turn.response.turn_result?.sources;
      if (sources?.length) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = '本轮来源与范围（历史数据不代表当前值）';
        details.append(summary);
        for (const source of sources) {
          const note = document.createElement('p');
          const outcomes = {data: '取得资料', partial: '部分资料缺失', error: '读取失败', empty: '无可用资料'};
          const sample = source.comments_sample;
          note.textContent = (outcomes[source.outcome] || '本轮资料') + (sample ? `；短评样本 ${sample.sampled} 条，记录总数 ${sample.total} 条` : '') + (source.recorded_at ? '；保存于 ' + new Date(source.recorded_at).toLocaleString() : '');
          details.append(note);
          for (const item of [...(sample?.items || []), ...(source.reviews_sample?.items || [])]) {
            const paragraph = document.createElement('p');
            paragraph.textContent = `${item.author || '作者未提供'}：${item.excerpt || item.title || ''}${item.truncated ? '（已截断）' : ''}`;
            details.append(paragraph);
            if (item.url) {
              try {
                const url = new URL(item.url);
                if (['https:', 'http:'].includes(url.protocol) && ['bgm.tv', 'bangumi.tv', 'chii.in'].includes(url.hostname)) {
                  const link = document.createElement('a');
                  link.href = url.href;
                  link.textContent = '查看原始出处';
                  link.target = '_blank';
                  link.rel = 'noopener noreferrer';
                  details.append(link);
                }
              } catch (_) { /* 不把无效地址展示为可点击出处。 */ }
            }
          }
        }
        $('messages').append(details);
      }
      if (turn.status === 'failed') show('failure', '本轮失败，未加入后续模型历史。');
    } else show('failure', turn.status === 'pending' ? '本轮正在执行。可刷新状态查看结果，不会重复执行。' : '本轮已中断；不会自动重放模型或工具。');
  }
  for (const key of contextFields) $(key).value = current?.context[key] || '';
  controls();
}
async function listing() {
  const sessions = await request('/api/sessions');
  $('sessions').replaceChildren();
  for (const session of sessions) {
    const option = document.createElement('option');
    option.value = session.id;
    option.textContent = session.title;
    $('sessions').append(option);
  }
  if (current) $('sessions').value = current.id;
  return sessions;
}
async function load(sid) {
  current = await request(`/api/sessions/${sid}`);
  localStorage.setItem('bgm-selected-session', sid);
  display();
  await listing();
}
async function create() {
  current = await request('/api/sessions', 'POST');
  localStorage.setItem('bgm-selected-session', current.id);
  display();
  await listing();
  status('已开始新对话。');
}
async function action(work) {
  try { await work(); } catch (error) { status(error.message); } finally { controls(); }
}
async function check() {
  ready = false;
  connected = false;
  controls();
  try {
    const config = await request('/api/readiness');
    connected = true;
    ready = config.ready;
    $("storage").textContent = "本机保存位置：" + config.storage_path;
    status(ready ? '可以开始对话。历史保存在本机。' : `缺少配置：${config.missing.join('、')}。请在本机配置后重启服务。`);
    // 没有模型配置也能查看和删除自己的历史。
    const sessions = await listing();
    const selected = localStorage.getItem('bgm-selected-session');
    const sid = sessions.find(item => item.id === selected)?.id || sessions[0]?.id;
    if (sid) await load(sid); else if (ready) await create();
  } catch (error) { status(error.message); }
  controls();
}
$('check').onclick = check;
$('new').onclick = () => action(create);
$('refresh').onclick = () => action(async () => { if (current) await load(current.id); status('已读取服务器保存的状态。'); });
$('sessions').onchange = () => action(async () => { await load($('sessions').value); status('已打开保存的对话。'); });
$('save-context').onclick = () => action(async () => {
  const context = Object.fromEntries(contextFields.map(key => [key, $(key).value.trim()]));
  current = await request(`/api/sessions/${current.id}/context`, 'PATCH', {version: current.version, context});
  display();
  status('背景已保存；后续理解、查询和回复会使用这些前提。');
});
$('delete').onclick = () => action(async () => {
  if (!current || !window.confirm('删除本机会话、正文、背景及来源？此操作无法撤销。')) return;
  await request(`/api/sessions/${current.id}?version=${current.version}`, 'DELETE');
  localStorage.removeItem('bgm-selected-session');
  current = null;
  display();
  const sessions = await listing();
  if (sessions.length) await load(sessions[0].id);
  status('会话已删除。');
});
$('chat').onsubmit = async event => {
  event.preventDefault();
  const message = $('message').value.trim();
  if (!message || busy || !ready || !current) return;
  busy = true;
  controls();
  const sid = current.id;
  const requestId = crypto.randomUUID();
  show('user', message);
  $('message').value = '';
  status('正在处理…');
  try {
    const turn = await request(`/api/sessions/${sid}/turns`, 'POST', {message, request_id: requestId, version: current.version, depth: $('depth').value});
    await load(sid);
    status(turn.status === 'pending' ? '正在执行，可刷新状态。' : turn.status === 'failed' ? '本轮处理失败，结果已保存。' : turn.response?.turn_result?.status === 'partial' ? '已保存回复，部分资料缺失。' : '回复已保存。');
  } catch (error) {
    status(error.message);
    try {
      await load(sid);
      const turn = await request(`/api/sessions/${sid}/turns/${requestId}`);
      status(turn.status === 'pending' ? '连接已恢复，本轮仍在执行；刷新状态即可，不需要重发。' : '已读取本轮保存的结果。');
    } catch (_) {
      status('无法确认本轮结果。请刷新状态；相同请求 ID：' + requestId + '。不要连续重发。');
    }
  } finally { busy = false; controls(); }
};
check();
