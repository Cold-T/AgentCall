'use strict';
const base = document.querySelector('meta[name="api-base"]').content;
const $ = selector => document.querySelector(selector);
const enc = encodeURIComponent;
const form = $('#task-create');
let models = [];
const voices = {openai:['marin','cedar','alloy','ash','ballad','coral','echo','sage','shimmer','verse'], gemini:['Aoede','Puck','Charon','Kore','Fenrir','Zephyr','Leda','Orus']};
const chosenVoices = new Map();
let activeTask;
let historyOffset = 0;
let contactsRequest = 0;
let detailRequest = 0;
const states = {saved:'待执行', queued:'排队中', preparing:'准备中', dialing:'拨号中', in_call:'通话中', finalizing:'整理结果', ended:'已结束', active:'通话中', incoming:'来电', outgoing:'呼出'};
const outcomes = {completed:'已完成', partial:'部分完成', incomplete:'未完成', cancelled:'已取消', service_restart:'服务重启', failed:'失败'};
function status(value) { return states[value] || outcomes[value] || value || '—'; }
function time(value) { return value ? new Date(value).toLocaleString() : '—'; }
function notice(message, error = false) {
  $('#notice').hidden = false;
  $('#notice').classList.toggle('error', error);
  $('#notice').textContent = message;
}
async function api(path, method = 'GET', body) {
  const response = await fetch(base + path, {
    method, credentials: 'same-origin',
    headers: {'X-AgentCall-CSRF': '1', ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (response.status === 401) { location.reload(); throw new Error('登录已失效，请重新输入 PIN。'); }
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查填写内容。');
  return data;
}
async function run(operation, item) {
  if (item) item.disabled = true;
  try { await operation(); } catch (error) { notice(error.message, true); }
  finally { if (item) item.disabled = false; }
}
function button(label, action) {
  const item = document.createElement('button'); item.type = 'button'; item.textContent = label;
  item.onclick = () => run(action, item); return item;
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
    for (const [key] of columns) tr.insertCell().textContent = String(row[key] ?? '—');
    if (actions) { const cell = tr.insertCell(); for (const [label, action] of actions(row)) cell.append(button(label, action)); }
  }
  parent.append(element);
}
async function tab(name) {
  document.querySelectorAll('main > section').forEach(item => { item.hidden = item.id !== name; });
  document.querySelectorAll('nav button').forEach(item => {
    const selected = item.dataset.tab === name;
    item.setAttribute('aria-selected', String(selected)); item.tabIndex = selected ? 0 : -1;
  });
  if (name === 'call') await Promise.all([refreshContacts(), currentCalls()]);
  if (name === 'history') await refreshHistory();
}
const tabs = [...document.querySelectorAll('nav button')];
for (const [index, item] of tabs.entries()) {
  item.onclick = () => run(() => tab(item.dataset.tab));
  item.onkeydown = event => {
    const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : undefined;
    if (next !== undefined) { event.preventDefault(); tabs[next].focus(); tabs[next].click(); }
  };
}
$('#logout').onclick = () => run(async () => { await api('/session/logout', 'POST'); location.reload(); });
async function refreshDevices() {
  const devices = await api('/devices');
  table('#device-list', devices.map(row => ({...row, bluetooth:row.connected ? '已连接' : '未连接', phone:row.hfp_ready ? '可通话' : '未就绪'})), [['name','设备'], ['address','地址'], ['bluetooth','蓝牙'], ['phone','通话状态']], row => {
    const id = row.id || row.path.split('/').pop();
    return [['pair','配对'], ['connect','连接'], ['disconnect','断开'], ['sync','同步联系人与历史']].map(([action,label]) => [label, async () => {
      await api(`/devices/${enc(id)}/${action}`, 'POST'); notice(label + '请求已完成'); await refreshDevices();
    }]);
  });
  document.querySelectorAll('.device-select').forEach(select => {
    const selected = select.value; select.replaceChildren();
    if (select.dataset.empty) select.add(new Option(select.dataset.empty, ''));
    else select.add(new Option(devices.length ? '请选择通话设备' : '请先连接设备', ''));
    for (const device of devices) select.add(new Option((device.name || device.address) + (device.connected ? ' · 已连接' : ''), device.id || device.path.split('/').pop()));
    if (selected && [...select.options].some(option => option.value === selected)) select.value = selected;
    else if (!select.dataset.empty) {
      const device = devices.find(item => item.hfp_ready) || devices.find(item => item.connected);
      if (device) select.value = device.id || device.path.split('/').pop();
    }
  });
  await refreshContacts();
}
$('#refresh-devices').onclick = () => run(refreshDevices, $('#refresh-devices'));
document.querySelectorAll('[data-discovery]').forEach(item => item.onclick = () => run(async () => {
  await api('/discovery/' + item.dataset.discovery, 'POST'); notice('设备搜索状态已更新'); await refreshDevices();
}, item));
$('#refresh-pairing').onclick = () => run(async () => {
  const data = await api('/pairing'); const parent = $('#pairing-list'); parent.replaceChildren();
  if (!data.pending_ids.length) { parent.textContent = '没有等待确认的配对'; return; }
  for (const id of data.pending_ids) {
    const p = document.createElement('p'); p.textContent = '请核对手机上的配对信息：' + id + ' ';
    for (const [label, accept] of [['允许', true], ['拒绝', false]]) p.append(button(label, async () => {
      await api('/pairing/' + enc(id), 'POST', {accept}); p.remove(); notice('已处理配对确认'); await refreshDevices();
    }));
    parent.append(p);
  }
}, $('#refresh-pairing'));
async function refreshContacts() {
  const request = ++contactsRequest;
  const device = form.elements.device.value;
  const select = form.elements.contact_id;
  const selected = select.value;
  if (!device) { select.replaceChildren(new Option('选择联系人或输入号码', '')); return; }
  const contacts = [];
  for (let offset = 0; ; offset += 1000) {
    const rows = await api(`/contacts?device=${enc(device)}&limit=1000&offset=${offset}`);
    if (request !== contactsRequest) return;
    contacts.push(...rows);
    if (rows.length < 1000) break;
  }
  select.replaceChildren(new Option(contacts.length ? '选择联系人或输入号码' : '暂无联系人，可输入号码或先同步', ''));
  for (const contact of contacts) select.add(new Option(`${contact.name || '未命名'} · ${contact.number}`, contact.id));
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}
form.elements.device.onchange = () => { form.elements.contact_id.value = ''; run(refreshContacts); };
form.elements.contact_id.onchange = () => { if (form.elements.contact_id.value) form.elements.number.value = ''; };
form.elements.number.oninput = () => { if (form.elements.number.value) form.elements.contact_id.value = ''; };
$('#refresh-contacts').onclick = () => run(refreshContacts, $('#refresh-contacts'));
function loadVoices() {
  const model = form.elements.model.value ? models[Number(form.elements.model.value)] : null;
  const select = form.elements.voice; select.replaceChildren();
  if (!model) { select.add(new Option('请先选择模型', '')); select.disabled = true; return; }
  select.disabled = false;
  const available = [...new Set([...(voices[model.provider] || []), model.voice].filter(Boolean))];
  for (const voice of available) select.add(new Option(voice + (voice === model.voice ? '（默认）' : ''), voice));
  const preferred = chosenVoices.get(model.provider) || model.voice;
  select.value = available.includes(preferred) ? preferred : available[0];
}
form.elements.model.onchange = loadVoices;
form.elements.voice.onchange = () => {
  const model = form.elements.model.value ? models[Number(form.elements.model.value)] : null;
  if (model) chosenVoices.set(model.provider, form.elements.voice.value);
};
async function loadModels() {
  const data = await api('/settings'); const active = data.active.provider;
  models = [
    {...(active.provider === 'openai' ? active : {provider:'openai', model:'gpt-realtime-2.1', voice:'marin', options:{}})},
    {...(active.provider === 'gemini' ? active : {provider:'gemini', model:'gemini-3.8-live', voice:'Aoede', options:{}})}
  ];
  form.elements.model.replaceChildren();
  for (const [index, model] of models.entries()) {
    const available = data.credentials[model.provider];
    const option = new Option(`${model.provider === 'openai' ? 'OpenAI' : 'Gemini'} · ${model.model}${available ? '' : '（未配置 API Key）'}`, String(index));
    option.disabled = !available; form.elements.model.add(option);
  }
  const selected = models.findIndex(model => model.provider === active.provider && data.credentials[model.provider]);
  form.elements.model.value = String(selected >= 0 ? selected : models.findIndex(model => data.credentials[model.provider]));
  loadVoices();
  $('#start-call').disabled = !models.some(model => data.credentials[model.provider]);
  $('#model-status').textContent = '双方对话转写会自动启用，可在历史详情查看。' + (data.credentials.gemini ? '' : ' Gemini 尚未配置 API Key。');
}
form.onsubmit = event => {
  event.preventDefault();
  run(async () => {
    const data = Object.fromEntries(new FormData(form));
    if (!data.contact_id && !data.number.trim()) throw new Error('请选择联系人或输入电话号码。');
    const selected = models[Number(data.model)];
    if (!selected || !data.model) throw new Error('请选择可用模型。');
    if (!data.voice) throw new Error('请选择声音。');
    const goal = data.goal.trim();
    if (!goal) throw new Error('请填写通话目标。');
    const options = {...selected.options, ...(selected.provider === 'openai' ? {transcription:selected.options?.transcription || {model:'gpt-4o-mini-transcribe'}} : {inputAudioTranscription:{}, outputAudioTranscription:{}})};
    const body = {
      device:data.device, ...(data.contact_id ? {contact_id:data.contact_id} : {number:data.number.trim()}),
      max_call_seconds:Number(data.max_call_seconds), goal, completion_criteria:goal, ...(data.background.trim() ? {background:data.background} : {}),
      config:{provider:selected.provider, model:selected.model, voice:data.voice, language:data.language, options},
      start_immediately:true
    };
    const task = await api('/tasks', 'POST', body); activeTask = task.id;
    notice('通话任务已提交，正在排队拨号。'); form.elements.goal.value = '';
    await currentCalls();
  }, $('#start-call'));
};
async function currentCalls() {
  const calls = await api('/calls/current');
  table('#current-list', calls.map(row => ({...row, stateLabel:status(row.state)})), [['number','号码'], ['stateLabel','状态']], row => [
    ['挂断', async () => { await api(`/calls/${enc(row.id)}/hangup`, 'POST'); notice('挂断请求已提交'); await currentCalls(); }]
  ]);
  if (activeTask) {
    const task = await api('/tasks/' + enc(activeTask));
    $('#task-status').textContent = `本次任务：${status(task.state)}${task.outcome ? ' · ' + status(task.outcome) : ''}`;
    $('#task-status').append(button('查看', async () => { await tab('history'); await historyDetail({task, source:'project'}); }));
    if (task.state !== 'ended') $('#task-status').append(button('取消任务', async () => {
      await api(`/tasks/${enc(activeTask)}/cancel`, 'POST'); notice('取消请求已提交'); await currentCalls();
    }));
  }
}
async function refreshHistory() {
  const device = $('#history-device').value;
  const rows = await api(`/history?limit=51&offset=${historyOffset}${device ? '&device=' + enc(device) : ''}`);
  $('#history-prev').disabled = historyOffset === 0;
  $('#history-next').disabled = rows.length <= 50;
  $('#history-page').textContent = '第 ' + (historyOffset / 50 + 1) + ' 页';
  table('#history-list', rows.slice(0,50).map(row => ({...row,
    number:row.call?.number || row.task?.input.number,
    goal:row.task?.input.goal || (row.source === 'pbap' ? '手机通话历史' : '手动通话'),
    time:time(row.recorded_at), stateLabel:status(row.task?.outcome || row.task?.state || row.call?.state),
    duration:row.call?.duration_seconds == null ? '—' : Math.round(row.call.duration_seconds) + ' 秒'
  })), [['time','时间'], ['number','号码'], ['goal','目标'], ['stateLabel','状态 / 结果'], ['duration','通话时长']], row => [
    ['查看详情', () => historyDetail(row)]
  ]);
}
async function historyDetail(row) {
  const request = ++detailRequest;
  $('#history-detail').hidden = false;
  $('#history-summary').replaceChildren();
  $('#history-downloads').replaceChildren(); $('#recording-status').textContent = '';
  $('#history-ai-summary').textContent = '正在读取…'; $('#history-summary-model').textContent = '';
  $('#history-detail').focus({preventScroll:true});
  $('#history-detail').scrollIntoView({behavior:'instant', block:'start'});
  $('#history-result').textContent = '正在读取…'; $('#history-transcript').textContent = '正在读取…';
  const task = row.task ? await api('/tasks/' + enc(row.task.id)) : null;
  const call = task?.call || (row.call ? await api(`/calls/${enc(row.call.id)}?source=${enc(row.source)}`) : null);
  const events = [];
  if (task) {
    let after = 0;
    for (;;) {
      const batch = await api(`/tasks/${enc(task.id)}/events?kind=model.transcript&limit=1000&after_id=${after}`);
      if (request !== detailRequest) return;
      events.push(...batch);
      if (batch.length < 1000) break;
      after = batch[batch.length - 1].id;
    }
  }
  if (request !== detailRequest) return;
  if (task) {
    for (const [kind, label, extension] of [['transcript','下载 Transcript','txt'], ['recording','下载录音','wav']]) {
      if (!task.downloads?.[kind]) continue;
      const link = document.createElement('a'); link.className = 'button'; link.textContent = label;
      link.href = base + `/tasks/${enc(task.id)}/${kind}`; link.download = `${kind}-${task.id}.${extension}`;
      $('#history-downloads').append(link);
    }
  }
  $('#recording-status').textContent = task?.downloads?.recording ? '录音：左声道为对方，右声道为 AI 助理。' : task && task.state !== 'ended' ? '录音在通话结束后可下载。' : '该通话没有已保存的录音。录音功能启用后的新 AI 通话会自动保存。';
  const fields = [['号码',call?.number || task?.input.number], ['目标',task?.input.goal], ['状态',status(task?.outcome || task?.state || call?.state)], ['开始时间',time(call?.started_at || task?.started_at || task?.created_at)], ['结束时间',time(call?.ended_at || task?.ended_at)], ['结束原因',call?.end_reason], ['模型',task?.config.model], ['声音',task?.config.voice], ['语言',task?.config.language]];
  for (const [label,value] of fields) {
    if (value == null) continue;
    const dt = document.createElement('dt'); dt.textContent = label;
    const dd = document.createElement('dd'); dd.textContent = value; $('#history-summary').append(dt,dd);
  }
  $('#history-result').textContent = task?.model_result ? JSON.stringify(task.model_result, null, 2) : task?.error ? JSON.stringify(task.error, null, 2) : task ? '尚未提交结果。' : '该通话没有 AI 任务结果。';
  const parent = $('#history-transcript'); parent.replaceChildren();
  let previous;
  for (const event of events) {
    const data = event.data;
    if (!data.text) continue;
    const key = data.item_id || data.response_id;
    if (data.delta && previous && previous.role === data.role && previous.key === key && !previous.finished) {
      previous.text.textContent += data.text; previous.finished = data.finished;
    } else {
      const p = document.createElement('p'); const speaker = document.createElement('strong');
      speaker.textContent = `${data.role === 'assistant' ? 'AI 助理' : '对方'} · ${time(event.time)}： `;
      const text = document.createElement('span'); text.textContent = data.text; p.append(speaker,text); parent.append(p);
      previous = {role:data.role, key, text, finished:data.finished};
    }
  }
  if (!parent.childNodes.length) parent.textContent = '暂无对话转写。旧通话未启用转写或未接通时，可能没有 transcript。';
  $('#history-detail').scrollIntoView({behavior:'instant', block:'start'});
  await loadResultSummary(task, request);
}
async function loadResultSummary(task, request, retry = false) {
  if (request !== detailRequest) return;
  const target = $('#history-ai-summary');
  if (!task || task.state !== 'ended') {
    target.textContent = task ? '通话结束后生成结果总结。' : '该通话没有 AI 任务记录可总结。'; return;
  }
  $('#history-summary-model').textContent = 'GPT-5.6 Luna'; target.textContent = '正在生成结果总结…';
  try {
    const summary = task.summary?.status === 'completed' ? task.summary : await api(`/tasks/${enc(task.id)}/summary${retry ? '?retry=true' : ''}`, 'POST');
    if (request !== detailRequest) return;
    target.textContent = summary.status === 'completed' ? summary.text : summary.error || '总结尚未完成。';
    if (summary.status !== 'completed') target.append(button('重试总结', () => loadResultSummary(task, request, true)));
  } catch (error) {
    if (request !== detailRequest) return;
    target.textContent = error.message; target.append(button('重试总结', () => loadResultSummary(task, request, true)));
  }
}
$('#refresh-history').onclick = () => run(refreshHistory, $('#refresh-history'));
$('#history-device').onchange = () => { historyOffset = 0; ++detailRequest; $('#history-detail').hidden = true; run(refreshHistory); };
$('#history-prev').onclick = () => { historyOffset = Math.max(0, historyOffset - 50); run(refreshHistory); };
$('#history-next').onclick = () => { historyOffset += 50; run(refreshHistory); };
run(async () => {
  const health = await api('/health'); $('#health').textContent = health.ready ? '服务已就绪' : '蓝牙未就绪：' + (health.bluetooth_error || '等待连接');
  await Promise.all([refreshDevices(), loadModels(), currentCalls()]);
});
let polling = false;
setInterval(async () => {
  if (document.hidden || polling) return;
  polling = true;
  try {
    if (!$('#devices').hidden) await refreshDevices();
    if (!$('#call').hidden) await currentCalls();
  } catch (error) { notice(error.message, true); }
  finally { polling = false; }
}, 5000);
