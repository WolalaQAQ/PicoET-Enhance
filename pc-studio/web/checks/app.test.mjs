import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createApp, captureProgress, deriveView, channelValue, requestJson, PRIMARY_CHANNELS, ROUTABLE_CHANNELS, MOUTH_PRESETS, PAGES, routePage, EXPRESSION_POLL_MS} from "../app.mjs";
import {DIAGNOSTIC_GROUPS, messages, preferredLanguage, initialLanguage, errorMessage, LANGUAGE_KEY, PINNED_CHANNELS_KEY} from "../i18n.mjs";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const channels = [...new Set([...PRIMARY_CHANNELS, "TongueLeft", "TongueUp", "TongueRoll", ...Array.from({length: 40}, (_, i) => `fixture${i}`)])];
class Element {
  value = ""; textContent = ""; hidden = false; disabled = false; attributes = {}; listeners = {}; children = [];
  checked = false; open = false; valid = true;
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; if (key === "src") this.src = ""; }
  checkValidity() { return this.valid; }
  emit(type) { return this.listeners[type]?.({preventDefault() {}}); }
}
function setup(t, options = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, new Element()]));
  elements.get("preview-enabled").checked = true; elements.get("guide-enabled").checked = true; elements.get("label-stage").value = "raw_only"; elements.get("person_id").value = "me"; elements.get("receive-results").checked = true;
  elements.get("cymple-host").value = "127.0.0.1"; elements.get("cymple-port").value = "23303"; elements.get("cymple-left-port").value = "45682"; elements.get("cymple-right-port").value = "45683"; elements.get("cymple-left-rotate").value = "0"; elements.get("cymple-right-rotate").value = "0";
  const doc = {documentElement: {}, hidden: false, title: "", createElement: () => new Element(), getElementById: id => { assert.ok(elements.has(id), id); return elements.get(id); }};
  const config = {headset_host: "headset.example", headset_port: 45681, model_path: "D:/models/student.onnx", model_kind: "face50", device: "auto", stale_ms: 350, preview_fps: 10, smoothing_ms: 20,
    replay_path: "D:/data/reference.npz", mouth_mode: "curated", hybrid_channels: null, channel_sources: null, enabled: false, output_enabled: false, output_host: "127.0.0.1", output_port: 45682};
  const trained = ["JawOpen", "TongueOut", "TongueLeft"];
  const server = {config, calls: [], before: null, failStatus: false, failStart: false, previewStatus: 200, status: {
    running: false, connected: false, config, source: {session_id: "18446744073709551000", source_seq: "9007199254740993"},
    contract: {schema: "pico-mouth50-v1", channels}, model: {trained_channels: trained}, output_session_id: "test-output",
    prediction: {fresh: false, state: "stopped", valid_mask: 0, channels: {JawOpen: .3, TongueOut: .1, TongueLeft: .2}},
    cymple_relay: {running: false, state: "idle", source_host: "", left_port: 45682, right_port: 45683, cymple_host: "127.0.0.1", cymple_port: 23303, error: "",
      config: {cymple_host: "127.0.0.1", cymple_port: 23303, left_port: 45682, right_port: 45683, left_rotate: 0, left_hflip: false, left_vflip: false, right_rotate: 0, right_hflip: false, right_vflip: false},
      eyes: {eye_left: {channel: 0, port: 45682, state: "idle", connected: false, fps: 0, frames: 0, avg_kb: 0, error: null},
        eye_right: {channel: 1, port: 45683, state: "idle", connected: false, fps: 0, frames: 0, avg_kb: 0, error: null}}},
    metrics: {source_fresh: false, video_fresh: false}, recording: {state: "idle"}, training: {state: "idle", running: false, operations: {
      export: [{name: "checkpoint", required: true}, {name: "output", required: true}],
       "local-adapt": [{name: "dataset", required: true}, {name: "initial", required: false}, {name: "teacher_approval", required: false}]}}}};
  server.reference = {version: 1, neutral: "neutral.webp", model: {name: "Reference head", copyright: "Reference copyright"}, poses: [
    {id: "neutral", title: {en: "Relax", zh: "放松"}, instruction: {en: "Relax your mouth", zh: "自然放松嘴部"}, front: "neutral.webp", provenance: []},
    {id: "JawForward", title: {en: "Jaw forward", zh: "下颌前伸"}, instruction: {en: "Move your jaw forward", zh: "下颌轻轻向前伸"}, front: "JawForward.webp", profile: "JawForward-profile.webp", animation: "JawForward-motion.webp", provenance: ["original_model_morphs_with_original_correctives"]},
     {id: "LipFunnelUpper", title: {en: "Upper lip funnel", zh: "上唇漏斗"}, instruction: {en: "Funnel your upper lip", zh: "只让上唇做漏斗"}, front: "LipFunnelUpper.webp", animation: "LipFunnelUpper-motion.webp", provenance: ["original_model_morphs_with_original_correctives"]}]};
  server.presets = [
    {id:"complete",name:{en:"Complete",zh:"完整"},description:{en:"All fixture actions",zh:"全部测试动作"},steps:server.reference.poses.map(p=>({id:p.id,title:p.title}))},
    {id:"basics",name:{en:"Basics",zh:"基础"},description:{en:"Two ordered actions",zh:"两个有序动作"},steps:[server.reference.poses[1],server.reference.poses[0]].map(p=>({id:p.id,title:p.title}))}
  ];
  function active(mode) {
    Object.assign(server.status, {running: true, connected: mode !== "replay", source_mode: mode === "capture" ? "live" : mode, capture_only: mode === "capture"});
    config.enabled = true;
    const validBits = trained.reduce((mask, name) => {
      const idx = server.status.contract.channels.indexOf(name);
      return idx >= 0 ? (mask | (1n << BigInt(idx))) : mask;
    }, 0n);
    Object.assign(server.status.prediction, {fresh: mode !== "capture", state: mode === "capture" ? "untrained" : "valid", valid_mask: mode === "capture" ? 0 : validBits.toString()});
    Object.assign(server.status.metrics, {source_fresh: mode !== "capture", video_fresh: true});
  }
  const fetcher = async (path, opts = {}) => {
    server.calls.push({path, ...opts}); const custom = await server.before?.(path, opts); if (custom) return custom;
    if (path === "/api/discovery") return Response.json({devices: server.discovered ?? []});
    if (path === "/api/presets") return Response.json({presets:server.presets,default:"complete"});
    if (path === "/api/display" || path === "/api/recording/display") return Response.json({accepted:true});
    if (path === "/api/model/select") return Response.json(server.selectedModel ?? {cancelled: true});
    if (path === "/static/guide-reference/manifest.json") return server.reference ? Response.json(server.reference) : Response.json({error: "Not found"}, {status: 404});
    if (path === "/api/expressions") {
      if (server.failExpressions) return Response.json({error: "Not found"}, {status: 404});
      return Response.json({
        running: server.status.running, connected: server.status.connected,
        source_mode: server.status.source_mode, capture_only: server.status.capture_only,
        metrics: {source_fresh: server.status.metrics.source_fresh, video_fresh: server.status.metrics.video_fresh},
        prediction: server.status.prediction, live_stock: server.status.live_stock,
      });
    }
    if (path === "/api/status") { if (server.failStatus) throw TypeError("Failed to fetch"); return Response.json(server.status); }
    if (path === "/api/config") {
      if (opts.method === "POST") {
        const changes = JSON.parse(opts.body);
        if (server.status.running && ["headset_host", "model_path", "device", "output_port", "output_enabled"].some(key => changes[key] !== config[key])) Object.assign(server.status, {running: false, connected: false});
        Object.assign(config, changes);
      }
      return Response.json(config);
    }
    if (path === "/api/start") { if (server.failStart) return Response.json({error: "ONNX model does not exist: fixture"}, {status: 400}); const {mode} = JSON.parse(opts.body); active(mode); return Response.json(server.status); }
    if (path === "/api/stop") { Object.assign(server.status, {running: false, connected: false}); config.enabled = false; return Response.json(server.status); }
    if (path === "/api/cymple-relay") {
      const req = JSON.parse(opts.body);
      if (req.action === "start") {
        const {action, headset_host, ...cfg} = req;
        Object.assign(server.status.cymple_relay.config, cfg);
        Object.assign(server.status.cymple_relay, {
          running: true, state: "running", source_host: headset_host || "192.0.2.10",
          left_port: cfg.left_port ?? 45682, right_port: cfg.right_port ?? 45683,
          cymple_host: cfg.cymple_host ?? "127.0.0.1", cymple_port: cfg.cymple_port ?? 23303, error: ""
        });
        server.status.cymple_relay.eyes = {
          eye_left: {channel: 0, port: cfg.left_port ?? 45682, state: "streaming", connected: true, fps: 90.0, frames: 45, avg_kb: 28.4, error: null},
          eye_right: {channel: 1, port: cfg.right_port ?? 45683, state: "streaming", connected: true, fps: 89.8, frames: 44, avg_kb: 27.9, error: null}
        };
      } else if (req.action === "stop") {
        Object.assign(server.status.cymple_relay, {running: false, state: "idle", source_host: ""});
        server.status.cymple_relay.eyes.eye_left.state = "idle";
        server.status.cymple_relay.eyes.eye_right.state = "idle";
      }
      return Response.json(server.status.cymple_relay);
    }
    if (path === "/api/recording") {
      const body = JSON.parse(opts.body);
      if (body.action === "begin") { Object.assign(server.status.recording, {state: "recording", session_id: "local", path: "D:/local/recording", guide: {index: 0, phase: "prepare", progress: 0, preset_id:body.preset_id,total:server.presets.find(p=>p.id===body.preset_id)?.steps.length,
        step: {id: "neutral", kind: "negative", prepare: {en: "Relax your mouth", zh: "放松嘴部"}, hold: {en: "Hold", zh: "保持"}, confirm: {en: "Confirm", zh: "确认"}}}}); }
      else if (["ready", "finish", "confirm", "skip"].includes(body.action)) { server.status.recording.guide.phase = body.action === "ready" ? "hold" : "review"; }
      else { server.status.recording.state = body.action === "cancel" ? "cancelled" : "stopped"; Object.assign(server.status, {running: false, connected: false}); }
      return Response.json(server.status.recording);
    }
    if (path === "/api/training") { const {action} = JSON.parse(opts.body); Object.assign(server.status.training, {running: action !== "cancel", state: action === "cancel" ? "cancelled" : "training"}); return Response.json(server.status.training); }
    if (path.startsWith("/api/recording/review?")) return Response.json({session_id: "local", offset: 0, total: 2, samples: [
      {sample_id: "f000000000", prompt_candidate: {labels: {TongueOut: .5}}}, {sample_id: "f000000001", prompt_candidate: {labels: {TongueOut: .5}}}]});
    if (path === "/api/recording/review") return Response.json({samples: 2});
    if (path.startsWith("/api/preview.jpg")) return server.previewStatus === 204 ? new Response(null, {status: 204}) : new Response(new Uint8Array([255, 216, 255, 217]), {status: server.previewStatus});
    throw Error(path);
  };
  const timerCalls = [], timers = {setInterval: (fn, ms) => { timerCalls.push({fn, ms}); return timerCalls.length; }, setTimeout: (fn, ms) => { timerCalls.push({fn, ms}); return timerCalls.length; }, clearInterval() {}, clearTimeout() {}};
  const urls = {created: [], revoked: [], createObjectURL() { const url = `blob:${this.created.length}`; this.created.push(url); return url; }, revokeObjectURL(url) { this.revoked.push(url); }};
  const saved = new Map(), storage = {getItem: k => saved.get(k), setItem: (k, v) => saved.set(k, v)}; let time = 10000;
  const app = createApp(doc, {fetch: fetcher, timers, urlApi: urls, storage, languages: ["en-US"], now: () => time, ...options}); t.after(() => app.destroy());
  return {app, server, doc, el: id => doc.getElementById(id), row: name => app.rows.get(`outputs/${name}`), saved, urls, timers: timerCalls, advance: ms => time += ms};
}

function externalGuide(f) {
  f.server.status.read_only=true;
  f.server.status.external_guide={case:"external-fixture-A",role:"A",index:0,frame:"external-frame",running:true,phase:"imitate",
    steps:[{text:"Keep the current opening",visual:{demo_prompt_ids:["JawForward","LipFunnelUpper"],note:"Opening reference only"}}]};
}
test("capture progress is always visible and derives 0/31, 7/31 and 31/31 from the external preset",async t=>{
  const f=setup(t);externalGuide(f);
  const external=f.server.status.external_guide;
  external.steps=Array.from({length:31},(_,i)=>({...external.steps[0],action:`step-${i}`}));
  external.running=false;external.phase="practice";
  await f.app.init();
  assert.equal(f.el("capture-progress-count").textContent,"0 / 31");
  assert.equal(f.el("capture-progress").max,31);
  assert.equal(f.el("capture-progress").hidden,false);
  external.running=true;external.phase="imitate";external.index=6;await f.app.pollStatus();
  assert.equal(f.el("capture-progress-count").textContent,"7 / 31");
  assert.equal(f.el("capture-progress").value,7);
  assert.equal(f.el("capture-preset").disabled,true);
  f.app.selectPreset("basics");assert.equal(f.app.state.selectedPreset,external.case);
  external.running=false;external.phase="done";external.index=30;await f.app.pollStatus();
  assert.equal(f.el("capture-progress-count").textContent,"31 / 31");
  assert.equal(f.el("capture-progress-phase").textContent,messages.en.visualdone);
});
test("preset selection previews its exact action order and count without starting anything",async t=>{
  const f=setup(t);await f.app.init();const before=f.server.calls.length;
  f.app.selectPreset("basics");
  assert.equal(f.el("capture-progress-count").textContent,"0 / 2");
  assert.equal(f.el("capture-action-list").children[0].textContent,"Jaw forward");
  assert.equal(f.el("capture-action-list").children[1].textContent,"Relax");
  assert.equal(f.el("reference-image").src,"/static/guide-reference/JawForward.webp");
  assert.equal(f.server.calls.length,before);
  assert.equal(f.el("record-consent").checked,false);
  f.app.setLanguage("zh-CN");
  assert.equal(f.app.state.selectedPreset,"basics");
  assert.equal(f.el("capture-action-list").children[0].textContent,"下颌前伸");
  assert.equal(f.el("capture-progress-count").textContent,"0 / 2");
});
test("the server default can offer a saved continuation without granting consent or starting input",async t=>{
  const f=setup(t), tail={...f.server.presets[1],id:"continuation"};
  f.server.before = path => path==="/api/presets" ? Response.json({
    presets:[tail,...f.server.presets], default:tail.id, continuation:{person_id:"same-wearer",label_stage:"raw_only"}
  }) : null;
  await f.app.init();
  assert.equal(f.el("capture-preset").value,"continuation");
  assert.equal(f.el("person_id").value,"same-wearer");
  assert.equal(f.el("capture-progress-count").textContent,"0 / 2");
  assert.equal(f.el("record-consent").checked,false);
  assert.match(f.el("reference-image").src,/JawForward.webp$/);
  assert.ok(f.server.calls.every(call=>call.method==="GET"));
});
test("sealed JawOpen C requires a recording click, shows reference media and does not offer a restart after disconnect",async t=>{
  const f=setup(t,{paint:fn=>fn()});
  const step={id:"c_jaw_open_small_1",demo_id:"JawForward",title:{en:"Open slightly",zh:"小幅张嘴"}};
  const plan={...f.server.presets[1],id:"sealed-c-jaw",steps:[step]};
  f.server.presets=[plan];
  f.server.before=path=>path==="/api/presets" ? Response.json({presets:[plan],default:plan.id,sealed_c:true}) : null;
  Object.assign(f.server.status,{managed_capture:true,capture_control:{sealed_c:true,preset_id:plan.id,state:"idle"}});
  await f.app.init(); f.app.navigate("recording");
  assert.equal(f.el("capture-preset").value,plan.id);
  assert.equal(f.el("capture-preset").disabled,true);
  assert.equal(f.el("capture-step-nav").hidden,true);
  assert.equal(f.el("review").hidden,true);
  assert.equal(f.el("record-start").disabled,true);
  assert.equal(f.el("consent-label").textContent,messages.en.sealedConsent);
  assert.ok(f.server.calls.every(call=>call.method==="GET"));
  f.el("record-consent").checked=true; f.el("record-consent").emit("change");
  assert.ok(f.server.calls.every(call=>call.method==="GET"));
  await f.app.record("begin");
  const begin=JSON.parse(f.server.calls.find(c=>c.path==="/api/recording").body);
  assert.equal(begin.preset_id,plan.id);
  assert.equal(begin.confirmed,true);
  assert.equal(begin.role,undefined);
  assert.equal(begin.evaluation_only,undefined);
  f.server.status.capture_control.state="recording";
  Object.assign(f.server.status.recording.guide,{step,automatic:true,phase:"prepare",frame:"jaw-frame"});
  await f.app.pollStatus();
  assert.equal(f.el("reference-image").src,"/static/guide-reference/JawForward-motion.webp");
  for(const id of ["reference-image","reference-neutral-image","reference-profile-image"])
    Object.assign(f.el(id),{complete:true,naturalWidth:512});
  await f.app.reportVisual();
  assert.equal(JSON.parse(f.server.calls.at(-1).body).media_ready,true);
  f.server.status.capture_control.state="error";
  f.server.status.capture_control.error="Truncated frame payload (47/2190 bytes)";
  f.server.status.recording.state="stopped";
  await f.app.pollStatus();
  assert.equal(f.el("record-next").textContent,messages.en.sealedInterrupted);
  assert.equal(f.el("record-continue").hidden,true);
  assert.equal(f.el("record-start").disabled,true);
  assert.equal(f.el("capture").disabled,true);
  await f.app.record("begin");
  assert.equal(f.server.calls.filter(c=>c.path==="/api/recording").length,1);
});
test("recording submits the selected preset and locks it while the real backend guide advances",async t=>{
  const f=setup(t);await f.app.init();f.app.selectPreset("basics");
  await f.app.start("capture");f.el("record-consent").checked=true;f.el("record-consent").emit("change");await f.app.record("begin");
  const body=JSON.parse(f.server.calls.find(c=>c.path==="/api/recording").body);
  assert.equal(body.preset_id,"basics");
  assert.equal(f.el("capture-progress-count").textContent,"1 / 2");
  assert.equal(f.el("capture-preset").disabled,true);
  f.app.selectPreset("complete");assert.equal(f.app.state.selectedPreset,"basics");
  Object.assign(f.server.status.recording.guide,{index:2,phase:"finished"});
  await f.app.pollStatus();
  assert.equal(f.el("capture-progress-count").textContent,"2 / 2");
});
test("read-only preset previews do not replace the external plan or grant headset readiness",async t=>{
  const f=setup(t,{viewer:"headset",paint:fn=>fn()});externalGuide(f);
  f.server.status.external_guide.running=false;f.server.status.external_guide.phase="practice";
  await f.app.init();f.app.navigate("recording");f.app.selectPreset("basics");
  for(const id of ["reference-image","reference-neutral-image","reference-profile-image"])
    Object.assign(f.el(id),{complete:true,naturalWidth:512});
  await f.app.reportVisual();
  assert.equal(JSON.parse(f.server.calls.at(-1).body).media_ready,false);
  assert.equal(f.el("capture-progress-count").textContent,"0 / 2");
  assert.equal(f.server.status.external_guide.steps.length,1);
  assert.ok(f.el("capture-preset-description").textContent.includes(messages.en.capturePreviewOnly));
  assert.ok(f.server.calls.filter(c=>c.method==="POST").every(c=>c.path==="/api/display"));
});
test("progress never marks a paused external hold as a finished plan",()=>{
  const preset={id:"round",steps:Array(5).fill({})};
  assert.deepEqual(captureProgress({external_guide:{case:"round",steps:preset.steps,index:2,phase:"paused",running:false}},preset),
    {total:5,current:3,active:false,phaseKey:"visualpaused"});
});
test("unguided recording is distinct from a preset that has not started",async t=>{
  const f=setup(t);await f.app.init();
  Object.assign(f.server.status.recording,{state:"recording",guide:null});
  await f.app.pollStatus();
  assert.equal(f.el("capture-progress-count").textContent,"—");
  assert.equal(f.el("capture-progress-phase").textContent,messages.en.captureFree);
  assert.equal(f.el("capture-preset").disabled,true);
  assert.equal(f.el("capture-action-list").children.length,3);
});
test("external 31-step state uses the original reference renderer without a second page or local recording",async t=>{
  const f=setup(t); externalGuide(f); await f.app.init(); f.app.navigate("recording");
  assert.equal(f.el("reference-image").src,"/static/guide-reference/JawForward-motion.webp");
  assert.equal(f.el("reference-profile-image").src,"/static/guide-reference/JawForward-profile.webp");
  assert.equal(f.el("reference-neutral-image").src,"/static/guide-reference/LipFunnelUpper.webp");
  assert.equal(f.el("reference-neutral-caption").textContent,messages.en.referenceOpening);
  assert.match(f.el("reference-instruction").textContent,/Keep the current opening/);
  assert.equal(f.el("reference-select").disabled,true);
  assert.equal(f.server.status.recording.state,"idle");
  assert.ok(f.server.calls.every(c=>c.method==="GET"));
});
test("external hold freezes the existing image and read-only mode blocks all capture and training controls",async t=>{
  const f=setup(t); externalGuide(f); await f.app.init();
  f.server.status.external_guide.phase="hold"; await f.app.pollStatus();
  assert.equal(f.el("reference-image").src,"/static/guide-reference/JawForward.webp");
  for(const id of ["start","capture","save","record-start","train-start","model-browse","output_enabled"])
    assert.equal(f.el(id).disabled,true,id);
  await f.app.start(); await f.app.record("begin"); await f.app.train();
  assert.ok(f.server.calls.every(c=>c.method==="GET"));
});
test("original reference images must load and paint before a headset display receipt can be ready",async t=>{
  let paints=0;
  const f=setup(t,{viewer:"headset",paint:fn=>{paints++;fn();}}); externalGuide(f);
  await f.app.init(); f.app.navigate("recording"); await f.app.reportVisual();
  assert.equal(JSON.parse(f.server.calls.at(-1).body).media_ready,false);
  for(const id of ["reference-image","reference-neutral-image","reference-profile-image"])
    Object.assign(f.el(id),{complete:true,naturalWidth:512});
  await f.app.reportVisual();
  const receipt=JSON.parse(f.server.calls.at(-1).body);
  assert.equal(receipt.frame,"external-frame"); assert.equal(receipt.viewer,"headset");
  assert.equal(receipt.visible,true); assert.equal(receipt.media_ready,true); assert.equal(paints,2);
  f.el("reference-image").getBoundingClientRect=()=>({height:512,top:-600,bottom:-88});
  await f.app.reportVisual(); assert.equal(JSON.parse(f.server.calls.at(-1).body).media_ready,false);
  f.doc.hidden=true; await f.app.reportVisual();
  assert.equal(JSON.parse(f.server.calls.at(-1).body).visible,false);
});
test("leaving the original capture page or expiring status cannot grant visual readiness",async t=>{
  const f=setup(t,{viewer:"headset",paint:fn=>fn()}); externalGuide(f); await f.app.init();
  await f.app.reportVisual();
  assert.equal(JSON.parse(f.server.calls.at(-1).body).visible,false);
  const calls=f.server.calls.length; f.advance(1600); await f.app.reportVisual();
  assert.equal(f.server.calls.length,calls);
});

test("task pages keep advanced tools folded and preserve every existing capability", () => {
  for (const opening of html.matchAll(/<details\b[^>]*>/g)) assert.doesNotMatch(opening[0], /\bopen\b/);
  for (const id of ["advanced", "diagnostics", "recording", "training", "capture", "model_path", "start", "stop", "preview"]) assert.ok(html.includes(`id="${id}"`));
  assert.doesNotMatch(html, /segmentation|size_hysteresis|calibrate|face_source|upload|type="file"/);
});
test("both languages are complete and browser defaults respect the first preference", () => {
  assert.deepEqual(Object.keys(messages.en).sort(), Object.keys(messages["zh-CN"]).sort());
  for (const text of Object.values(messages.en)) assert.ok(!/[\p{Script=Han}]/u.test(text), text);
  assert.equal(preferredLanguage(["en-US", "zh-CN"]), "en"); assert.equal(preferredLanguage(["zh-Hans-CN"]), "zh-CN");
  assert.equal(initialLanguage({getItem: () => "en"}, ["zh-CN"]), "en");
});
test("opening and translating never starts input, recording, training or speech", async t => {
  const speech = {calls: [], cancel() {}, speak(x) { this.calls.push(x); }};
  const {app, server, el, doc} = setup(t, {speech, Utterance: class {}}); await app.init(); app.setLanguage("zh-CN");
  assert.ok(server.calls.every(c => c.method === "GET")); assert.equal(speech.calls.length, 0); assert.equal(server.config.output_enabled, false);
  assert.equal(doc.title, messages["zh-CN"].title); assert.equal(el("record-consent").checked, false);
});
test("language change preserves unsaved values, consent, fields and expanded state", async t => {
  const {app, server, el, saved} = setup(t); await app.init(); el("model_path").value = "D:/unsaved.onnx"; el("advanced").open = true; el("record-consent").checked = true;
  app.trainInputs.get("checkpoint").input.value = "D:/candidate.pt"; el("settings").emit("input"); const n = server.calls.length; app.setLanguage("zh-CN");
  assert.equal(server.calls.length, n); assert.equal(el("model_path").value, "D:/unsaved.onnx"); assert.equal(el("advanced").open, true);
  assert.equal(el("record-consent").checked, true); assert.equal(app.trainInputs.get("checkpoint").input.value, "D:/candidate.pt"); assert.equal(saved.get(LANGUAGE_KEY), "zh-CN");
});
test("Start is strict inference, preserves UDP off and outputs direct named controls", async t => {
  const {app, server, row} = setup(t); await app.init(); await app.start();
  const config = JSON.parse(server.calls.find(c => c.path === "/api/config" && c.method === "POST").body);
  assert.equal(config.model_kind, "face50"); assert.equal(config.output_enabled, false); assert.ok(!("face_source" in config));
  assert.deepEqual(JSON.parse(server.calls.find(c => c.path === "/api/start").body), {mode: "live"});
  assert.equal(row("TongueOut").output.textContent, "0.100"); assert.equal(app.rows.get("channel-table/TongueLeft").output.textContent, "0.200"); assert.equal(app.rows.get("channel-table/TongueUp").output.textContent, "—");
  assert.equal(app.rows.get("channel-table/TongueUp").bar.hidden, true);
});
test("untrained heads cannot be displayed as valid even with nonzero values and mask", async t => {
  const {app, server, row} = setup(t); await app.init(); await app.start();
  server.status.prediction.channels.TongueUp = .95; server.status.prediction.valid_mask = (1n << 50n).toString(); await app.pollStatus();
  assert.equal(app.rows.get("channel-table/TongueUp").output.textContent, "—"); assert.equal(row("TongueOut").output.textContent, "—");
  server.status.prediction.valid_mask = ((1n << 50n)-1n).toString(); await app.pollStatus();
  assert.equal(app.rows.get("channel-table/TongueUp").output.textContent, "—"); assert.equal(row("TongueOut").output.textContent, "0.100");
});
test("channel helper requires trained name, valid bit and finite value", () => {
  const status = {contract: {channels: ["TongueOut"]}, model: {trained_channels: ["TongueOut"]}, prediction: {valid_mask: "1", channels: {TongueOut: 0}}};
  assert.equal(channelValue(status, "TongueOut", true), 0); assert.equal(channelValue(status, "TongueOut", false), null);
  status.prediction.channels.TongueOut = NaN; assert.equal(channelValue(status, "TongueOut", true), null);
});
test("valid_mask bits 32, 43, 44 and 49 never wrap into uint32; unknown high heads stay grey", async t => {
  const {app, server, row} = setup(t); await app.init(); await app.start();
  const names = Array.from({length: 50}, (_, i) => `head${i}`), heads = [[32, "TongueOut"], [43, "TongueRoll"], [44, "MouthUpperLeft"], [49, "LipFunnelLower"]];
  names[0] = "JawOpen"; for (const [index, name] of heads) names[index] = name;
  server.status.contract.channels = names; server.status.model.trained_channels = ["JawOpen", ...heads.map(([, name]) => name)];
  server.status.prediction.channels = {JawOpen: .7, TongueOut: 0, TongueRoll: .43, MouthUpperLeft: .44, LipFunnelLower: .49};
  for (const [index, name] of heads) {
    server.status.prediction.valid_mask = Number(1n << BigInt(index)); await app.pollStatus();
    assert.equal(app.rows.get(`channel-table/${name}`).output.textContent, server.status.prediction.channels[name].toFixed(3));
    assert.equal(row("JawOpen").output.textContent, "—", `high bit ${index} must not wrap to index0`);
    for (const [otherIndex, otherName] of heads) if (otherIndex !== index) assert.equal(app.rows.get(`channel-table/${otherName}`).output.textContent, "—");
  }
  server.status.prediction.valid_mask = heads.reduce((mask, [i]) => mask | (1n << BigInt(i)), 0n).toString();
  server.status.model.trained_channels = ["TongueOut"]; await app.pollStatus();
  assert.equal(row("TongueOut").output.textContent, "0.000");
  for (const [, name] of heads.slice(1)) { const item = app.rows.get(`channel-table/${name}`); assert.equal(item.output.textContent, "—"); assert.equal(item.bar.hidden, true); }
});
test("capture-only can start without any model and preview does not require valid channels", async t => {
  const {app, server, el, row} = setup(t); server.config.model_path = ""; await app.init(); await app.start("capture"); await app.refreshPreview();
  assert.deepEqual(JSON.parse(server.calls.find(c => c.path === "/api/start").body), {mode: "capture"});
  assert.equal(el("preview").hidden, false); assert.equal(row("TongueOut").output.textContent, "—"); assert.equal(el("status").textContent, messages.en.recordingOnly);
});
test("stock diagnostics distinguish no labels, unassociated labels and exact associations in both languages", async t => {
  const {app, server, el} = setup(t); await app.init();
  for (const language of ["en", "zh-CN"]) {
    app.setLanguage(language);
    for (const [records, associated, unassociated, key] of [[0, 0, 0, "associationWaiting"], [3, 0, 3, "associationUnverified"], [4, 1, 3, "associationExact"]]) {
      server.status.stock = {active: records > 0, records, associated, unassociated, error: null};
      await app.pollStatus();
      assert.ok(el("stock-info").textContent.startsWith(messages[language][key]));
      assert.ok(el("stock-info").textContent.includes(`"associated":${associated}`));
      assert.ok(el("stock-info").textContent.includes(`"unassociated":${unassociated}`));
    }
  }
});
test("missing inference model does not POST and errors translate", async t => {
  const {app, server, el} = setup(t); server.config.model_path = ""; await app.init(); await app.start();
  assert.ok(server.calls.every(c => c.method === "GET")); assert.equal(el("error").textContent, messages.en.required); app.setLanguage("zh-CN"); assert.equal(el("error").textContent, messages["zh-CN"].required);
});
test("Save can persist blank model without starting capture or output", async t => {
  const {app, server, el} = setup(t); await app.init(); el("model_path").value = ""; el("settings").emit("input"); await app.save();
  assert.equal(server.config.model_path, ""); assert.ok(!server.calls.some(c => c.path === "/api/start"));
});
test("record requires separate consent and a person ID, not an existing model", async t => {
  const {app, server, el} = setup(t); server.config.model_path = ""; await app.init(); await app.record("begin");
  assert.ok(!server.calls.some(c => c.path === "/api/recording")); await app.start("capture"); el("person_id").value = "local-person"; el("record-consent").checked = true; el("person_id").emit("change"); await app.record("begin");
  const body = JSON.parse(server.calls.find(c => c.path === "/api/recording").body);
  assert.deepEqual(body, {action: "begin", person_id: "local-person", confirmed: true, guide: true, stage: "raw_only",receive_results:true,preset_id:"complete",automatic:true,prepare_seconds:7,step_index:0});
  assert.equal(el("record-consent").checked, false); assert.equal(el("guide").hidden, false); assert.equal(el("guide-step").textContent, "Relax your mouth");
});
test("guide phase controls use real ready/finish/confirm/skip API, never auto-approve labels", async t => {
  const {app, el, server} = setup(t); await app.init(); await app.start("capture"); el("person_id").value = "local"; el("record-consent").checked = true; el("person_id").emit("change"); await app.record("begin");
  assert.equal(el("guide-ready").disabled, false); assert.equal(el("guide-confirm").disabled, true); await app.record("ready");
  assert.equal(el("guide-step").textContent, "Hold"); assert.ok(!server.calls.some(c => c.path.includes("review")));
});
test("One-Euro filter parameters are configurable, bilingual and toggling off disables inputs without starting tracking", async t => {
  const {app, server, el} = setup(t); await app.init();
  assert.equal(el("filter_enabled").checked, true);
  assert.equal(el("filter_min_cutoff").value, "7.96");
  assert.equal(el("filter_beta").value, "4");
  assert.equal(el("filter_d_cutoff").value, "1");
  el("filter_min_cutoff").value = "2.5"; el("filter_beta").value = "6.5"; el("filter_d_cutoff").value = "1.5";
  el("filter_enabled").checked = false; el("settings").emit("input"); app.setLanguage("zh-CN");
  assert.equal(el("filter-min-cutoff-label").textContent, messages["zh-CN"].filterMinCutoff);
  assert.equal(el("filter-beta-label").textContent, messages["zh-CN"].filterBeta);
  assert.equal(el("filter-d-cutoff-label").textContent, messages["zh-CN"].filterDCutoff);
  assert.equal(el("filter_min_cutoff").disabled, true);
  assert.equal(el("filter_beta").disabled, true);
  assert.equal(el("filter_d_cutoff").disabled, true);
  await app.save();
  const posted = JSON.parse(server.calls.find(c => c.path === "/api/config" && c.method === "POST").body);
  assert.equal(posted.filter_enabled, false);
  assert.equal(posted.filter_min_cutoff, 2.5);
  assert.equal(posted.filter_beta, 6.5);
  assert.equal(posted.filter_d_cutoff, 1.5);
  assert.equal(posted.smoothing_ms, 0);
  assert.ok(!server.calls.some(c => c.path === "/api/start"));
  assert.equal(posted.mouth_mode, "curated"); assert.equal(posted.hybrid_channels, null); assert.equal(posted.channel_sources, null); assert.ok(!("face_source" in posted));
});
test("image-only recording sends an explicit result opt-out and locks it after start", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start("capture");
  el("record-consent").checked = true; el("record-consent").emit("change");
  el("receive-results").checked = false;
  await app.record("begin");
  assert.equal(JSON.parse(server.calls.find(c=>c.path==="/api/recording").body).receive_results,false);
  assert.equal(el("receive-results").disabled,true);
});
test("managed capture starts from one recording click without a separate camera or agent step",async t=>{
  const f=setup(t);f.server.status.managed_capture=true;f.server.status.capture_control={state:"idle",error:""};
  f.server.config.headset_host="";f.server.config.model_path="";
  await f.app.init();f.app.navigate("recording");
  assert.equal(f.el("record-start").disabled,true);
  f.el("record-consent").checked=true;f.el("record-consent").emit("change");
  assert.equal(f.el("record-start").disabled,false);
  await f.app.record("begin");
  const request=f.server.calls.find(c=>c.path==="/api/recording");
  assert.equal(JSON.parse(request.body).automatic,true);
  assert.ok(!f.server.calls.some(c=>c.path==="/api/start"));
  assert.equal(f.el("connection-setup").hidden,false);
  assert.equal(f.el("capture").hidden,false);
});
test("managed recording saves a discovered headset draft before beginning without a separate save",async t=>{
  const f=setup(t);f.server.status.managed_capture=true;f.server.status.capture_control={state:"idle",error:""};
  f.server.config.headset_host="";f.server.config.model_path="";
  f.server.discovered=[{name:"PICO",host:"192.0.2.10",port:45681}];
  await f.app.init();f.app.navigate("recording");
  assert.equal(f.app.state.dirty,true);
  assert.equal(f.server.config.headset_host,"");
  f.el("record-consent").checked=true;f.el("record-consent").emit("change");
  assert.equal(f.el("record-start").disabled,false);
  await f.app.record("begin");
  const posts=f.server.calls.filter(c=>c.method==="POST");
  assert.deepEqual(posts.map(c=>c.path),["/api/config","/api/recording"]);
  assert.equal(JSON.parse(posts[0].body).headset_host,"192.0.2.10");
  assert.equal(f.app.state.dirty,false);
  assert.equal(f.server.config.model_path,"");
});
test("managed recording keeps multiple headsets selectable and saves the user's choice",async t=>{
  const f=setup(t);f.server.status.managed_capture=true;f.server.status.capture_control={state:"idle"};
  f.server.config.headset_host="";
  f.server.discovered=[{name:"First",host:"192.0.2.10",port:45681},{name:"Second",host:"192.0.2.11",port:45681}];
  await f.app.init();f.app.navigate("recording");
  assert.equal(f.el("headset-select").disabled,false);
  f.el("headset-select").value="192.0.2.11:45681";f.el("headset-select").emit("change");
  f.el("record-consent").checked=true;f.el("record-consent").emit("change");
  await f.app.record("begin");
  assert.equal(f.server.config.headset_host,"192.0.2.11");
});
test("managed capture records existing preview or live tracking but does not treat replay as a live source",async t=>{
  const f=setup(t);f.server.status.managed_capture=true;f.server.status.capture_control={state:"idle"};
  await f.app.init();await f.app.start("capture");f.app.navigate("recording");
  f.el("record-consent").checked=true;f.el("record-consent").emit("change");
  assert.equal(f.el("record-start").disabled,false);
  Object.assign(f.server.status,{capture_only:false});await f.app.pollStatus();
  assert.equal(f.el("record-start").disabled,false);
  Object.assign(f.server.status,{capture_only:true,source_mode:"replay"});await f.app.pollStatus();
  assert.equal(f.el("record-start").disabled,true);
  assert.equal(f.el("record-next").textContent,messages.en.recordCloseInput);
});
test("automatic capture shows countdown and animated preparation, then a still hold with real paint receipts",async t=>{
  let paints=0;
  const f=setup(t,{paint:fn=>{paints++;fn();}});
  f.server.status.recording={state:"recording",session_id:"self-operated",guide:{
    automatic:true,paused:false,index:0,total:3,preset_id:"complete",phase:"prepare",frame:"prepare-frame",
    display_ready:true,remaining_seconds:2.2,step:{id:"JawForward",kind:"pose",title:{en:"Jaw forward",zh:"下颌前伸"}}}};
  await f.app.init();f.app.navigate("recording");
  assert.equal(f.el("guide-countdown").textContent,"3 s");
  assert.equal(f.el("guide-ready").hidden,true);
  assert.equal(f.el("reference-image").src,"/static/guide-reference/JawForward-motion.webp");
  await f.app.reportVisual();
  assert.equal(JSON.parse(f.server.calls.at(-1).body).media_ready,false);
  for(const id of ["reference-image","reference-neutral-image","reference-profile-image"])
    Object.assign(f.el(id),{complete:true,naturalWidth:512});
  await f.app.reportVisual();
  assert.equal(f.server.calls.at(-1).path,"/api/recording/display");
  assert.deepEqual(JSON.parse(f.server.calls.at(-1).body),{
    session_id:"self-operated",frame:"prepare-frame",phase:"prepare",visible:true,media_ready:true});
  assert.equal(paints,2);
  Object.assign(f.server.status.recording.guide,{phase:"hold",frame:"hold-frame"});
  await f.app.pollStatus();
  assert.equal(f.el("reference-image").src,"/static/guide-reference/JawForward.webp");
  f.app.navigate("preferences");await f.app.reportVisual();
  assert.equal(JSON.parse(f.server.calls.at(-1).body).visible,false);
});
test("automatic pause resume and skip are controlled entirely in the capture UI",async t=>{
  const f=setup(t);
  const guide={automatic:true,paused:false,index:0,total:3,preset_id:"complete",phase:"hold",frame:"fixture",
    remaining_seconds:2,display_ready:true,step:{id:"neutral",kind:"negative",title:{en:"Relax",zh:"放松"}}};
  f.server.status.recording={state:"recording",session_id:"fixture",guide};
  f.server.before=(path,opts)=>{
    if(path!=="/api/recording")return null;
    const {action}=JSON.parse(opts.body);
    if(["pause","resume"].includes(action)){guide.paused=action==="pause";guide.phase="prepare";return Response.json({state:"recording"});}
    return null;
  };
  await f.app.init();f.app.navigate("recording");
  await f.el("guide-pause").emit("click");
  assert.equal(f.el("guide-pause").textContent,messages.en.guideResume);
  assert.equal(f.el("guide-countdown").hidden,true);
  await f.el("guide-pause").emit("click");
  assert.equal(f.el("guide-pause").textContent,messages.en.guidePause);
  await f.app.record("skip");
  assert.deepEqual(f.server.calls.filter(c=>c.path==="/api/recording").map(c=>JSON.parse(c.body).action),["pause","resume","skip"]);
  assert.ok(!f.server.calls.some(c=>c.path.includes("/review")));
});
test("a desktop monitor cannot send automatic receipts for a headset-owned recording",async t=>{
  const f=setup(t,{viewer:"desktop"});
  f.server.status.capture_control={state:"recording",viewer:"headset"};
  f.server.status.recording={state:"recording",session_id:"fixture",guide:{automatic:true,phase:"prepare",
    frame:"fixture",step:{id:"JawForward"}}};
  await f.app.init();f.app.navigate("recording");await f.app.reportVisual();
  assert.ok(!f.server.calls.some(c=>c.path==="/api/recording/display"));
});
test("automatic text steps acknowledge visible painted instructions without loading substitute images",async t=>{
  let paints=0;
  const f=setup(t,{viewer:"headset",paint:fn=>{paints++;fn();}});
  f.server.reference.poses.push({id:"occluded",title:{en:"Cover the mouth",zh:"遮挡嘴部"}});
  f.server.status.capture_control={state:"recording",viewer:"headset"};
  f.server.status.recording={state:"recording",session_id:"fixture",guide:{automatic:true,phase:"prepare",
    frame:"text",step:{id:"occluded",kind:"occlusion",title:{en:"Cover your mouth briefly",zh:"短暂遮挡嘴部"}}}};
  await f.app.init();f.app.navigate("recording");await f.app.reportVisual();
  assert.equal(f.el("reference-views").hidden,true);
  assert.equal(f.el("reference-image").src,"");
  assert.equal(f.el("reference-message").textContent,"Cover your mouth briefly");
  const call=f.server.calls.find(c=>c.path==="/api/recording/display");
  assert.equal(JSON.parse(call.body).media_ready,true);
  assert.equal(JSON.parse(call.body).viewer,"headset");
  assert.equal(paints,2);
});
test("managed startup locks the chosen plan and recording options before frames arrive",async t=>{
  const f=setup(t);f.server.status.managed_capture=true;f.server.status.capture_control={state:"starting"};
  await f.app.init();
  for(const id of ["capture-preset","person_id","record-consent","guide-enabled","label-stage",
    "headset-select","discover","capture","headset_host","save","start","replay"])
    assert.equal(f.el(id).disabled,true,id);
  assert.equal(f.el("record-state").textContent,messages.en.captureStarting);
});
test("optional speech speaks each guide phase once and language switch needs no request", async t => {
  const speech = {calls: [], cancel() {}, speak(x) { this.calls.push(x); }};
  const {app, el, server} = setup(t, {speech, Utterance: class { constructor(text) { this.text = text; } }}); await app.init();
  await app.start("capture");
  el("person_id").value = "local"; el("record-consent").checked = true; el("voice-enabled").checked = true; el("person_id").emit("change"); await app.record("begin");
  assert.equal(speech.calls.length, 1); await app.pollStatus(); assert.equal(speech.calls.length, 1); const n = server.calls.length; app.setLanguage("zh-CN");
  assert.equal(server.calls.length, n); assert.equal(speech.calls.length, 2); assert.equal(speech.calls[1].lang, "zh-CN");
});
test("record cancellation clears old output and preview and keeps archives indicated", async t => {
  const {app, el, row} = setup(t); await app.init(); await app.start(); el("person_id").value = "local"; el("record-consent").checked = true; el("person_id").emit("change");
  await app.record("begin"); await app.refreshPreview(); await app.record("cancel");
  assert.equal(el("preview").hidden, true); assert.equal(app.rows.get("channel-table/TongueLeft").output.textContent, "—"); assert.equal(el("record-path").textContent, "D:/local/recording");
});
test("local training form is supplied by backend CLI spec and uses real operation/args", async t => {
  const {app, el, server} = setup(t); await app.init(); app.trainInputs.get("checkpoint").input.value = "student.pt"; app.trainInputs.get("output").input.value = "candidate.onnx"; await app.train();
  const body = JSON.parse(server.calls.find(c => c.path === "/api/training").body);
  assert.deepEqual(body, {action: "begin", operation: "export", args: {checkpoint: "student.pt", output: "candidate.onnx"}});
  assert.equal(el("train-cancel").disabled, false); await app.train("cancel"); assert.equal(el("train-state").textContent, messages.en.cancelled);
});
test("training choice values show localized labels and follow the language", async t => {
  const {app, server} = setup(t); server.status.training.operations = {"teacher-train": [{name: "tune", required: false, choices: ["frozen", "last-two"], default: "frozen"}]};
  await app.init(); const options = () => app.trainInputs.get("tune").input.children;
  assert.deepEqual(options().map(o => o.value), ["frozen", "last-two"]); assert.equal(options()[1].textContent, messages.en["choice-last-two"]);
  app.setLanguage("zh-CN"); assert.equal(options()[0].textContent, messages["zh-CN"]["choice-frozen"]); assert.deepEqual(options().map(o => o.value), ["frozen", "last-two"]);
});
test("DINO form labels do not name a single DINO version", () => {
  for (const language of ["en", "zh-CN"]) for (const key of ["dino_repo", "backbone_checkpoint"]) assert.doesNotMatch(messages[language][key], /v2|V2|DINOv/);
});
test("missing training integration is explicitly disabled, never an empty success", async t => {
  const {app, server, el} = setup(t); server.status.training.operations = {}; await app.init(); await app.train();
  assert.equal(el("train-start").disabled, true); assert.equal(el("train-state").textContent, messages.en.trainUnavailable); assert.ok(!server.calls.some(c => c.path === "/api/training"));
});
test("Stop releases object URLs and late previews cannot resurrect an image", async t => {
  const {app, server, el, row, urls} = setup(t); await app.init(); await app.start(); await app.refreshPreview(); let resolve;
  server.before = path => path.startsWith("/api/preview.jpg") ? new Promise(done => { resolve = done; }) : null;
  const pending = app.refreshPreview(); await app.stop(); resolve(new Response(new Uint8Array([1]))); await pending;
  assert.equal(el("preview").hidden, true); assert.equal(row("TongueOut").output.textContent, "—"); assert.deepEqual(urls.revoked, urls.created);
});
test("new output epoch invalidates pending preview", async t => {
  const {app, server, el} = setup(t); await app.init(); await app.start(); let resolve;
  server.before = path => path.startsWith("/api/preview.jpg") ? new Promise(done => { resolve = done; }) : null;
  const pending = app.refreshPreview(); server.status.output_session_id = "new"; await app.pollStatus(); resolve(new Response(new Uint8Array([1]))); await pending;
  assert.equal(el("preview").hidden, true);
});
test("offline and expired status clear outputs and do not restart on recovery", async t => {
  const {app, server, row, advance} = setup(t); await app.init(); await app.start(); server.failStatus = true; await app.pollStatus(); assert.equal(row("TongueOut").output.textContent, "—");
  server.failStatus = false; await app.pollStatus(); advance(1600); app.setLanguage("en"); assert.equal(row("TongueOut").output.textContent, "—");
  assert.equal(server.calls.filter(c => c.path === "/api/start").length, 1); assert.equal(deriveView(server.status, true, 1500).valid, false);
});
test("save backend reconfiguration clears actual run and does not auto-restart", async t => {
  const {app, server, el, row} = setup(t); await app.init(); await app.start(); el("output_port").value = "45692"; el("settings").emit("input"); await app.save();
  assert.equal(server.status.running, false); assert.equal(row("TongueOut").output.textContent, "—"); assert.equal(server.calls.filter(c => c.path === "/api/start").length, 1);
});
test("advanced replay works without headset and preserves output preference", async t => {
  const {app, server, el} = setup(t); server.config.headset_host = ""; server.config.output_enabled = true; await app.init(); await app.start("replay");
  assert.equal(server.config.output_enabled, true); assert.deepEqual(JSON.parse(server.calls.find(c => c.path === "/api/start").body), {mode: "replay"}); assert.equal(el("status").textContent, messages.en.replaying);
});
test("diagnostics preserve uint64 strings and actual backend fallback", async t => {
  const {app, server, el} = setup(t); Object.assign(server.status.model, {providers: ["DmlExecutionProvider"], fallbacks: [{engine: "onnxgraph", device: "cuda", error: "not available"}]}); await app.init();
  assert.equal(el("source-info").textContent, "18446744073709551000 / 9007199254740993"); assert.equal(el("provider-info").textContent, "DmlExecutionProvider"); assert.match(el("fallback-info").textContent, /cuda: not available/);
});
test("preview toggle is independent, one request at a time and hidden tabs do not fetch", async t => {
  const {app, el, server, doc, row} = setup(t); await app.init(); await app.start(); await app.refreshPreview(); const n = server.calls.length;
  el("preview-enabled").checked = false; el("preview-enabled").emit("change"); await app.refreshPreview(); assert.equal(server.calls.length, n); assert.equal(row("TongueOut").output.textContent, "0.100"); assert.equal(app.state.dirty, false);
  doc.hidden = true; el("preview-enabled").checked = true; await app.refreshPreview(); assert.equal(server.calls.length, n);
});
test("raw HTTP errors stay available with bilingual known-error explanations", async () => {
  const raw = "POST /api/start · HTTP 400: trained_channels missing"; assert.ok(errorMessage("zh-CN", raw).includes(messages["zh-CN"].manifestInvalid));
  assert.equal(errorMessage("en", "POST /api/model/select · HTTP 400: The adjacent JSON model manifest was not found"), messages.en.modelMissing);
  assert.equal(errorMessage("zh-CN", "Waiting for next PTJ1 frame"), messages["zh-CN"].cameraStreamWaiting);
  assert.equal(errorMessage("en", "Waiting for next PTJ1 frame"), messages.en.cameraStreamWaiting);
  await assert.rejects(requestJson(async () => Response.json({error: "model missing"}, {status: 422}), "/api/start", {}), /POST \/api\/start.*HTTP 422.*model missing/);
  await assert.rejects(requestJson(async () => new Response("not json"), "/api/status"), /HTTP 200.*not json/);
});
test("record consent never auto-starts an input connection", async t => {
  const {app, el, server} = setup(t); await app.init(); el("person_id").value = "local"; el("record-consent").checked = true; el("person_id").emit("change"); await app.record("begin");
  assert.equal(el("record-start").disabled, true); assert.ok(server.calls.every(c => c.method === "GET"));
});
test("review loads local thumbnails, separates proposals from labels and supports explicit batch approval", async t => {
  const {app, server, el} = setup(t); server.status.recording = {state: "stopped", path: "D:/local", session_id: "local"}; await app.init();
  el("review-targets").value = "{}"; el("reviewer").value = "local-user"; el("observation").value = "visible";
  await app.loadReviews(); assert.equal(el("review-grid").children.length, 2);
  const first = el("review-grid").children[0]; assert.match(first.children[1].src, /^\/api\/recording\/image\?session=local&sample=f/);
  first.children[1].emit("click"); assert.equal(el("review-targets").value, "{}");
  el("review-proposal").emit("click"); assert.deepEqual(JSON.parse(el("review-targets").value), {TongueOut: .5});
  el("review-select-page").emit("click"); await app.applyReview(); assert.ok(!server.calls.some(c => c.path === "/api/recording/review"));
  el("review-confirmed").checked = true; el("review-confirmed").emit("change"); await app.applyReview();
  const body = JSON.parse(server.calls.find(c => c.path === "/api/recording/review").body);
  assert.deepEqual(body.sample_ids, ["f000000000", "f000000001"]); assert.deepEqual(body.targets, {TongueOut: .5}); assert.equal(body.confirmed, true); assert.equal(el("review-confirmed").checked, false);
});
test("changing recording session clears review thumbnails and old selections", async t => {
  const {app, server, el} = setup(t); server.status.recording = {state: "stopped", path: "D:/local", session_id: "local"}; await app.init(); await app.loadReviews();
  el("review-select-page").emit("click"); server.status.recording.session_id = "new-session"; await app.pollStatus();
  assert.equal(app.state.reviewSelected.size, 0); assert.equal(el("review-grid").children.length, 0); assert.equal(el("review-image").hidden, true);
});

test("reference images are available for practice without a headset, model, consent or recording", async t => {
  const {app, server, el} = setup(t); server.config.headset_host = ""; server.config.model_path = ""; await app.init();
  assert.equal(el("reference-views").hidden, false); assert.equal(el("reference-select").disabled, false);
  assert.equal(el("reference-image").src, "/static/guide-reference/neutral.webp");
  assert.equal(el("reference-mode").textContent, messages.en.referencePractice);
  assert.ok(server.calls.every(call => call.method === "GET")); assert.equal(server.status.running, false);
});
test("practice selection changes images and optional side view without sending controls or targets", async t => {
  const {app, server, el} = setup(t); await app.init(); const count = server.calls.length;
  app.selectReference("JawForward"); assert.equal(el("reference-image").src, "/static/guide-reference/JawForward.webp");
  assert.equal(el("reference-profile").hidden, false); assert.match(el("reference-profile-image").src, /JawForward-profile/);
  assert.equal(el("reference-origin").textContent, messages.en.referenceOriginal);
  assert.equal(server.calls.length, count); assert.equal(el("record-consent").checked, false);
  app.selectReference("LipFunnelUpper"); assert.equal(el("reference-profile").hidden, true);
  assert.equal(el("reference-profile-image").src, ""); assert.equal(el("reference-origin").textContent, messages.en.referenceOriginal);
});
test("practice previous/next respects boundaries and stays local", async t => {
  const {app, server, el} = setup(t); await app.init(); assert.equal(el("reference-prev").disabled, true);
  el("reference-prev").emit("click"); assert.equal(app.state.referenceSelected, "neutral");
  el("reference-next").emit("click"); assert.equal(app.state.referenceSelected, "JawForward");
  el("reference-next").emit("click"); assert.equal(el("reference-next").disabled, true);
  el("reference-prev").emit("click"); assert.equal(app.state.referenceSelected, "JawForward");
  assert.ok(server.calls.every(call => call.method === "GET"));
});
test("added tongue controls are attributed separately from original morphs in both languages", async t => {
  const {app, server, el} = setup(t);
  server.reference.poses.push({id: "TongueOut", title: {en: "Tongue out", zh: "伸舌"}, front: "TongueOut.webp",
    animation: "TongueOut-motion.webp", provenance: ["original_model_morphs_with_original_correctives", "original_tongue_mesh_with_authored_skin_binding_and_post_skinning_controls"]});
  await app.init(); app.selectReference("TongueOut");
  assert.equal(el("reference-origin").textContent, messages.en.referenceAuthored);
  app.setLanguage("zh-CN"); assert.equal(el("reference-origin").textContent, messages["zh-CN"].referenceAuthored);
  el("reference-play").emit("click"); assert.match(el("reference-image").src, /TongueOut-motion.webp$/);
  app.selectReference("JawForward"); assert.equal(el("reference-origin").textContent, messages["zh-CN"].referenceOriginal);
  assert.ok(server.calls.every(call => call.method === "GET"));
});
test("independent lip controls carry their authored origin without changing labels or recording", async t => {
  const {app, server, el} = setup(t);
  server.reference.poses.push({id: "MouthUpperLeft", title: {en: "Upper lip left", zh: "上唇左移"}, front: "MouthUpperLeft.webp",
    provenance: ["original_model_morphs_with_original_correctives", "original_mouth_shift_deltas_with_authored_independent_lip_masks"]});
  await app.init(); app.selectReference("MouthUpperLeft");
  assert.equal(el("reference-origin").textContent, messages.en.referenceAuthored);
  assert.equal(server.status.recording.state, "idle"); assert.equal(el("record-consent").checked, false);
  assert.ok(server.calls.every(call => call.method === "GET"));
});
test("language switch preserves reference pose and image while translating instructions, options and alt text", async t => {
  const {app, server, el} = setup(t); await app.init(); app.selectReference("JawForward"); const count = server.calls.length;
  app.setLanguage("zh-CN"); assert.equal(el("reference-select").value, "JawForward");
  assert.equal(el("reference-pose").textContent, "下颌前伸"); assert.equal(el("reference-instruction").textContent, "下颌轻轻向前伸");
  assert.match(el("reference-image").attributes.alt, /下颌前伸/); assert.equal(server.calls.length, count);
});
test("recording guide follows the actual backend pose and cannot be changed by the practice browser", async t => {
  const {app, server, el} = setup(t); await app.init();
  server.status.recording = {state: "recording", session_id: "new", guide: {step: {id: "JawForward"}, phase: "hold"}};
  await app.pollStatus(); assert.equal(el("reference-select").disabled, true); assert.equal(el("reference-next").disabled, true);
  assert.equal(el("reference-mode").textContent, messages.en.referenceFollowing); assert.match(el("reference-image").src, /JawForward.webp/);
  app.selectReference("LipFunnelUpper"); assert.match(el("reference-image").src, /JawForward.webp/);
  server.status.recording.guide.step = {id: "LipFunnelUpper"}; await app.pollStatus();
  assert.match(el("reference-image").src, /LipFunnelUpper.webp/); assert.ok(!server.calls.some(call => call.path.includes("review")));
});
test("expired recording snapshot stops claiming a current pose and returns to labelled practice", async t => {
  const {app, server, el, advance} = setup(t); await app.init();
  server.status.recording = {state: "recording", guide: {step: {id: "JawForward"}, phase: "hold"}}; await app.pollStatus();
  advance(1600); app.setLanguage("en"); assert.equal(el("reference-mode").textContent, messages.en.referencePractice);
  assert.match(el("reference-image").src, /neutral.webp/);
});
test("missing reference bundle is explicit and does not block normal app controls", async t => {
  const {app, server, el} = setup(t); server.reference = null; await app.init();
  assert.equal(el("reference-views").hidden, true); assert.equal(el("reference-message").textContent, messages.en.referenceMissing);
  assert.equal(el("reference-select").disabled, true); assert.equal(el("capture").disabled, false); assert.equal(el("error").hidden, true);
});
test("a missing active pose is never substituted with the previous or neutral image", async t => {
  const {app, server, el} = setup(t); await app.init(); app.selectReference("JawForward");
  server.status.recording = {state: "recording", guide: {step: {id: "unknown"}, phase: "prepare"}}; await app.pollStatus();
  assert.equal(el("reference-views").hidden, true); assert.equal(el("reference-image").src, "");
  assert.equal(el("reference-message").textContent, messages.en.referencePoseMissing);
});
test("image load failure hides all reference views and a new selection clears the failure", async t => {
  const {app, el} = setup(t); await app.init(); app.selectReference("JawForward"); el("reference-image").emit("error");
  assert.equal(el("reference-views").hidden, true); assert.equal(el("reference-message").textContent, messages.en.referenceImageError);
  app.selectReference("LipFunnelUpper"); assert.equal(el("reference-views").hidden, false);
});
test("movement animation is opt-in and does not start any capture", async t => {
  const {app, server, el} = setup(t); await app.init(); app.selectReference("JawForward");
  assert.match(el("reference-image").src, /JawForward.webp$/); el("reference-play").emit("click");
  assert.match(el("reference-image").src, /JawForward-motion.webp$/); assert.equal(el("reference-play").attributes["aria-pressed"], "true");
  app.selectReference("LipFunnelUpper"); assert.match(el("reference-image").src, /LipFunnelUpper.webp$/);
  assert.ok(server.calls.every(call => call.method === "GET"));
});

function addTextOnlyReference(server) {
  const item = {id: "TongueOut", title: {en: "Extend tongue", zh: "伸舌"}, instruction: {en: "Extend comfortably", zh: "舒适地伸舌"},
    unavailable_reason: {en: "No tongue extension morph in the original model.", zh: "原模型没有伸舌形态键。"}};
  server.reference.poses.push(item); return item;
}
test("a text-only original-model gap clears every image and never requests undefined or substitutes neutral", async t => {
  const {app, server, el} = setup(t); const missing = addTextOnlyReference(server); await app.init(); app.selectReference("JawForward");
  el("reference-play").emit("click"); const count = server.calls.length; app.selectReference("TongueOut");
  for (const id of ["reference-image", "reference-neutral-image", "reference-profile-image"]) assert.equal(el(id).src, "");
  assert.equal(el("reference-views").hidden, true); assert.equal(el("reference-play").hidden, true);
  assert.equal(el("reference-message").textContent, missing.unavailable_reason.en); assert.equal(el("reference-origin").textContent, "");
  assert.equal(server.calls.length, count); assert.match(el("reference-select").children[3].textContent, /text only/);
});
test("text-only instructions, reason and selector label translate while retaining the pose", async t => {
  const {app, server, el} = setup(t); const missing = addTextOnlyReference(server); await app.init(); app.selectReference("TongueOut"); app.setLanguage("zh-CN");
  assert.equal(el("reference-select").value, "TongueOut"); assert.equal(el("reference-instruction").textContent, missing.instruction.zh);
  assert.equal(el("reference-message").textContent, missing.unavailable_reason.zh); assert.match(el("reference-select").children[3].textContent, /仅文字/);
  app.selectReference("JawForward"); assert.equal(el("reference-views").hidden, false); assert.match(el("reference-image").src, /JawForward.webp$/);
});
test("actual recording can follow a text-only step without skipping, approving or starting another action", async t => {
  const {app, server, el} = setup(t); const missing = addTextOnlyReference(server); await app.init();
  server.status.recording = {state: "recording", guide: {step: {id: "TongueOut", kind: "pose"}, phase: "prepare"}}; await app.pollStatus();
  assert.equal(el("reference-mode").textContent, messages.en.referenceFollowing); assert.equal(el("reference-message").textContent, missing.unavailable_reason.en);
  assert.equal(el("reference-views").hidden, true); assert.equal(el("reference-select").disabled, true);
  assert.ok(server.calls.every(call => call.method === "GET")); assert.equal(server.status.recording.guide.step.id, "TongueOut");
});
test("the archived occlusion entry is not selectable but still renders in a frozen guide", async t => {
  const {app, server, el} = setup(t);
  const old = {id: "negative_occluded", legacy_only: true, title: {en: "Occluded"}, unavailable_reason: {en: "Legacy text."}};
  server.reference.poses.splice(1, 0, old);
  await app.init();
  assert.ok(![...el("reference-select").children].some(option => option.value === old.id));
  app.selectReference(old.id);
  assert.notEqual(el("reference-select").value, old.id);
  server.status.recording = {state: "recording", guide: {step: {id: old.id}, phase: "prepare"}};
  await app.pollStatus();
  assert.equal(el("reference-message").textContent, old.unavailable_reason.en);
});
test("automatic guide text matches the controls that are actually shown", async t => {
  const f = setup(t);
  const guide = {automatic: true, paused: false, index: 0, total: 3, preset_id: "complete", phase: "prepare", frame: "f1",
    display_ready: true, remaining_seconds: 2, step: {id: "JawForward", kind: "pose", title: {en: "Jaw forward", zh: "下颌前伸"}}};
  f.server.status.recording = {state: "recording", session_id: "auto", guide};
  await f.app.init(); f.app.navigate("recording");
  for (const language of ["en", "zh-CN"]) {
    f.app.setLanguage(language); const text = messages[language];
    guide.index = 0; guide.phase = "prepare"; await f.app.pollStatus();
    assert.equal(f.el("guide-step").textContent, `${text.autoPrepare} ${language === "en" ? "Jaw forward" : "下颌前伸"}`);
    assert.equal(f.el("guide-hint").textContent, text.autoPrepareHelp);
    assert.equal(f.el("capture-progress-phase").textContent, text.capturePrepare);
    guide.phase = "hold"; await f.app.pollStatus();
    assert.equal(f.el("guide-hint").textContent, text.autoHoldHelp);
    assert.equal(f.el("capture-progress-phase").textContent, text.captureHold);
    for (const id of ["guide-ready", "guide-finish", "guide-confirm"]) assert.equal(f.el(id).hidden, true, id);
    guide.index = guide.total - 1; await f.app.pollStatus();
    assert.equal(f.el("guide-hint").textContent, text.autoFinalHoldHelp);
    for (const key of ["autoPrepareHelp", "autoHoldHelp", "autoFinalHoldHelp", "autoPausedHelp", "autoShowImage"])
      assert.doesNotMatch(text[key], /开始保持|结束保持|确认已完成|Start holding|Finish holding|Confirm action/, key);
  }
});
test("moving steps keep their animation during the hold and still steps show the still image", async t => {
  const f = setup(t);
  f.server.reference.poses.push({id: "tongue_sweep", title: {en: "Sweep", zh: "往返"}, instruction: {en: "Sweep", zh: "往返"}, front: "tongue_sweep.webp", animation: "tongue_sweep-motion.webp", provenance: []});
  const guide = {automatic: true, paused: false, index: 0, total: 2, preset_id: "complete", phase: "hold", frame: "f1", display_ready: true,
    remaining_seconds: 2, step: {id: "tongue_sweep", kind: "continuous", title: {en: "Sweep", zh: "往返"}}};
  f.server.status.recording = {state: "recording", session_id: "auto", guide};
  await f.app.init(); f.app.navigate("recording");
  assert.equal(f.el("reference-image").src, "/static/guide-reference/tongue_sweep-motion.webp");
  guide.step = {id: "JawForward", kind: "pose", title: {en: "Jaw forward", zh: "下颌前伸"}}; guide.frame = "f2"; await f.app.pollStatus();
  assert.equal(f.el("reference-image").src, "/static/guide-reference/JawForward.webp");
});
test("the optional refit suggestion appears only after a recording is saved", async t => {
  const f = setup(t); await f.app.init(); f.app.navigate("recording");
  assert.notEqual(f.el("record-next").textContent, messages.en.recordRefit);
  f.server.status.capture_control = {state: "completed"}; f.server.status.managed_capture = true;
  f.server.status.recording = {state:"stopped", guide:{phase:"finished"}}; await f.app.pollStatus();
  assert.equal(f.el("record-next").textContent, messages.en.recordRefit);
  assert.ok(!["capturePrepare", "captureHold", "autoPrepareHelp", "autoHoldHelp"].some(key => messages.en[key].includes(messages.en.recordRefit)));
});
test("reference follows guide.current consistently with the main recording prompt", async t => {
  const {app, server, el} = setup(t); await app.init();
  server.status.recording = {state: "recording", guide: {current: {id: "JawForward"}, step: {id: "LipFunnelUpper"}, phase: "hold"}};
  await app.pollStatus(); assert.match(el("reference-image").src, /JawForward.webp$/);
});

test("discovery selects a unique verified source as a draft without starting or saving", async t => {
  const {app, server, el} = setup(t);
  server.config.headset_host = "";
  server.discovered = [{host: "198.51.100.8", port: 45681, name: "PICO Mouth", session_id: "44"}];
  await app.init();
  assert.equal(el("headset_host").value, "198.51.100.8");
  assert.equal(el("headset-select").value, "198.51.100.8:45681");
  assert.equal(app.state.dirty, true); assert.equal(server.config.headset_host, "");
  assert.ok(server.calls.every(call => call.method === "GET"));
});
test("multiple discoveries require explicit selection, preserving device ports", async t => {
  const {app, server, el} = setup(t); server.config.headset_host = "";
  server.discovered = [{host: "198.51.100.8", port: 45681, name: "PICO Mouth"}, {host: "198.51.100.9", port: 45690, name: "PICO Mouth"}];
  await app.init(); assert.equal(el("headset_host").value, "");
  el("headset-select").value = "198.51.100.9:45690"; el("headset-select").emit("change");
  assert.equal(el("headset_host").value, "198.51.100.9"); assert.equal(el("headset_port").value, "45690");
  assert.ok(server.calls.every(call => call.method === "GET"));
  el("headset-select").value = ""; el("headset-select").emit("change");
  assert.equal(el("headset_host").value, "");
});
test("refresh does not replace manual or unsaved connection settings", async t => {
  const {app, server, el} = setup(t); await app.init();
  el("headset_host").value = "manual.local"; el("page-preferences").emit("input");
  server.discovered = [{host: "198.51.100.8", port: 45681, name: "PICO Mouth"}];
  await app.discover(); assert.equal(el("headset_host").value, "manual.local");
  app.setLanguage("zh-CN"); assert.equal(el("headset_host").value, "manual.local");
});
test("model picker returns a real local path as an unsaved choice without loading or starting", async t => {
  const {app, server, el} = setup(t); await app.init();
  server.selectedModel = {cancelled: false, path: "D:/my models/个人模型.onnx"};
  await app.chooseModel();
  assert.equal(el("model_path").value, server.selectedModel.path); assert.equal(el("model-name").textContent, "个人模型.onnx");
  assert.equal(app.state.dirty, true); assert.equal(server.config.model_path, "D:/models/student.onnx");
  assert.deepEqual(server.calls.filter(call => call.method === "POST").map(call => call.path), ["/api/model/select"]);
  app.setLanguage("en"); assert.equal(el("model_path").value, server.selectedModel.path);
});
test("cancelling the model picker keeps the current model and settings unchanged", async t => {
  const {app, server, el} = setup(t); await app.init(); await app.chooseModel();
  assert.equal(el("model_path").value, server.config.model_path); assert.equal(app.state.dirty, false);
});
test("file dialog cannot open twice and late completion cannot update a destroyed page", async t => {
  const {app, server, el} = setup(t); await app.init(); let resolve;
  server.before = path => path === "/api/model/select" ? new Promise(done => { resolve = done; }) : null;
  const pending = app.chooseModel(); await app.chooseModel();
  assert.equal(server.calls.filter(call => call.path === "/api/model/select").length, 1);
  app.destroy(); resolve(Response.json({path: "D:/late.onnx", cancelled: false})); await pending;
  assert.equal(el("model_path").value, server.config.model_path);
});
test("navigation displays exactly one task page and never starts or saves anything", async t => {
  const {app, el, server} = setup(t); await app.init(); const count = server.calls.length;
  for (const [page, nav] of [["recording", "recording"], ["training", "training"], ["diagnostics", "diagnostics"], ["preferences", "advanced"], ["tracking", "workspace"]]) {
    el(`nav-${nav}`).emit("click");
    assert.deepEqual(PAGES.filter(id => !el(`page-${id}`).hidden), [page]);
    assert.equal(el("connection-setup").hidden, !["tracking", "recording"].includes(page));
    assert.equal(el("model-setup").hidden, page !== "tracking");
    assert.equal(el(`nav-${nav}`).attributes["aria-current"], "page");
  }
  assert.equal(server.calls.length, count); assert.equal(el("record-consent").checked, false);
});
test("HTML contains unique IDs and external advanced fields belong to the settings form", () => {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const field of ["headset_host", "headset_port", "model_path", "device", "stale_ms", "preview_fps", "output_enabled", "output_port", "replay_path", "filter_enabled", "filter_min_cutoff", "filter_beta", "filter_d_cutoff"])
    assert.match(html, new RegExp(`id="${field}" form="settings"`));
  for (const floatField of ["filter_min_cutoff", "filter_beta", "filter_d_cutoff", "stale_ms"])
    assert.match(html, new RegExp(`id="${floatField}"[^>]*step="any"`));
});
test("direct links and browser history switch pages without recreating the app", async t => {
  const events = new Map(), navigation = {location: {hash: "#/recording"},
    addEventListener: (name, fn) => events.set(name, fn), removeEventListener: name => events.delete(name)};
  const {app, el, server} = setup(t, {navigation}); await app.init();
  assert.equal(app.state.page, "recording"); assert.equal(el("page-tracking").hidden, true);
  app.selectReference("JawForward"); el("record-consent").checked = true; el("model_path").value = "D:/draft.onnx"; el("settings").emit("input");
  el("nav-advanced").emit("click"); assert.equal(navigation.location.hash, "#/preferences");
  navigation.location.hash = "#/recording"; events.get("hashchange")();
  assert.equal(app.state.page, "recording"); assert.equal(app.state.referenceSelected, "JawForward");
  assert.equal(el("model_path").value, "D:/draft.onnx"); assert.equal(el("record-consent").checked, true);
  assert.ok(server.calls.every(call => call.method === "GET"));
  app.destroy(); assert.equal(events.size, 0);
});
test("invalid and empty routes use tracking, while all five task routes are supported", () => {
  for (const page of PAGES) assert.equal(routePage(`#/${page}`), page);
  assert.equal(routePage("#unexpected"), "tracking"); assert.equal(routePage(), "tracking");
});
test("page switching keeps active tracking alive but does not fetch hidden previews", async t => {
  const {app, server} = setup(t); await app.init(); await app.start(); const count = server.calls.length;
  app.navigate("training"); await app.refreshPreview();
  assert.equal(server.status.running, true); assert.equal(server.calls.length, count);
  app.navigate("preferences"); app.navigate("recording"); await app.refreshPreview();
  assert.equal(server.status.running, true); assert.equal(server.calls.filter(c => c.path === "/api/start").length, 1);
});
test("recording and training expose only the relevant primary actions", async t => {
  const {app, el} = setup(t); await app.init(); app.navigate("recording");
  assert.equal(el("capture").hidden, false); assert.equal(el("capture-stop").hidden, true);
  assert.equal(el("record-stop").hidden, true); assert.equal(el("record-cancel").hidden, true);
  assert.equal(el("record-options").open, false); assert.equal(el("reference-picker").open, false);
  app.navigate("training"); assert.equal(el("training-tools").open, false); assert.equal(el("training-status").hidden, true);
});
test("plain errors are shown in context while raw request details stay in diagnostics", async t => {
  const {app, el, server} = setup(t); await app.init(); server.failStart = true; await app.start();
  assert.equal(el("error").textContent, messages.en.modelMissing);
  assert.doesNotMatch(el("error").textContent, /POST|HTTP|fixture/);
  assert.match(el("technical-error").textContent, /POST \/api\/start.*fixture/);
});


test("Cymple eye relay starts folded and idle, keeps options folded, never auto-POSTs, and translates labels", async t => {
  const f = setup(t);
  await f.app.init();
  assert.ok(f.server.calls.every(c => c.method === "GET"));
  assert.equal(f.el("cymple-relay").hidden, false);
  assert.equal(f.el("cymple-relay").open, false);
  assert.equal(f.el("cymple-relay-options").open, false);
  assert.equal(f.el("cymple-relay-status").textContent, messages.en.cympleStateIdle);
  assert.equal(f.el("cymple-relay-start").disabled, false);
  assert.equal(f.el("cymple-relay-stop").hidden, true);
  assert.equal(f.el("cymple-left-metrics").textContent, "—");
  const n = f.server.calls.length;
  f.el("cymple-relay").open = true;
  f.app.setLanguage("zh-CN");
  await f.app.pollStatus();
  assert.equal(f.el("cymple-relay").open, true);
  assert.equal(f.server.calls.length, n + 1);
  assert.equal(f.el("cymple-relay-title").textContent, messages["zh-CN"].cympleRelayTitle);
  assert.equal(f.el("cymple-relay-start").textContent, messages["zh-CN"].cympleRelayStart);
  assert.equal(f.el("cymple-relay-status").textContent, messages["zh-CN"].cympleStateIdle);
});

test("Cymple eye relay starts/stops without a mouth model and never dirties mouth settings", async t => {
  const f = setup(t);
  f.server.config.model_path = "";
  await f.app.init();
  assert.equal(f.el("cymple-relay-start").disabled, false);

  f.el("cymple-left-rotate").value = "90"; f.el("cymple-left-rotate").emit("change");
  f.el("cymple-right-vflip").checked = true; f.el("cymple-right-vflip").emit("change");
  f.el("cymple-port").value = "23309"; f.el("cymple-port").emit("input");
  assert.equal(f.app.state.relayDirty, true);
  assert.equal(f.app.state.dirty, false);
  assert.equal(f.el("save").disabled, true);

  await f.el("cymple-relay-start").emit("click");
  const relayCall = f.server.calls.find(c => c.path === "/api/cymple-relay");
  const reqBody = JSON.parse(relayCall.body);
  assert.equal(reqBody.action, "start");
  assert.equal(reqBody.headset_host, "headset.example");
  assert.equal(reqBody.cymple_port, 23309);
  assert.equal(reqBody.left_rotate, 90);
  assert.equal(reqBody.right_vflip, true);

  assert.equal(f.el("cymple-relay-status").textContent, messages.en.cympleStateRunning);
  assert.equal(f.el("cymple-relay-start").hidden, true);
  assert.equal(f.el("cymple-relay-stop").hidden, false);
  assert.equal(f.el("cymple-relay-stop").disabled, false);
  assert.equal(f.el("cymple-left-rotate").disabled, true);
  assert.match(f.el("cymple-relay-endpoint").textContent, /headset\.example \(45682\/45683\) → 127\.0\.0\.1:23309/);
  assert.match(f.el("cymple-left-metrics").textContent, /90\.0 fps · 28\.4 KB\/f · 45 frames/);

  await f.el("cymple-relay-stop").emit("click");
  assert.equal(f.el("cymple-relay-status").textContent, messages.en.cympleStateIdle);
  assert.equal(f.el("cymple-relay-start").hidden, false);
  assert.equal(f.el("cymple-left-rotate").disabled, false);
});

test("discovered 3-camera PICO device auto-fills left/right eye ports without dirtying mouth Save", async t => {
  const f = setup(t);
  f.server.config.headset_host = "";
  f.server.discovered = [
    {host: "198.51.100.8", port: 45681, name: "PICO Cameras", streams: [
      {id: "mouth", port: 45681, path: "/stream.mjpg"},
      {id: "eye_left", port: 46101, path: "/stream.mjpg"},
      {id: "eye_right", port: 46102, path: "/stream.mjpg"}
    ]},
    {host: "198.51.100.9", port: 45681, name: "PICO Cameras", streams: [
      {id: "mouth", port: 45681, path: "/stream.mjpg"},
      {id: "eye_left", port: 46201, path: "/stream.mjpg"},
      {id: "eye_right", port: 46202, path: "/stream.mjpg"}
    ]}
  ];
  await f.app.init();
  f.el("headset-select").value = "198.51.100.9:45681";
  f.el("headset-select").emit("change");
  assert.equal(f.el("cymple-left-port").value, "46201");
  assert.equal(f.el("cymple-right-port").value, "46202");

  const fSingle = setup(t);
  fSingle.server.config.headset_host = "";
  fSingle.server.discovered = [f.server.discovered[0]];
  await fSingle.app.init();
  assert.equal(fSingle.el("cymple-left-port").value, "46101");
  assert.equal(fSingle.el("cymple-right-port").value, "46102");
  await fSingle.app.pollStatus();
  assert.equal(fSingle.el("cymple-left-port").value, "46101");
});

test("read_only, sealed_c and active managed capture disable or hide Cymple relay controls", async t => {
  const fSealed = setup(t);
  fSealed.server.status.capture_control = {sealed_c: true, state: "idle"};
  await fSealed.app.init();
  assert.equal(fSealed.el("cymple-relay").hidden, true);
  assert.equal(fSealed.el("cymple-relay-start").disabled, true);
  await fSealed.app.cympleRelay("start");
  assert.ok(!fSealed.server.calls.some(c => c.path === "/api/cymple-relay"));

  const fReadOnly = setup(t);
  externalGuide(fReadOnly);
  await fReadOnly.app.init();
  assert.equal(fReadOnly.el("cymple-relay").hidden, true);
  assert.equal(fReadOnly.el("cymple-relay-start").disabled, true);

  const fCapture = setup(t);
  fCapture.server.status.capture_control = {state: "recording"};
  await fCapture.app.init();
  assert.equal(fCapture.el("cymple-relay").hidden, false);
  assert.equal(fCapture.el("cymple-relay-start").disabled, true);
});

test("three routing presets, 46-target customization, filters and bilingual switching apply live without stopping", async t => {
  const {app, server, el, row} = setup(t);
  await app.init();
  await app.start();
  assert.equal(app.morphControls.size, 46);
  assert.equal(ROUTABLE_CHANNELS.length, 46);
  assert.equal(app.morphGroups.length, 6);
  assert.equal(el("routing-preset-badge").textContent, messages.en.presetStateCurated);
  assert.equal(el("preset-curated").attributes["aria-pressed"], "true");
  assert.equal(el("preset-hybrid").attributes["aria-pressed"], "false");
  assert.equal(el("preset-model-only").attributes["aria-pressed"], "false");
  assert.match(el("hybrid-channels-summary").textContent, /Native 28 · Model 18/);
  assert.equal(el("hybrid-live-hint").hidden, false);
  assert.equal(el("hybrid-live-hint").textContent, messages.en.hybridStockWaiting);

  // Switch to Full native hybrid preset (37 native, 9 missing on model).
  el("preset-hybrid").emit("click");
  assert.equal(el("routing-preset-badge").textContent, messages.en.presetStateHybrid);
  assert.match(el("hybrid-channels-summary").textContent, /Native 37 · Model 9/);
  await app.save();
  const hybridPost = JSON.parse(server.calls.filter(c => c.path === "/api/config" && c.method === "POST").at(-1).body);
  assert.equal(hybridPost.mouth_mode, "hybrid");
  assert.equal(hybridPost.channel_sources, null);
  assert.equal(server.status.running, true);

  // Switch to Model only preset (0 native, 46 model) and verify panel stays accessible for per-target overrides.
  el("preset-model-only").emit("click");
  assert.equal(el("routing-preset-badge").textContent, messages.en.presetStateModel);
  assert.match(el("hybrid-channels-summary").textContent, /Native 0 · Model 46/);
  assert.equal(el("hybrid-live-hint").hidden, true);
  await app.save();
  const modelPost = JSON.parse(server.calls.filter(c => c.path === "/api/config" && c.method === "POST").at(-1).body);
  assert.equal(modelPost.mouth_mode, "model_only");
  assert.equal(modelPost.channel_sources, null);

  // Customize individual morph targets (preset -> custom routing -> bilingual switch preserves edits -> save).
  el("preset-curated").emit("click");
  app.morphControls.get("MouthSmileL").stockBtn.emit("click");
  app.morphControls.get("TongueRoll").offBtn.emit("click");
  assert.equal(app.state.dirty, true);
  assert.equal(el("routing-preset-badge").textContent, messages.en.presetStateCustom);
  assert.match(el("hybrid-channels-summary").textContent, /Native 29 · Model 16 · Off 1/);

  app.setLanguage("zh-CN");
  assert.equal(el("routing-preset-badge").textContent, messages["zh-CN"].presetStateCustom);
  assert.equal(el("preset-curated-title").textContent, messages["zh-CN"].presetCuratedTitle);
  assert.equal(app.morphControls.get("JawOpen").tagEl.textContent, messages["zh-CN"].tagGood);
  assert.equal(el("channel-search").attributes.placeholder, messages["zh-CN"].channelSearchPlaceholder);

  await app.save();
  const customPost = JSON.parse(server.calls.filter(c => c.path === "/api/config" && c.method === "POST").at(-1).body);
  assert.equal(customPost.mouth_mode, "custom");
  assert.equal(customPost.channel_sources.MouthSmileL, "stock");
  assert.equal(customPost.channel_sources.TongueRoll, "off");
  assert.equal(customPost.channel_sources.JawOpen, "stock");
  assert.equal(server.status.running, true);

  // Filter chips and search do not mark settings dirty.
  el("filter-stock").emit("click");
  assert.equal(app.state.dirty, false);
  assert.equal(app.morphControls.get("MouthSmileL").rowEl.hidden, false);
  assert.equal(app.morphControls.get("TongueLeft").rowEl.hidden, true);
  el("filter-good").emit("click");
  assert.equal(app.morphControls.get("LipFunnelUpper").rowEl.hidden, false);
  assert.equal(app.morphControls.get("CheekPuffLeft").rowEl.hidden, false);
  assert.equal(app.morphControls.get("MouthSmileL").rowEl.hidden, true);
  el("filter-all").emit("click");
  el("channel-search").value = "funnel";
  el("channel-search").emit("input");
  assert.equal(app.state.dirty, false);
  assert.equal(app.morphControls.get("LipFunnelUpper").rowEl.hidden, false);
  assert.equal(app.morphControls.get("JawOpen").rowEl.hidden, true);
  el("channel-search").value = "";
  el("channel-search").emit("input");

  // Returning individual overrides to match a preset restores preset identity.
  app.morphControls.get("MouthSmileL").modelBtn.emit("click");
  app.morphControls.get("TongueRoll").modelBtn.emit("click");
  assert.equal(el("routing-preset-badge").textContent, messages["zh-CN"].presetStateCurated);
  await app.save();
  const restoredPost = JSON.parse(server.calls.filter(c => c.path === "/api/config" && c.method === "POST").at(-1).body);
  assert.equal(restoredPost.mouth_mode, "curated");
  assert.equal(restoredPost.channel_sources, null);

  Object.assign(server.status.prediction, {
    valid_mask: String(1n << BigInt(server.status.contract.channels.indexOf("JawOpen"))),
    native_channels: ["JawOpen"],
    channels: {JawOpen: .45}
  });
  server.status.live_stock = {wanted: true, active: true, fresh: true};
  await app.pollStatus();
  assert.equal(row("JawOpen").output.textContent, "0.450");
  assert.equal(app.rows.get("channel-table/TongueUp").output.textContent, "—");
  assert.equal(el("hybrid-live-hint").textContent, messages["zh-CN"].hybridStockFresh);
});

test("legacy hybrid_channels array migrates cleanly into per-morph-target routing", async t => {
  const {app, server, el} = setup(t);
  server.config.mouth_mode = "hybrid";
  server.config.hybrid_channels = ["MouthFunnel", "JawOpen"];
  server.config.channel_sources = null;
  await app.init();
  assert.equal(app.morphControls.get("JawOpen").stockBtn.attributes["aria-pressed"], "true");
  assert.equal(app.morphControls.get("LipFunnelUpper").stockBtn.attributes["aria-pressed"], "true");
  assert.equal(app.morphControls.get("LipFunnelLower").stockBtn.attributes["aria-pressed"], "true");
  assert.equal(app.morphControls.get("MouthSmileL").modelBtn.attributes["aria-pressed"], "true");
  assert.match(el("hybrid-channels-summary").textContent, /Native 3 · Model 43/);
});

test("diagnostics page strictly separates expression monitor and connection diagnostics with quick links and alert badge", async t => {
  const {app, server, el} = setup(t);
  await app.init();
  const initialCalls = server.calls.length;

  // Quick-jump from Tracking opens Diagnostics on Expression monitor tab.
  el("diag-tab-connection").emit("click");
  assert.equal(app.state.diagView, "connection");
  el("go-diagnostics").emit("click");
  assert.equal(app.state.page, "diagnostics");
  assert.equal(el("page-diagnostics").hidden, false);
  assert.equal(app.state.diagView, "expressions");
  assert.equal(el("expression-diagnostics").hidden, false);
  assert.equal(el("diagnostics").hidden, true);
  assert.equal(el("diag-tab-expressions").attributes["aria-selected"], "true");
  assert.equal(el("diag-tab-connection").attributes["aria-selected"], "false");
  assert.equal(app.state.dirty, false);

  // Switch to Connection diagnostics tab and back; Settings quick-link opens Diagnostics cleanly.
  el("diag-tab-connection").emit("click");
  assert.equal(el("expression-diagnostics").hidden, true);
  assert.equal(el("diagnostics").hidden, false);
  assert.equal(el("diag-tab-connection").attributes["aria-selected"], "true");
  assert.equal(el("diag-conn-badge").attributes["data-alert"], "false");
  el("nav-advanced").emit("click");
  assert.equal(app.state.page, "preferences");
  el("settings-go-diagnostics").emit("click");
  assert.equal(app.state.page, "diagnostics");
  assert.equal(server.calls.length, initialCalls);

  // Error surface sets Connection diagnostics badge alert without marking settings dirty.
  server.failStart = true;
  await app.start();
  assert.equal(el("diag-conn-badge").attributes["data-alert"], "true");
  assert.match(el("diag-conn-badge").textContent, /^Issue \(1\)/);
  assert.equal(el("conn-error-card").attributes["data-has-error"], "true");
  assert.equal(app.state.dirty, false);
});

test("expression monitor renders 50 anatomical channels with live origin tags, region tabs, active/source filters and search", async t => {
  const FULL_50_CHANNELS = DIAGNOSTIC_GROUPS.flatMap(g => g.items.map(i => i.name));
  assert.equal(FULL_50_CHANNELS.length, 50);
  const {app, server, el, row} = setup(t);
  server.status.contract.channels = FULL_50_CHANNELS;
  server.status.model.trained_channels = FULL_50_CHANNELS.filter(n => n !== "TongueRoll");
  await app.init();

  // Save custom routing: curated defaults with TongueUp=off and JawOpen/MouthPucker on stock.
  app.morphControls.get("TongueUp").offBtn.emit("click");
  await app.save();
  await app.start();

  const tongueUpIndex = FULL_50_CHANNELS.indexOf("TongueUp");
  const allExceptTongueUp = ((1n << 50n) - 1n) & ~(1n << BigInt(tongueUpIndex));
  Object.assign(server.status.prediction, {
    valid_mask: allExceptTongueUp.toString(),
    native_channels: ["JawOpen"],
    channels: {JawOpen: 0.62, MouthPucker: 0.35, MouthSmileL: 0.015, TongueLeft: 0.48, TongueUp: 0.7, TongueRoll: 0.8},
  });
  server.status.live_stock = {wanted: true, active: true, fresh: true, age_ms: 4.2};
  await app.pollStatus();

  assert.equal(app.exprGroups.length, 7);
  assert.equal(row("JawOpen").output.textContent, "0.620");
  assert.equal(row("JawOpen").originEl.textContent, messages.en.originNativeLive);
  assert.equal(row("JawOpen").cardEl.attributes["data-active"], "true");
  assert.equal(row("JawOpen").cardEl.attributes["data-origin"], "native");

  assert.equal(row("MouthPucker").originEl.textContent, messages.en.originModelFallback);
  assert.equal(row("MouthPucker").cardEl.attributes["data-active"], "true");

  assert.equal(app.rows.get("channel-table/TongueLeft").output.textContent, "0.480");
  assert.equal(app.rows.get("channel-table/TongueLeft").originEl.textContent, messages.en.originModel);
  assert.equal(app.rows.get("channel-table/MouthSmileL").cellEl.attributes["data-active"], "false");
  assert.equal(app.rows.get("channel-table/TongueUp").output.textContent, "—");
  assert.equal(app.rows.get("channel-table/TongueUp").originEl.textContent, messages.en.originOff);
  assert.equal(app.rows.get("channel-table/TongueRoll").output.textContent, "—");
  assert.equal(app.rows.get("channel-table/TongueRoll").originEl.textContent, messages.en.originUntrained);
  assert.match(el("live-stock-info").textContent, /Connected · fresh \(4\.2 ms\)/);

  // Active filter (> 0.02): MouthSmileL (0.015) hidden, JawOpen/TongueLeft visible.
  el("expr-active-only").emit("click");
  assert.equal(app.rows.get("channel-table/JawOpen").cellEl.hidden, false);
  assert.equal(app.rows.get("channel-table/TongueLeft").cellEl.hidden, false);
  assert.equal(app.rows.get("channel-table/MouthSmileL").cellEl.hidden, true);
  el("expr-active-only").emit("click");

  // Source filter: Native vs New model.
  el("expr-src-native").emit("click");
  assert.equal(app.rows.get("channel-table/JawOpen").cellEl.hidden, false);
  assert.equal(app.rows.get("channel-table/MouthPucker").cellEl.hidden, false);
  assert.equal(app.rows.get("channel-table/LipFunnelUpper").cellEl.hidden, false);
  assert.equal(app.rows.get("channel-table/TongueLeft").cellEl.hidden, true);
  el("expr-src-model").emit("click");
  assert.equal(app.rows.get("channel-table/TongueLeft").cellEl.hidden, false);
  assert.equal(app.rows.get("channel-table/JawOpen").cellEl.hidden, true);
  assert.equal(app.rows.get("channel-table/TongueRoll").cellEl.hidden, true);
  el("expr-src-all").emit("click");

  // Region tab filtering: clicking tongue shows only tongue group.
  const tongueRegionBtn = el("expr-regions").children.find(b => b.attributes["data-region"] === "tongue");
  const allRegionBtn = el("expr-regions").children.find(b => b.attributes["data-region"] === "all");
  tongueRegionBtn.emit("click");
  assert.equal(app.exprGroups.find(g => g.group.id === "tongue").groupEl.hidden, false);
  assert.equal(app.exprGroups.find(g => g.group.id === "jaw").groupEl.hidden, true);
  allRegionBtn.emit("click");

  // Search filtering & empty state.
  el("expr-search").value = "smile";
  el("expr-search").emit("input");
  assert.equal(app.rows.get("channel-table/MouthSmileL").cellEl.hidden, false);
  assert.equal(app.rows.get("channel-table/JawOpen").cellEl.hidden, true);
  assert.equal(el("expr-empty").hidden, true);

  el("expr-search").value = "nonexistent-xyz";
  el("expr-search").emit("input");
  assert.equal(el("expr-empty").hidden, false);
  assert.equal(app.state.dirty, false);
});

test("expression freeze pauses readout updates and auto-clears on stop or offline", async t => {
  const {app, server, el, row} = setup(t);
  await app.init();
  assert.equal(el("expr-freeze").disabled, true);

  await app.start();
  server.status.prediction.channels.JawOpen = 0.30;
  await app.pollStatus();
  assert.equal(el("expr-freeze").disabled, false);
  assert.equal(row("JawOpen").output.textContent, "0.300");

  // Freeze captures current readouts in UI while ignoring live status drift.
  el("expr-freeze").emit("click");
  assert.equal(app.state.exprFrozen, true);
  assert.equal(el("expr-freeze").textContent, messages.en.exprResume);
  assert.equal(el("expr-live-pill").textContent, messages.en.exprFrozen);

  server.status.prediction.channels.JawOpen = 0.88;
  await app.pollStatus();
  assert.equal(row("JawOpen").output.textContent, "0.300");
  assert.equal(app.rows.get("channel-table/JawOpen").output.textContent, "0.300");

  // Resume immediately reveals live 0.880 value.
  el("expr-freeze").emit("click");
  assert.equal(app.state.exprFrozen, false);
  assert.equal(row("JawOpen").output.textContent, "0.880");

  // Freeze again, then Stop: outputs must clear to "—" and unfreeze automatically.
  el("expr-freeze").emit("click");
  assert.equal(app.state.exprFrozen, true);
  await app.stop();
  assert.equal(app.state.exprFrozen, false);
  assert.equal(row("JawOpen").output.textContent, "—");
  assert.equal(app.rows.get("channel-table/JawOpen").output.textContent, "—");
  assert.equal(el("expr-freeze").disabled, true);
});

test("language switch translates diagnostics tabs, regions, origin badges and connection cards while keeping UI filters", async t => {
  const {app, server, el, row} = setup(t);
  await app.init();
  await app.start();
  el("diag-tab-connection").emit("click");
  el("expr-active-only").emit("click");
  el("expr-search").value = "jaw";
  el("expr-search").emit("input");
  const tongueRegionBtn = el("expr-regions").children.find(b => b.attributes["data-region"] === "tongue");
  tongueRegionBtn.emit("click");
  el("expr-freeze").emit("click");

  const callsBefore = server.calls.length;
  app.setLanguage("zh-CN");
  assert.equal(server.calls.length, callsBefore);
  assert.equal(app.state.diagView, "connection");
  assert.equal(app.state.exprActiveOnly, true);
  assert.equal(app.state.exprQuery, "jaw");
  assert.equal(app.state.exprRegion, "tongue");
  assert.equal(app.state.exprFrozen, true);

  assert.equal(el("nav-diagnostics-label").textContent, messages["zh-CN"].navDiagnostics);
  assert.equal(el("diag-tab-expressions-label").textContent, messages["zh-CN"].diagTabExpressions);
  assert.equal(el("diag-tab-connection-label").textContent, messages["zh-CN"].diagTabConnection);
  assert.equal(el("conn-card-rates-title").textContent, messages["zh-CN"].connCardRates);
  assert.equal(el("expr-freeze").textContent, messages["zh-CN"].exprResume);
  assert.equal(tongueRegionBtn.textContent, messages["zh-CN"].regionTongue);
  assert.equal(app.rows.get("channel-table/TongueLeft").originEl.textContent, messages["zh-CN"].originModel);
});

test("pinned monitor supports pinning, unpinning, persistence, clear and reset defaults", async t => {
  const {app, el, row, saved} = setup(t);
  await app.init();
  assert.deepEqual(app.state.pinnedChannels, PRIMARY_CHANNELS);
  assert.equal(el("outputs").hidden, false);
  assert.equal(el("expr-pins-empty").hidden, true);
  assert.equal(el("expr-pins-reset").hidden, true);
  assert.equal(el("expr-pins-clear").hidden, false);

  // Pin TongueLeft from the 50-channel table.
  app.rows.get("channel-table/TongueLeft").pinBtn.emit("click");
  assert.ok(app.state.pinnedChannels.includes("TongueLeft"));
  assert.ok(row("TongueLeft"));
  assert.equal(el("expr-pins-reset").hidden, false);
  assert.deepEqual(JSON.parse(saved.get(PINNED_CHANNELS_KEY)), app.state.pinnedChannels);

  // Unpin directly from the top pinned card.
  row("JawOpen").pinBtn.emit("click");
  assert.ok(!app.state.pinnedChannels.includes("JawOpen"));
  assert.equal(row("JawOpen"), undefined);
  assert.equal(app.rows.get("channel-table/JawOpen").pinBtn.attributes["aria-pressed"], "false");

  // Clear all pins hides grid and shows compact empty hint.
  el("expr-pins-clear").emit("click");
  assert.deepEqual(app.state.pinnedChannels, []);
  assert.equal(el("outputs").hidden, true);
  assert.equal(el("expr-pins-empty").hidden, false);
  assert.equal(el("expr-pins-clear").hidden, true);
  assert.equal(el("expr-pins-reset").hidden, false);

  // Reset restores default balanced channels.
  el("expr-pins-reset").emit("click");
  assert.deepEqual(app.state.pinnedChannels, PRIMARY_CHANNELS);
  assert.equal(el("outputs").hidden, false);
  assert.equal(el("expr-pins-empty").hidden, true);
  assert.equal(el("expr-pins-reset").hidden, true);
});

test("expression monitor polls /api/expressions at 30 Hz only when live and visible, pausing on freeze or tab switch", async t => {
  const {app, server, el, doc, row} = setup(t);
  await app.init();
  await app.start();
  assert.equal(EXPRESSION_POLL_MS, 33);

  // On tracking page, pollExpressions is a no-op.
  const callsBefore = server.calls.filter(c => c.path === "/api/expressions").length;
  await app.pollExpressions();
  assert.equal(server.calls.filter(c => c.path === "/api/expressions").length, callsBefore);

  // Navigate to diagnostics → expressions: pollExpressions updates readouts directly.
  app.navigate("diagnostics");
  server.status.prediction.channels.JawOpen = 0.777;
  await app.pollExpressions();
  assert.equal(server.calls.filter(c => c.path === "/api/expressions").length, callsBefore + 1);
  assert.equal(row("JawOpen").output.textContent, "0.777");
  assert.equal(app.rows.get("channel-table/JawOpen").output.textContent, "0.777");

  // Freeze pauses /api/expressions requests.
  el("expr-freeze").emit("click");
  const frozenCalls = server.calls.filter(c => c.path === "/api/expressions").length;
  server.status.prediction.channels.JawOpen = 0.111;
  await app.pollExpressions();
  assert.equal(server.calls.filter(c => c.path === "/api/expressions").length, frozenCalls);
  assert.equal(row("JawOpen").output.textContent, "0.777");

  // Unfreeze resumes and immediately updates; switching to connection tab or hiding doc pauses again.
  el("expr-freeze").emit("click");
  await app.pollExpressions();
  assert.equal(row("JawOpen").output.textContent, "0.111");

  el("diag-tab-connection").emit("click");
  const connCalls = server.calls.filter(c => c.path === "/api/expressions").length;
  await app.pollExpressions();
  assert.equal(server.calls.filter(c => c.path === "/api/expressions").length, connCalls);

  el("diag-tab-expressions").emit("click");
  doc.hidden = true;
  const hiddenCalls = server.calls.filter(c => c.path === "/api/expressions").length;
  await app.pollExpressions();
  assert.equal(server.calls.filter(c => c.path === "/api/expressions").length, hiddenCalls);
});

test("expression diagnostics shows unselected native and model readouts side-by-side and toggles cleanly", async t => {
  const FULL_50_CHANNELS = DIAGNOSTIC_GROUPS.flatMap(g => g.items.map(i => i.name));
  const {app, server, el, row} = setup(t);
  server.status.contract.channels = FULL_50_CHANNELS;
  server.status.model.trained_channels = FULL_50_CHANNELS;
  await app.init();
  app.morphControls.get("MouthSmileL").modelBtn.emit("click");
  app.morphControls.get("TongueUp").offBtn.emit("click");
  await app.save();
  await app.start();

  const allBits = (1n << 50n) - 1n;
  const tongueUpIdx = FULL_50_CHANNELS.indexOf("TongueUp");
  Object.assign(server.status.prediction, {
    valid_mask: (allBits & ~(1n << BigInt(tongueUpIdx))).toString(),
    model_valid_mask: allBits.toString(),
    native_channels: ["JawOpen"],
    channels: {JawOpen: 0.72, MouthSmileL: 0.44, CheekPuffLeft: 0.31},
    stock_channels: {JawOpen: 0.72, MouthSmileL: 0.19, CheekPuffLeft: 0.25},
    model_channels: {JawOpen: 0.58, MouthSmileL: 0.44, CheekPuffLeft: 0.31, TongueUp: 0.63},
  });
  await app.pollStatus();

  // JawOpen routed to native: shows native=0.720 (selected) AND unselected model=0.580
  const jawCard = row("JawOpen");
  assert.equal(jawCard.compare.wrap.hidden, false);
  assert.equal(jawCard.compare.wrap.attributes["data-single"], "false");
  assert.equal(jawCard.compare.native.item.hidden, false);
  assert.equal(jawCard.compare.native.labelEl.textContent, messages.en.exprCompareNative);
  assert.equal(jawCard.compare.native.valEl.textContent, "0.720");
  assert.equal(jawCard.compare.native.item.attributes["data-selected"], "true");
  assert.equal(jawCard.compare.model.valEl.textContent, "0.580");
  assert.equal(jawCard.compare.model.item.attributes["data-selected"], "false");

  // CheekPuffLeft (native merged parent): native slot explicitly labeled Native (merged)
  const puffCell = app.rows.get("channel-table/CheekPuffLeft");
  assert.equal(puffCell.compare.native.item.hidden, false);
  assert.equal(puffCell.compare.native.labelEl.textContent, messages.en.exprCompareNativeMerged);
  assert.equal(puffCell.compare.native.valEl.textContent, "0.250");
  assert.equal(puffCell.compare.model.valEl.textContent, "0.310");

  // MouthSmileL routed to model: shows unselected native=0.190 AND model=0.440 (selected)
  const smileCell = app.rows.get("channel-table/MouthSmileL");
  assert.equal(smileCell.compare.native.valEl.textContent, "0.190");
  assert.equal(smileCell.compare.native.item.attributes["data-selected"], "false");
  assert.equal(smileCell.compare.model.valEl.textContent, "0.440");
  assert.equal(smileCell.compare.model.item.attributes["data-selected"], "true");

  // TongueUp (pure extended, unsupported by headset native): native column hidden, model comparison still shows 0.630
  const upCell = app.rows.get("channel-table/TongueUp");
  assert.equal(upCell.output.textContent, "—");
  assert.equal(upCell.compare.wrap.attributes["data-single"], "true");
  assert.equal(upCell.compare.native.item.hidden, true);
  assert.equal(upCell.compare.model.valEl.textContent, "0.630");

  // Toggle dual-source comparison off/on
  el("expr-compare-toggle").emit("click");
  assert.equal(app.state.exprCompare, false);
  assert.equal(jawCard.compare.wrap.hidden, true);
  assert.equal(smileCell.compare.wrap.hidden, true);
  el("expr-compare-toggle").emit("click");
  assert.equal(app.state.exprCompare, true);
  assert.equal(jawCard.compare.wrap.hidden, false);
});

test("Windows socket errors, previewError reset, relay error isolation, and pollExpressions signal", async t => {
  const f = setup(t);
  await f.app.init();

  assert.equal(errorMessage("zh-CN", "[WinError 10061] Connection refused"), messages["zh-CN"].connectionRefused);
  assert.equal(errorMessage("zh-CN", "[WinError 10060] timed out"), messages["zh-CN"].connectionTimeout);
  assert.equal(errorMessage("zh-CN", "[WinError 10054] Connection reset"), messages["zh-CN"].cameraStreamClosed);

  await f.app.start("capture");
  f.server.previewStatus = 500;
  await f.app.refreshPreview();
  assert.equal(f.app.state.previewError, true);
  await f.app.stop();
  assert.equal(f.app.state.previewError, false);

  // Cymple relay errors do not duplicate into top #error.
  f.server.before = (path) => {
    if (path === "/api/cymple-relay") return Response.json({error: "No PICO camera service found"}, {status: 400});
  };
  await f.el("cymple-relay-start").emit("click");
  assert.equal(f.el("cymple-relay-error").hidden, false);
  assert.equal(f.el("error").hidden, true);
  f.server.before = null;

  // pollExpressions passes an AbortSignal so hung fetches time out cleanly.
  await f.app.start();
  f.app.navigate("diagnostics");
  await f.app.pollExpressions();
  const exprCall = f.server.calls.findLast(c => c.path === "/api/expressions");
  assert.ok(exprCall?.signal instanceof AbortSignal);
});
