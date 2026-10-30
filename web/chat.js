const $ = id => document.getElementById(id);
let current = null, busy = false, ready = false, connected = false;
const activeStates = new Set(['accepted', 'running', 'cancelling']);
let pollTimer = null;
let navigation = 0;
const contextFields = ['object_url', 'progress', 'corrections', 'preferences', 'materials'];
const headers = () => ({'Content-Type': 'application/json', ...($('token').value ? {Authorization: `Bearer ${$('token').value}`} : {})});
const status = text => { $('status').textContent = text; };
function controls() {
  const pending = current?.turns.some(turn => activeStates.has(turn.status));
  for (const id of ['send', 'save-context']) $(id).disabled = !ready || busy || !current || pending;
  for (const id of ['new', 'delete', 'sessions']) $(id).disabled = busy || !connected;
  if ($('cancel')) $('cancel').disabled = busy || !connected || !current?.turns.some(turn => activeStates.has(turn.status));
  $('check').disabled = busy;
  $('refresh').disabled = false;
}
async function request(path, method = 'GET', body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), method === 'POST' && path.endsWith('/turns') ? 150000 : 10000);
  try {
    const response = await fetch(path, {method, headers: headers(), signal: controller.signal, ...(body ? {body: JSON.stringify(body)} : {})});
    if (!response.ok) {
      const messages = {401: '请填写有效的实例访问凭据。', 404: '会话已不存在，请新建对话。', 409: '会话状态已变化或正在执行，请刷新状态。', 422: '输入不符合要求；请检查条目链接、长度和必填内容（背景总长最多8000字）。', 429: '发送较频繁，请稍后重试。', 503: '会话存储不可用，未确认保存；请检查磁盘空间和数据目录。'};
      const error = new Error(messages[response.status] || '请求未完成，请检查服务后刷新状态。');
      error.status = response.status;
      throw error;
    }
    return await response.json();
  } catch (error) {
    if (controller.signal.aborted || error instanceof TypeError) {
      connected = ready = false;
      controls();
      throw new Error(controller.signal.aborted ? '连接等待超时，无法确认执行结果。请检查服务并刷新状态，不要重复发送。' : '无法连接本机服务。请启动服务后点击刷新状态；未发送的输入仍保留。');
    }
    throw error;
  } finally { clearTimeout(timeout); }
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
    } else show('failure', activeStates.has(turn.status) ? ({accepted:'请求已登记，等待执行。', running:'正在执行，断开网页不会取消本轮。', cancelling:'正在请求停止；等待服务端确认。'}[turn.status]) : ({cancelled:'本轮已取消。', failed:'本轮执行失败：' + (turn.reason || 'execution'), interrupted:'本轮已中断；不会自动重放模型或工具。'}[turn.status] || '等待状态更新。'));
  }
  for (const key of contextFields) $(key).value = current?.context[key] || '';
  controls();
}
async function listing(generation = navigation) {
  const sessions = await request('/api/sessions');
  if (generation !== navigation) return sessions;
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
async function load(sid, preserveDraft = false) {
  const generation = ++navigation;
  const draft = preserveDraft && current?.id === sid ? Object.fromEntries(contextFields.map(key => [key, $(key).value])) : null;
  const session = await request(`/api/sessions/${sid}`);
  // 轮询或旧标签操作的晚到响应不能覆盖用户刚选中的会话。
  if (generation !== navigation) return false;
  current = session;
  localStorage.setItem('bgm-selected-session', sid);
  display();
  await listing(generation);
  if (generation !== navigation) return false;
  if (draft) for (const key of contextFields) $(key).value = draft[key];
  schedulePoll();
  return true;
}
function schedulePoll() {
  clearTimeout(pollTimer);
  const turn = current?.turns.find(turn => activeStates.has(turn.status));
  if (!turn || !connected) return;
  const sid = current.id;
  pollTimer = setTimeout(async () => {
    if (current?.id !== sid) return;
    try {
      const latest = await request(`/api/sessions/${sid}/turns/${turn.request_id}`);
      if (current?.id !== sid) return;
      if (!await load(sid, true)) return;
      status(activeStates.has(latest.status) ? '本轮仍在执行，可明确取消；无需重复发送。' : latest.status === 'completed' ? '回复已保存。' : '本轮已结束：' + latest.status);
    } catch (error) { status(error.message + ' 可刷新状态继续查询。'); }
  }, 1500);
}
async function create() {
  const generation = ++navigation;
  const session = await request('/api/sessions', 'POST');
  if (generation !== navigation) return;
  current = session;
  localStorage.setItem('bgm-selected-session', current.id);
  display();
  await listing();
  status('已开始新对话。');
}
async function action(work) {
  try { await work(); } catch (error) { status(error.message); } finally { controls(); }
}
async function check({preserveDraft = false} = {}) {
  const draftSid = current?.id;
  const draft = preserveDraft ? Object.fromEntries(contextFields.map(key => [key, $(key).value])) : null;
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
  if (draft && current?.id === draftSid) {
    const changed = contextFields.some(key => draft[key] !== (current?.context[key] || ''));
    for (const key of contextFields) $(key).value = draft[key];
    if (connected && changed) status('连接已恢复。背景面板保留了未保存的编辑，请先保存背景再发送。');
  }
  controls();
}
$('check').onclick = () => check({preserveDraft: true});
$('new').onclick = () => action(create);
$('refresh').onclick = () => check({preserveDraft: true});
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
  ++navigation;
  localStorage.removeItem('bgm-selected-session');
  current = null;
  display();
  const sessions = await listing();
  if (sessions.length) await load(sessions[0].id);
  status('会话已删除。');
});
if ($('cancel')) $('cancel').onclick = () => action(async () => {
  const turn = current?.turns.find(turn => activeStates.has(turn.status));
  if (!turn) return;
  await request(`/api/sessions/${current.id}/turns/${turn.request_id}/cancel`, 'POST');
  await load(current.id, true);
  status('已提交取消请求，以服务端最终状态为准。');
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
  status('正在处理…');
  try {
    const turn = await request(`/api/sessions/${sid}/turns`, 'POST', {message, request_id: requestId, version: current.version, depth: $('depth').value});
    if ($('message').value.trim() === message) $('message').value = '';
    if (!await load(sid, true)) return;
    status(activeStates.has(turn.status) ? '正在执行，可刷新状态。' : turn.status === 'failed' ? '本轮处理失败，结果已保存。' : turn.response?.turn_result?.status === 'partial' ? '已保存回复，部分资料缺失。' : '回复已保存。');
  } catch (error) {
    status(error.message);
    if ([401, 403, 404, 409, 422, 429].includes(error.status)) {
      try { await load(sid, true); } catch (_) { /* 保留原始拒绝原因及输入。 */ }
      status(error.message);
      return;
    }
    try {
      if (!await load(sid, true)) return;
      const turn = await request(`/api/sessions/${sid}/turns/${requestId}`);
      if ($('message').value.trim() === message) $('message').value = '';
      status(activeStates.has(turn.status) ? '连接已恢复，本轮仍在执行；刷新状态即可，不需要重发。' : '已读取本轮保存的结果。');
    } catch (_) {
      status('无法确认本轮结果。请刷新状态；相同请求 ID：' + requestId + '。不要连续重发。');
    }
  } finally { busy = false; controls(); }
};
check();
