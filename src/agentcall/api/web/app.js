'use strict';
const base = document.querySelector('meta[name="api-base"]').content;
const $ = selector => document.querySelector(selector);
const enc = encodeURIComponent;
let savedSettings;
let watchController;
const startKeys = new Map();
function notice(message, error = false) {
  $('#notice').hidden = false;
  $('#notice').classList.toggle('error', error);
  $('#notice').textContent = message;
}
async function api(path, method = 'GET', body, headers = {}) {
  const response = await fetch(base + path, {
    method, credentials: 'same-origin',
    headers: {'X-AgentCall-CSRF': '1', ...(body === undefined ? {} : {'Content-Type': 'application/json'}), ...headers},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (response.status === 401) { location.reload(); throw new Error('登录已失效'); }
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
  return data;
}
function jsonObject(text) {
  const value = JSON.parse(text);
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('JSON 必须是对象');
  return value;
}
function values(form) { return Object.fromEntries(new FormData(form)); }
function query(form, omit = []) {
  return new URLSearchParams(Object.entries(values(form)).filter(([k, v]) => v !== '' && !omit.includes(k))).toString();
}
function show(selector, value) { $(selector).hidden = false; $(selector).textContent = JSON.stringify(value, null, 2); }
async function run(operation, button) {
  if (button) button.disabled = true;
  try { await operation(); } catch (error) { notice(error.message, true); }
  finally { if (button) button.disabled = false; }
}
function bindForm(id, action) {
  $(id).addEventListener('submit', event => {
    event.preventDefault();
    run(() => action(event.target, event.submitter), event.submitter);
  });
}
function button(text, action) {
  const item = document.createElement('button'); item.type = 'button'; item.textContent = text;
  item.addEventListener('click', () => run(action, item)); return item;
}
function table(selector, rows, columns, actions) {
  const parent = $(selector); parent.replaceChildren();
  if (!rows.length) { parent.textContent = '暂无记录'; return; }
  const element = document.createElement('table');
  const head = element.createTHead().insertRow();
  for (const [, label] of columns) { const th = document.createElement('th'); th.textContent = label; head.append(th); }
  if (actions) { const th = document.createElement('th'); th.textContent = '操作'; head.append(th); }
  const body = element.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const [key] of columns) {
      const value = row[key]; tr.insertCell().textContent = value && typeof value === 'object' ? JSON.stringify(value) : String(value ?? '');
    }
    if (actions) {
      const cell = tr.insertCell();
      for (const [label, action] of actions(row)) cell.append(button(label, action));
    }
  }
  parent.append(element);
}
function tab(name) {
  document.querySelectorAll('main > section').forEach(item => { item.hidden = item.id !== name; });
  document.querySelectorAll('nav button').forEach(item => item.setAttribute('aria-selected', String(item.dataset.tab === name)));
}
document.querySelectorAll('nav button').forEach(item => item.addEventListener('click', () => run(async () => {
  tab(item.dataset.tab);
  if (item.dataset.tab === 'settings') await loadSettings();
}, item)));
$('#logout').onclick = () => run(async () => { await api('/session/logout', 'POST'); location.reload(); });
async function refreshDevices() {
  const devices = await api('/devices');
  table('#device-list', devices, [['name', '名称'], ['address', '地址'], ['connected', '蓝牙连接'], ['hfp_ready', 'HFP 状态']], row => {
    const id = row.id || row.path.split('/').pop();
    return [['详情', async () => notice(JSON.stringify(await api('/devices/' + enc(id)), null, 2))],
      ...[['pair', '配对'], ['connect', '连接'], ['disconnect', '断开'], ['sync', '同步联系人 / 历史']].map(([action, label]) =>
        [label, async () => { await api(`/devices/${enc(id)}/${action}`, 'POST'); notice(label + '请求完成'); await refreshDevices(); }])];
  });
  document.querySelectorAll('.device-select').forEach(select => {
    const selected = select.value; select.replaceChildren();
    if (select.dataset.empty) select.add(new Option(select.dataset.empty, ''));
    for (const item of devices) select.add(new Option((item.name || item.address || item.id) + (item.connected ? ' · 已连接' : ''), item.id || item.path.split('/').pop()));
    if ([...select.options].some(item => item.value === selected)) select.value = selected;
  });
}
$('#refresh-devices').onclick = () => run(refreshDevices);
$('#saved-devices').onclick = () => run(async () => notice(JSON.stringify(await api('/devices/saved'), null, 2)));
document.querySelectorAll('[data-discovery]').forEach(item => item.onclick = () => run(async () => {
  await api('/discovery/' + item.dataset.discovery, 'POST'); notice('发现请求已提交');
}, item));
$('#refresh-pairing').onclick = () => run(async () => {
  const data = await api('/pairing'); const parent = $('#pairing-list'); parent.replaceChildren();
  if (!data.pending_ids.length) { parent.textContent = '没有等待确认的配对'; return; }
  for (const id of data.pending_ids) {
    const p = document.createElement('p'); p.textContent = id + ' ';
    for (const [label, accept] of [['允许', true], ['拒绝', false]]) p.append(button(label, async () => {
      await api('/pairing/' + enc(id), 'POST', {accept}); p.remove(); notice('已处理配对确认');
    }));
    parent.append(p);
  }
});
bindForm('#contact-search', async form => {
  table('#contact-list', await api('/contacts?' + query(form)), [['name', '姓名'], ['number', '号码'], ['device', '手机']], row => [
    ['详情', async () => notice(JSON.stringify(await api('/contacts/' + enc(row.id)), null, 2))],
    ['用于拨号', async () => { $('#dial').elements.contact_id.value = row.id; $('#dial').elements.number.value = ''; $('#dial').elements.device.value = row.device.split('/').pop(); tab('calls'); }],
    ['用于任务', async () => { $('#task-create').elements.contact_id.value = row.id; $('#task-create').elements.number.value = ''; $('#task-create').elements.device.value = row.device.split('/').pop(); tab('tasks'); }]
  ]);
});
$('#sync-contacts').onclick = () => run(async () => {
  const device = $('#contact-search').elements.device.value;
  if (!device) throw new Error('请先选中一部手机');
  const result = await api('/devices/' + enc(device) + '/sync', 'POST'); notice(JSON.stringify(result, null, 2));
});
async function currentCalls() {
  table('#current-list', await api('/calls/current'), [['id', '通话 ID'], ['number', '号码'], ['state', '状态']], row => [
    ['选择', async () => { $('#call-control').elements.id.value = row.id; }],
    ['接听', async () => { await api(`/calls/${enc(row.id)}/answer`, 'POST'); notice('接听请求已提交'); }],
    ['挂断', async () => { await api(`/calls/${enc(row.id)}/hangup`, 'POST'); notice('挂断请求已提交'); }]
  ]);
}
$('#current-calls').onclick = () => run(currentCalls);
bindForm('#dial', async form => {
  const data = values(form); if (!data.number) delete data.number; if (!data.contact_id) delete data.contact_id;
  const result = await api('/calls', 'POST', data); $('#call-control').elements.id.value = result.id;
  notice('拨号请求已提交：' + result.id); await currentCalls();
});
bindForm('#call-control', async (form, submitter) => {
  const data = values(form); const path = '/calls/' + enc(data.id); const action = submitter.value;
  if (action === 'show') show('#call-detail', await api(path));
  else { await api(path + '/' + action, 'POST', action === 'dtmf' ? {digits: data.digits} : undefined); notice('请求已提交'); }
});
bindForm('#call-search', async form => {
  table('#call-list', await api('/calls?' + query(form)), [['number', '号码'], ['direction', '方向'], ['source', '来源'], ['state', '状态'], ['started_at', '开始时间'], ['duration_seconds', '时长（秒）'], ['end_reason', '结束原因']], row => [
    ['详情', async () => show('#call-detail', await api('/calls/' + enc(row.id) + '?source=' + enc(row.source)))],
    ...(row.source === 'project' ? [['选择', async () => { $('#call-control').elements.id.value = row.id; }]] : [])
  ]);
});
bindForm('#task-create', async form => {
  const data = values(form); for (const key of ['number', 'contact_id']) if (!data[key]) delete data[key];
  data.max_call_seconds = Number(data.max_call_seconds); data.start_immediately = form.elements.start_immediately.checked;
  data.information = jsonObject(data.information); data.result_schema = jsonObject(data.result_schema); data.config = {};
  for (const key of ['provider', 'model', 'voice', 'language', 'options']) {
    if (data[key]) data.config[key] = key === 'options' ? jsonObject(data[key]) : data[key]; delete data[key];
  }
  const result = await api('/tasks', 'POST', data); $('#task-control').elements.id.value = result.id;
  notice('任务已创建：' + result.id); show('#task-detail', result);
});
$('#task-file').onchange = event => run(async () => {
  const file = event.target.files[0]; if (!file) return;
  const task = jsonObject(await file.text()); const form = $('#task-create');
  form.reset();
  for (const item of form.elements) {
    if (!item.name) continue;
    const value = ['provider', 'model', 'voice', 'language', 'options'].includes(item.name) ? (task.config || {})[item.name] : task[item.name];
    if (item.type === 'checkbox') item.checked = value === true;
    else if (value !== undefined && value !== null) item.value = typeof value === 'object' ? JSON.stringify(value, null, 2) : value;
    else if (['provider', 'model', 'voice', 'language', 'options', 'number', 'contact_id', 'background', 'completion_criteria'].includes(item.name)) item.value = '';
  }
  if (task.device) form.elements.device.value = task.device.split('/').pop();
  notice('任务 JSON 已导入，请检查后创建');
});
bindForm('#task-search', async form => {
  table('#task-list', (await api('/tasks?' + query(form))).map(row => ({...row, number:row.input.number})), [['id', '任务 ID'], ['number', '号码'], ['state', '状态'], ['outcome', '结束结果']], row => [
    ['选择 / 详情', async () => { $('#task-control').elements.id.value = row.id; show('#task-detail', await api('/tasks/' + enc(row.id))); }]
  ]);
});
bindForm('#task-control', async (form, submitter) => {
  const data = values(form); const action = submitter.value; const path = '/tasks/' + enc(data.id);
  let result;
  if (action === 'start') {
    const key = data.idempotency_key || startKeys.get(data.id) || crypto.randomUUID(); startKeys.set(data.id, key);
    result = await api(path + '/start', 'POST', undefined, {'Idempotency-Key': key}); notice('启动请求已提交');
  } else if (action === 'cancel') result = await api(path + '/cancel', 'POST');
  else if (action === 'context') result = await api(path + '/context', 'POST', {text: data.text});
  else result = await api(path + (action === 'show' ? '' : '/' + action));
  show('#task-detail', result);
});
bindForm('#event-search', async form => show('#event-output', await api('/events/history?' + query(form))));
$('#stop-watch').onclick = () => { if (watchController) watchController.abort(); notice('订阅已停止'); };
$('#watch').onclick = () => run(async () => {
  if (watchController) watchController.abort(); const controller = new AbortController(); watchController = controller;
  try {
    const response = await fetch(base + '/events?' + query($('#event-search'), ['after_id', 'limit']), {credentials: 'same-origin', signal: controller.signal});
    if (response.status === 401) { location.reload(); return; }
    if (!response.ok) throw new Error('无法订阅事件：' + response.status);
    notice('实时订阅已连接'); const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; const lines = [];
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let end;
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const data = block.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n');
        if (data) { lines.push(data); if (lines.length > 200) lines.shift(); $('#event-output').textContent = lines.join('\n'); }
      }
    }
    notice('事件连接已结束，可重新订阅');
  } catch (error) { if (error.name !== 'AbortError') throw error; }
});
const labels = {
  host:'监听地址', port:'监听端口', root_path:'API 路径前缀', adapter:'蓝牙适配器', codec:'SCO 编码', obex_bus:'OBEX D-Bus', database:'SQLite 数据库路径',
  pin_auth:'PIN 认证（只读）', reconnect_seconds:'重连间隔（秒）',
  model_connect_seconds:'模型连接超时（秒）', answer_timeout_seconds:'接通超时（秒）', audio_timeout_seconds:'音频就绪超时（秒）', hangup_timeout_seconds:'结束语等待超时（秒）'
};
const hiddenServiceFields = new Set(['token_env', 'api_key_env', 'gemini_api_key_env']);
async function loadSettings() {
  const response = await api('/settings'); savedSettings = response.saved;
  const parent = $('#service-fields'); parent.replaceChildren();
  for (const [name, value] of Object.entries(savedSettings.service)) {
    if (hiddenServiceFields.has(name)) continue;
    const label = document.createElement('label'); label.textContent = labels[name] || name;
    let input;
    const choices = {codec:['cvsd','msbc'], obex_bus:['session','system']};
    if (choices[name]) { input = document.createElement('select'); for (const option of choices[name]) input.add(new Option(option, option)); }
    else { input = document.createElement('input'); input.type = typeof value === 'number' ? 'number' : 'text'; if (input.type === 'number') { input.step = name === 'port' ? '1' : 'any'; input.min = name === 'port' ? '1' : '0.001'; } }
    input.name = 'service_' + name; input.value = String(value); input.readOnly = response.read_only.includes(name); label.append(input); parent.append(label);
  }
  const form = $('#settings-form'); for (const [key, value] of Object.entries(savedSettings.provider)) form.elements[key].value = key === 'options' ? JSON.stringify(value, null, 2) : value;
  $('#settings-status').textContent = response.restart_required.length ? '需重启服务才能生效：' + response.restart_required.map(key => labels[key] || key).join('、') : '已保存配置与当前运行配置一致。';
  if (!response.persistent) $('#settings-status').textContent = '当前服务未指定配置文件，无法持久化设置。';
  $('#credential-status').textContent = `OpenAI：${response.credentials.openai ? '已配置' : '未配置'} · Gemini：${response.credentials.gemini ? '已配置' : '未配置'} · PIN：${response.credentials.pin ? '已设置' : '未设置'}`;
}
$('#reload-settings').onclick = () => run(loadSettings);
$('#settings-form').elements.provider.onchange = event => {
  const form = $('#settings-form'); const gemini = event.target.value === 'gemini';
  form.elements.model.value = gemini ? 'gemini-3.8-live' : 'gpt-realtime-2.1'; form.elements.voice.value = gemini ? 'Aoede' : 'marin'; form.elements.options.value = '{}';
};
bindForm('#settings-form', async form => {
  const data = values(form); const service = {};
  for (const [key, value] of Object.entries(savedSettings.service)) service[key] = hiddenServiceFields.has(key) || typeof value === 'boolean' ? value : typeof value === 'number' ? Number(data['service_' + key]) : data['service_' + key];
  await api('/settings', 'PUT', {service, provider:{provider:data.provider, model:data.model, voice:data.voice, language:data.language, options:jsonObject(data.options)}});
  await loadSettings(); notice('配置已保存');
});
async function saveCredentials(form) {
  const data = values(form);
  if ('confirm' in data && data.pin !== data.confirm) throw new Error('两次 PIN 不一致');
  delete data.confirm;
  for (const key of Object.keys(data)) if (!data[key]) delete data[key];
  if (!Object.keys(data).length) throw new Error('请填写需要修改的凭据');
  const result = await api('/settings/credentials', 'PUT', data); form.reset();
  if (result.login_required) { location.reload(); return; }
  await loadSettings(); notice('API Key 已保存');
}
bindForm('#api-keys', saveCredentials);
bindForm('#credentials', saveCredentials);
run(async () => {
  const status = await api('/health'); $('#health').textContent = status.ready ? `服务已就绪 · ${status.adapter} · ${status.codec}` : '蓝牙未就绪：' + (status.bluetooth_error || '等待连接');
  await refreshDevices(); await currentCalls();
});
