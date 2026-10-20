import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentcall.tasks.models import (
    ProviderConfig,
    TaskInput,
    live_backend_instructions,
    live_instructions,
)


def test_live_ignores_inherited_realtime_controls():
    realtime = ProviderConfig()
    assert realtime.options and not realtime.is_live
    live = ProviderConfig(model="gpt-live-1", options=realtime.options)
    assert live.is_live and live.options == {}
    assert not ProviderConfig(provider="gemini", model="gpt-live-1").is_live
    assert "is_live" not in live.model_dump()
    assert (
        ProviderConfig(
            model="gpt-live-1",
            options={"turn_detection": {"create_response": False}, "truncation": "disabled"},
        ).options
        == {}
    )


@pytest.mark.parametrize("key", ["api_key", "endpoint", "backend_model", "live_interruptions"])
def test_live_rejects_unknown_or_secret_options(key):
    with pytest.raises(ValueError, match="server-only"):
        ProviderConfig(model="gpt-live-1", options={key: "value"})


def test_live_prompts_separate_voice_from_task_authority():
    task = {
        "input": TaskInput(
            device="test",
            number="123",
            goal="unique task objective",
            information={"fact": "verified fact"},
        ).model_dump(),
        "config": {"language": "中文"},
    }
    voice = live_instructions(task)
    backend = live_backend_instructions(task)
    assert "中文" in voice and "中文" in backend
    assert "unique task objective" not in voice and "verified fact" not in voice
    context = json.loads(backend.split("Task context:\n")[1])
    assert context["goal"] == task["input"]["goal"]
    assert context["information"] == task["input"]["information"]
    delegation_conditions = voice.split("Delegate to the backend when:\n", 1)[1].split(
        "Do not delegate", 1
    )[0]
    assert "IVR" not in delegation_conditions and "menu" not in delegation_conditions
    assert "You hear an IVR menu or automated recording, including a complete menu." in voice
    assert "After Luna assesses the current menu as complete" in backend
    assert "not task cancellation" in backend
    assert "Already executed" in backend
    assert "waits for playback" in backend


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_ui_live_selection_and_options_are_model_specific():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const load = source.split('async function loadModels() {')[1].split('\nform.onsubmit')[0];
const save = source.split('async function saveCallDefaults() {')[1].split('\nasync function loadModels()')[0];
const modelSelect = {options: [], replaceChildren() {this.options = []}, add(o) {this.options.push(o)}};
const elements = {model:modelSelect, voice:{value:'marin', options:[]},
  max_call_seconds:{value:300, checkValidity:() => true}};
const active = {provider:'openai', model:'gpt-live-1', voice:'marin', options:{}};
let settings = {active:{provider:active, service:{}}, credentials:{openai:true, gemini:true}, saved:{service:{}}};
let written;
const context = vm.createContext({form:{elements}, Option:function(text,value){this.text=text; this.value=value},
  $: () => ({}), loadVoices:() => {},
  api:async (path, method, body) => {
    if (method === 'PUT') {written = body; return {active:{provider:body.provider}};}
    return settings;
  }});
vm.runInContext('let models=[]; let defaultsRequest=0; let defaultsSave=Promise.resolve();' +
  'const selectedModel=()=>models[Number(form.elements.model.value)];' +
  'async function loadModels() {' + load + '\nasync function saveCallDefaults() {' + save, context);
(async () => {
  await vm.runInContext('loadModels()', context);
  assert.equal(modelSelect.options.length, 3);
  assert.equal(modelSelect.value, '1');
  settings.active.provider = {provider:'openai', model:'gpt-realtime-2.1', options:{speed:1.2}};
  await vm.runInContext('saveCallDefaults()', context);
  assert.equal(written.provider.model, 'gpt-live-1');
  assert.deepEqual(JSON.parse(JSON.stringify(written.provider.options)), {});
  vm.runInContext('models[0].options={speed:1.1}', context);
  settings.active.provider = active;
  modelSelect.value = '0';
  await vm.runInContext('saveCallDefaults()', context);
  assert.equal(written.provider.model, 'gpt-realtime-2.1');
  assert.equal(written.provider.options.speed, 1.1);
})().catch(error => {console.error(error); process.exitCode=1});
"""
    source = Path(__file__).parents[1] / "src/agentcall/api/web/app.js"
    subprocess.run(["node", "-e", script, str(source)], check=True, timeout=10)
