const $ = id => document.getElementById(id);
let sessionId = '', busy = false, ready = false;
const headers = () => ({'Content-Type': 'application/json', ...($('token').value ? {Authorization: `Bearer ${$('token').value}`} : {})});
function show(role, text) {
  const article = document.createElement('article');
  article.dataset.role = role;
  article.textContent = text;
  $('messages').append(article);
  return article;
}
async function check() {
  ready = false;
  $('send').disabled = true;
  try {
    const response = await fetch('/api/readiness', {headers: headers()});
    if (!response.ok) throw new Error(response.status === 401 ? '请填写有效的实例访问凭据。' : '配置检查失败，请检查服务。');
    const config = await response.json();
    ready = config.ready;
    $('status').textContent = ready ? '可以开始对话。' : `缺少配置：${config.missing.join('、')}。请在本机配置后重启服务。`;
  } catch (error) { $('status').textContent = error.message; }
  $('send').disabled = !ready || busy;
}
$('check').onclick = check;
$('new').onclick = () => { sessionId = ''; $('messages').replaceChildren(); $('status').textContent = '已开始新对话。'; };
$('chat').onsubmit = async event => {
  event.preventDefault();
  const message = $('message').value.trim();
  if (!message || busy || !ready) return;
  busy = true;
  $('send').disabled = $('new').disabled = $('check').disabled = true;
  show('user', message);
  $('message').value = '';
  $('status').textContent = '正在处理…';
  try {
    const response = await fetch('/chat', {method: 'POST', headers: headers(), body: JSON.stringify({message, session_id: sessionId, depth: $('depth').value})});
    if (!response.ok) {
      const messages = {401: '访问凭据无效。', 409: '当前会话正在处理另一条消息。', 429: '发送较频繁，请稍后重试。', 422: '输入不符合要求，请修改后重试。'};
      throw new Error(messages[response.status] || '请求未完成，请检查服务后重试。');
    }
    const result = await response.json();
    sessionId = result.session_id;
    const status = result.turn_result?.status;
    show('assistant', result.reply);
    $('status').textContent = status === 'failed' ? '本轮处理失败；可修改问题后重试。' : status === 'partial' ? '已回复，部分资料缺失；请留意回复中的限制。' : '本轮已结束。';
  } catch (error) {
    show('failure', '本轮未取得回复。发送内容：' + message);
    $('status').textContent = error.message === 'Failed to fetch' ? '连接中断，无法确认本轮结果。当前版本不支持查询未送达回复，请勿连续重发。' : error.message;
    $('message').value = message;
  } finally {
    busy = false;
    $('send').disabled = !ready;
    $('new').disabled = $('check').disabled = false;
  }
};
check();
