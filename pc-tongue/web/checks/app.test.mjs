import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createApp, deriveView, requestJson} from "../app.mjs";
import {errorMessage, messages, initialLanguage, preferredLanguage, LANGUAGE_KEY} from "../i18n.mjs";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
class Element {
  value = ""; textContent = ""; hidden = false; disabled = false; attributes = {}; listeners = {};
  checked = false; open = false; valid = true;
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; if (key === "src") this.src = ""; }
  checkValidity() { return this.valid; }
  emit(type) { return this.listeners[type]?.({preventDefault() {}}); }
}
function setup(t, options = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, new Element()]));
  elements.get("preview-enabled").checked = true;
  elements.get("voice-enabled").checked = true;
  const doc = {documentElement: {}, hidden: false, title: "", getElementById: id => {
    assert.ok(elements.has(id), id); return elements.get(id);
  }};
  const config = {headset_host: "headset.example", headset_port: 45681, model_path: "D:/models/shadow-final-v2.pt",
    device: "auto", smoothing_ms: 20, uncertain_hold_ms: 100, stale_ms: 350, preview_fps: 10,
    replay_path: "D:/data/reference.npz",
    enabled: false, output_enabled: false, output_host: "127.0.0.1", output_port: 45682, size_hysteresis: true};
  const server = {config, calls: [], failStatus: false, failStart: false, before: null, previewStatus: 200,
    status: {running: false, connected: false, config, source: {session_id: "18446744073709551000"},
      prediction: {fresh: false, override: false, state: "stopped", out: 1, left: .2, right: 0},
      metrics: {source_fresh: false}, personalization: {state: "idle", profile: {
        head: null, calibration: {left: .33, center: .66, right: 1.11, min_area: 100}}}}};
  const fetcher = async (path, opts = {}) => {
    server.calls.push({path, ...opts});
    const custom = await server.before?.(path, opts);
    if (custom) return custom;
    if (path === "/api/status") {
      if (server.failStatus) throw new TypeError("Failed to fetch");
      return Response.json(server.status);
    }
    if (path === "/api/config") {
      if (opts.method === "POST") {
        const changes = JSON.parse(opts.body);
        if (server.status.running && ["headset_host", "headset_port", "model_path", "device",
          "output_enabled", "output_host", "output_port"].some(key => changes[key] !== undefined && changes[key] !== config[key])) {
          Object.assign(server.status, {running: false, connected: false});
          config.enabled = false;
        }
        Object.assign(config, changes);
      }
      return Response.json(config);
    }
    if (path === "/api/start") {
      if (server.failStart) return Response.json({error: "Model file not found"}, {status: 400});
      const {mode} = JSON.parse(opts.body);
      Object.assign(server.status, {running: true, connected: mode === "live", source_mode: mode});
      config.enabled = true;
      server.status.replay = {active: mode === "replay", path: mode === "replay" ? config.replay_path : null,
        position: 0, total: mode === "replay" ? 2700 : null, finished: false};
      Object.assign(server.status.prediction, {fresh: true, override: true, state: "present"});
      server.status.metrics.source_fresh = true;
      return Response.json(server.status);
    }
    if (path === "/api/stop") {
      Object.assign(server.status, {running: false, connected: false});
      config.enabled = false;
      Object.assign(server.status.prediction, {fresh: false, override: false, state: "stopped"});
      return Response.json(server.status);
    }
    if (path === "/api/personalization") {
      const body = JSON.parse(opts.body), personal = server.status.personalization;
      if (body.action === "calibrate" || body.action === "retrain") {
        personal.state = "collecting"; personal.job_id = "test-job";
        personal.wizard = {step: "fit", hint: "fit", collected: 0, target: 20, progress: 0};
      } else { personal.state = body.action === "restore" ? "restored" : "cancelled"; personal.wizard = null; }
      return Response.json(personal);
    }
    if (path.startsWith("/api/preview.jpg")) return server.previewStatus === 204 ? new Response(null, {status: 204}) :
      new Response(new Uint8Array([255, 216, 255, 217]), {status: server.previewStatus});
    throw new Error(path);
  };
  const timerCalls = [];
  const timers = {setInterval: (fn, ms) => { timerCalls.push({fn, ms}); return timerCalls.length; },
    setTimeout: (fn, ms) => { timerCalls.push({fn, ms}); return timerCalls.length; }, clearInterval() {}, clearTimeout() {}};
  const urls = {created: [], revoked: [], createObjectURL() {
    const url = `blob:test-${this.created.length}`; this.created.push(url); return url;
  }, revokeObjectURL(url) { this.revoked.push(url); }};
  const saved = new Map();
  const storage = {getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value)};
  let time = 10000;
  const app = createApp(doc, {fetch: fetcher, timers, urlApi: urls, storage, languages: ["en-US"], now: () => time, ...options});
  t.after(() => app.destroy());
  return {app, server, doc, el: id => doc.getElementById(id), saved, urls, timers: timerCalls, advance: ms => time += ms};
}

test("advanced settings and diagnostics retain their controls and start collapsed; no mouth-source selector", () => {
  const advanced = html.match(/<details\b[^>]*id="advanced"[^>]*>([\s\S]*?)<\/details>/);
  const diagnostics = html.match(/<details\b[^>]*id="diagnostics"[^>]*>([\s\S]*?)<\/details>/);
  assert.ok(advanced); assert.ok(diagnostics);
  for (const opening of html.matchAll(/<details\b[^>]*>/g)) assert.doesNotMatch(opening[0], /\bopen\b/);
  for (const id of ["headset_port", "device", "smoothing_ms", "uncertain_hold_ms", "stale_ms", "preview_fps",
    "output_enabled", "output_port", "save", "replay"]) assert.ok(advanced[1].includes(`id="${id}"`), id);
  for (const id of ["source-info", "result-info", "rates", "queue", "latency"])
    assert.ok(diagnostics[1].includes(`id="${id}"`), id);
  assert.doesNotMatch(html, /face_source|Cymple/);
  const main = html.replace(advanced[0], "").replace(diagnostics[0], "");
  for (const id of ["headset_host", "model_path", "start", "stop", "preview-enabled", "preview", "out", "left", "right"])
    assert.ok(main.includes(`id="${id}"`), id);
});
test("browser language chooses Chinese only for a Chinese first preference", () => {
  assert.equal(preferredLanguage(["zh-Hans-CN", "en"]), "zh-CN");
  assert.equal(preferredLanguage(["en-US", "zh-CN"]), "en");
  assert.equal(preferredLanguage(["de-DE"]), "en");
  assert.equal(preferredLanguage([]), "en");
});
test("manual preference persists and storage failures do not break language selection", () => {
  assert.equal(initialLanguage({getItem: () => "en"}, ["zh-CN"]), "en");
  assert.equal(initialLanguage({getItem: () => "obsolete"}, ["zh-CN"]), "zh-CN");
  assert.equal(initialLanguage({getItem() { throw Error(); }}, ["en"]), "en");
});
test("both language dictionaries cover the same complete set of strings", () => {
  assert.deepEqual(Object.keys(messages.en).sort(), Object.keys(messages["zh-CN"]).sort());
  for (const value of Object.values(messages.en)) assert.ok(value && !/[\p{Script=Han}]/u.test(value), value);
});
test("known backend failures have bilingual explanations while retaining raw diagnostics", () => {
  const raw = "POST /api/start · HTTP 400: CUDA was requested but is not available";
  assert.equal(errorMessage("en", raw), `Request failed: ${messages.en.cudaUnavailable} · ${raw}`);
  assert.equal(errorMessage("zh-CN", raw), `${messages["zh-CN"].requestError}: ${messages["zh-CN"].cudaUnavailable} · ${raw}`);
  for (const language of ["en", "zh-CN"]) {
    const message = errorMessage(language, "GET /api/status: request timed out (3500 ms)");
    assert.ok(message.includes(messages[language].requestTimeout));
    assert.ok(message.includes("request timed out (3500 ms)"));
    assert.ok(errorMessage(language, "Unknown error <script>").endsWith("Unknown error <script>"));
  }
});
test("opening the page reads config/status without starting or sending results", async t => {
  const {app, server, el, doc, timers} = setup(t);
  await app.init();
  assert.ok(server.calls.every(c => c.method === "GET"));
  assert.equal(server.config.output_enabled, false);
  assert.equal(el("headset_host").value, server.config.headset_host);
  assert.equal(el("status").textContent, "Stopped");
  assert.equal(doc.documentElement.lang, "en");
  assert.equal(doc.title, "PICO Tongue");
  assert.ok(timers.some(c => c.ms === 500));
});
test("changing language preserves unsaved input and makes no backend requests", async t => {
  const {app, server, el, doc, saved} = setup(t);
  await app.init();
  el("model_path").value = "D:/unsaved.pt";
  el("device").value = "cpu";
  el("output_port").value = "45690";
  el("output_enabled").checked = true;
  el("advanced").open = true;
  el("settings").emit("input");
  const count = server.calls.length;
  el("language").value = "zh-CN";
  el("language").emit("change");
  assert.equal(server.calls.length, count);
  assert.equal(el("model_path").value, "D:/unsaved.pt");
  assert.equal(el("device").value, "cpu");
  assert.equal(el("output_port").value, "45690");
  assert.equal(el("output_enabled").checked, true);
  assert.equal(el("advanced").open, true);
  assert.equal(el("diagnostics").open, false);
  assert.equal(app.state.dirty, true);
  assert.equal(doc.documentElement.lang, "zh-CN");
  assert.equal(el("start").textContent, "启动");
  assert.equal(saved.get(LANGUAGE_KEY), "zh-CN");
  await app.pollStatus();
  assert.equal(el("model_path").value, "D:/unsaved.pt");
});
test("switching language when storage is blocked still works", async t => {
  const {app, el} = setup(t, {storage: {getItem() { throw Error(); }, setItem() { throw Error(); }}});
  await app.init();
  app.setLanguage("zh-CN");
  assert.equal(el("status").textContent, "已停止");
});
test("Start saves main and advanced settings, respects disabled UDP and never selects a mouth source", async t => {
  const {app, server, el} = setup(t);
  await app.init();
  el("headset_host").value = "headset.example";
  el("settings").emit("input");
  await el("settings").emit("submit");
  const config = JSON.parse(server.calls.find(c => c.path === "/api/config" && c.method === "POST").body);
  assert.deepEqual(config, {headset_host: "headset.example", model_path: server.config.model_path,
    headset_port: 45681, device: "auto", smoothing_ms: 20, uncertain_hold_ms: 100, stale_ms: 350,
    preview_fps: 10, output_enabled: false, output_host: "127.0.0.1", output_port: 45682, size_hysteresis: true});
  assert.deepEqual(JSON.parse(server.calls.find(c => c.path === "/api/start").body), {mode: "live"});
  assert.equal(el("status").textContent, "Running");
  assert.equal(el("out").textContent, "1.000");
  assert.equal(el("headset_host").disabled, false);
  assert.equal(server.config.output_enabled, false);
});
test("Stop immediately clears outputs and preview, and releases the object URL", async t => {
  const {app, el, urls} = setup(t);
  await app.init(); await app.start(); await app.refreshPreview();
  assert.equal(el("preview").hidden, false);
  await app.stop();
  assert.equal(el("out").textContent, "—");
  assert.equal(el("preview").hidden, true);
  assert.deepEqual(urls.revoked, urls.created);
  assert.equal(el("status").textContent, "Stopped");
});
test("PC does not infer ownership from a legacy face_source field", async t => {
  const {app, server, el} = setup(t);
  server.config.face_source = "cymple";
  await app.init(); await app.start();
  assert.equal(el("out").textContent, "1.000");
  assert.ok(server.calls.some(c => c.path === "/api/start"));
});
test("missing fields and backend errors are readable and translate on language change", async t => {
  const {app, server, el} = setup(t);
  await app.init(); el("model_path").value = "";
  await app.start();
  assert.equal(el("error").textContent, messages.en.required);
  assert.ok(server.calls.every(c => c.method === "GET"));
  app.setLanguage("zh-CN");
  assert.equal(el("error").textContent, messages["zh-CN"].required);
  el("model_path").value = "D:/missing.pt"; server.failStart = true;
  await app.start();
  assert.match(el("error").textContent, /HTTP 400.*Model file not found/);
  await app.pollStatus();
  assert.match(el("error").textContent, /Model file not found/);
  app.setLanguage("en");
  assert.match(el("error").textContent, /^Request failed:/);
});
test("offline status invalidates old output and reconnecting does not auto-start", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  server.failStatus = true; await app.pollStatus();
  assert.equal(el("out").textContent, "—");
  assert.equal(el("status").textContent, "Local service unavailable");
  const starts = server.calls.filter(c => c.path === "/api/start").length;
  server.failStatus = false; await app.pollStatus();
  assert.equal(server.calls.filter(c => c.path === "/api/start").length, starts);
});
test("initial failure recovers by GET only without overwriting edited config", async t => {
  const {app, server, el} = setup(t);
  server.failStatus = true; await app.init();
  assert.equal(el("start").disabled, true);
  server.failStatus = false; await app.pollStatus();
  assert.equal(el("start").disabled, false);
  assert.ok(server.calls.every(c => c.method === "GET"));
});
test("a successful Save clears an earlier polling error immediately", async t => {
  const {app, server, el} = setup(t);
  await app.init();
  server.failStatus = true; await app.pollStatus();
  assert.equal(el("error").hidden, false);
  server.failStatus = false;
  el("settings").emit("input");
  await app.save();
  assert.equal(el("error").hidden, true);
  assert.equal(el("status").textContent, "Stopped");
});
test("expired snapshots and explicitly stale predictions never remain valid", async t => {
  const {app, server, advance} = setup(t);
  await app.init(); await app.start();
  assert.equal(deriveView(server.status, true, 1500).valid, false);
  server.status.prediction.stale = true;
  assert.equal(deriveView(server.status, true).valid, false);
  advance(1600);
  await app.refreshPreview();
});
test("late preview response cannot resurrect an image after Stop", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  let resolve;
  server.before = path => path.startsWith("/api/preview.jpg") ? new Promise(done => { resolve = done; }) : null;
  const pending = app.refreshPreview();
  await app.stop();
  resolve(new Response(new Uint8Array([1]))); await pending;
  assert.equal(el("preview").hidden, true);
});
test("new session cancels old pending preview and updates exact source diagnostics", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  let resolve;
  server.before = path => path.startsWith("/api/preview.jpg") ? new Promise(done => { resolve = done; }) : null;
  const pending = app.refreshPreview();
  server.status.source.session_id = "18446744073709551001";
  await app.pollStatus();
  resolve(new Response(new Uint8Array([1]))); await pending;
  assert.equal(el("preview").hidden, true);
  assert.match(el("source-info").textContent, /^18446744073709551001 \/ /);
});

test("advanced values read back, numeric zero is preserved and Save never starts inference", async t => {
  const {app, server, el} = setup(t);
  await app.init();
  assert.equal(el("headset_port").value, "45681");
  assert.equal(el("device").value, "auto");
  el("smoothing_ms").value = "0";
  el("uncertain_hold_ms").value = "0";
  el("stale_ms").value = "275";
  el("preview_fps").value = "5";
  el("output_enabled").checked = true;
  el("output_port").value = "45690";
  el("device").value = "cpu";
  el("settings").emit("change");
  assert.equal(el("config-state").textContent, "Unsaved changes");
  await el("save").emit("click");
  assert.equal(server.config.smoothing_ms, 0);
  assert.equal(server.config.uncertain_hold_ms, 0);
  assert.equal(server.config.stale_ms, 275);
  assert.equal(server.config.output_port, 45690);
  assert.equal(server.config.output_enabled, true);
  assert.equal(el("device").value, "cpu");
  assert.equal(el("config-state").textContent, "Saved");
  assert.equal(el("save").disabled, true);
  assert.equal(server.status.running, false);
  assert.ok(!server.calls.some(c => c.path === "/api/start"));
  await app.pollStatus();
  assert.equal(el("smoothing_ms").value, "0");
  assert.equal(el("output_enabled").checked, true);
});
test("Start retains enabled UDP and the selected port", async t => {
  const {app, server, el} = setup(t);
  server.config.output_enabled = true;
  server.config.output_port = 45691;
  await app.init(); await app.start();
  assert.equal(server.config.output_enabled, true);
  assert.equal(server.config.output_port, 45691);
  assert.equal(el("output_enabled").checked, true);
});
test("saving connection/device/output changes reads back the backend stop without restarting", async t => {
  const {app, server, el, urls} = setup(t);
  await app.init(); await app.start(); await app.refreshPreview();
  el("output_port").value = "45692";
  el("settings").emit("input");
  await app.save();
  assert.equal(app.state.status.running, false);
  assert.equal(app.state.config.enabled, false);
  assert.equal(el("status").textContent, "Stopped");
  assert.equal(el("out").textContent, "—");
  assert.equal(el("preview").hidden, true);
  assert.deepEqual(urls.revoked, urls.created);
  assert.equal(server.calls.filter(c => c.path === "/api/start").length, 1);
  assert.equal(el("start").disabled, false);
});
test("saving smoothing while running leaves the run active", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  el("smoothing_ms").value = "30";
  el("settings").emit("input");
  await app.save();
  assert.equal(server.status.running, true);
  assert.equal(server.config.smoothing_ms, 30);
  assert.equal(server.calls.filter(c => c.path === "/api/start").length, 1);
});
test("replay uses the configured dataset without a headset and preserves result sending preference", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await el("replay").emit("click");
  assert.deepEqual(JSON.parse(server.calls.find(c => c.path === "/api/start").body), {mode: "replay"});
  assert.equal(server.status.connected, false);
  assert.equal(el("status").textContent, "Replaying data");
  assert.equal(el("out").textContent, "1.000");
  assert.equal(server.config.output_enabled, false);
  assert.match(el("replay-info").textContent, /D:\/data\/reference.npz.*0 \/ 2700 frames/);
  assert.equal(el("encode-label").textContent, "PC replay JPEG encoding");
  server.status.replay.finished = true;
  server.status.replay.position = 2700;
  await app.pollStatus();
  assert.match(el("replay-info").textContent, /2700 \/ 2700 frames · Finished/);
  app.setLanguage("zh-CN");
  assert.equal(el("status").textContent, messages["zh-CN"].replaying);
  await app.stop();
  assert.equal(el("out").textContent, "—");
});
test("invalid advanced settings do not send POST or open collapsed details", async t => {
  const {app, server, el} = setup(t);
  await app.init();
  el("settings").valid = false;
  el("settings").emit("input");
  await app.start(); await app.save();
  assert.equal(el("error").textContent, messages.en.invalidSettings);
  assert.equal(el("advanced").open, false);
  assert.ok(server.calls.every(c => c.method === "GET"));
});
test("preview toggle clears the image, cancels late data and does not submit config or stop inference", async t => {
  const {app, server, el, urls} = setup(t);
  await app.init(); await app.start(); await app.refreshPreview();
  let resolve;
  server.before = path => path.startsWith("/api/preview.jpg") ? new Promise(done => { resolve = done; }) : null;
  const pending = app.refreshPreview();
  const count = server.calls.length;
  el("preview-enabled").checked = false;
  el("preview-enabled").emit("change");
  await app.refreshPreview();
  assert.equal(server.calls.length, count);
  resolve(new Response(new Uint8Array([1]))); await pending;
  assert.equal(el("preview").hidden, true);
  assert.deepEqual(urls.revoked, urls.created);
  assert.equal(el("placeholder").textContent, "Preview off");
  assert.equal(el("out").textContent, "1.000");
  assert.equal(server.status.running, true);
  assert.equal(app.state.dirty, false);
  server.before = null;
  el("preview-enabled").checked = true;
  el("preview-enabled").emit("change");
  await app.refreshPreview();
  assert.equal(el("preview").hidden, false);
});
test("preview scheduling uses the saved frame rate", async t => {
  const {app, server, timers} = setup(t);
  server.config.preview_fps = 4;
  await app.init();
  assert.ok(timers.some(c => c.ms === 250));
});
test("diagnostics preserve uint64 strings and subtract nanoseconds before conversion", async t => {
  const {app, server, el} = setup(t);
  Object.assign(server.status, {engine: "ONNX Runtime", device: "CUDA",
    prediction_source: {session_id: "18446744073709551000", source_seq: "9007199254740993"}});
  Object.assign(server.status.source, {source_seq: "9007199254740994", raw_identity: "18446744073709551615",
    sensor_ns: "9007199254740999", vendor_ns: "9007199254741000", eye0_sensor_ns: "9007199254740998",
    eye1_sensor_ns: "9007199254741001", mouth_sensor_ns: "9007199254740999",
    encode_begin_ns: "18446744073700000000", encode_end_ns: "18446744073701250000"});
  Object.assign(server.status.metrics, {source_age_ms: 10.2, receive_age_ms: 1.3, received_hz: 89.9,
    inference_hz: 89.8, source_gaps: 2, pending_dropped: 1, duplicates: 0, out_of_order: 0,
    queue_depth: 1, queue_ms: .2, pc_total_ms: 1.8, clock_uncertainty_ms: .4,
    latency_ms: {decode_ms: {p50: .2, p95: .4}, predict_ms: {p50: 1.2, p95: 2},
      source_copy_to_result_ms: {p50: 10.8, p95: 13.5}}});
  await app.init();
  assert.equal(el("source-info").textContent, "18446744073709551000 / 9007199254740994");
  assert.equal(el("result-info").textContent, "18446744073709551000 / 9007199254740993");
  assert.equal(el("raw-identity").textContent, "18446744073709551615");
  assert.equal(el("time-eye0").textContent, "9007199254740998");
  assert.equal(el("time-eye1").textContent, "9007199254741001");
  assert.equal(el("time-mouth").textContent, "9007199254740999");
  assert.equal(el("encode-time").textContent, "1.3 ms");
  assert.equal(el("rates").textContent, "89.9 / 89.8 Hz");
  assert.equal(el("queue").textContent, "1 / 0.2 ms");
  assert.equal(el("latency").textContent, "10.8 / 13.5 ms");
  assert.equal(el("engine-info").textContent, "ONNX Runtime / CUDA");
});
test("missing diagnostics show dashes, unsafe numeric identities are not presented as exact", async t => {
  const {app, server, el} = setup(t);
  server.status.source = {session_id: Number.MAX_SAFE_INTEGER + 2};
  await app.init();
  assert.equal(el("source-info").textContent, `${messages.en.unsafeInteger} / —`);
  assert.equal(el("queue").textContent, "— / — ms");
  assert.equal(el("latency").textContent, "—");
  assert.equal(el("encode-time").textContent, "— ms");
  app.setLanguage("zh-CN");
  assert.match(el("source-info").textContent, new RegExp(messages["zh-CN"].unsafeInteger));
});
test("expired diagnostics clear live metrics but retain the last exact identities", async t => {
  const {app, server, el, advance} = setup(t);
  server.status.metrics.received_hz = 90;
  await app.init(); await app.start();
  advance(1500);
  app.setLanguage("en");
  assert.equal(el("rates").textContent, "— / — Hz");
  assert.equal(el("snapshot-state").textContent, "Status snapshot expired");
  assert.match(el("source-info").textContent, /^18446744073709551000/);
  assert.equal(el("out").textContent, "—");
});
test("all restored labels, hints and output bars switch language without changing values or details", async t => {
  const {app, server, el} = setup(t);
  await app.init();
  const values = app.readForm();
  el("diagnostics").open = true;
  const count = server.calls.length;
  for (const language of ["zh-CN", "en"]) {
    app.setLanguage(language);
    for (const [id, key] of [["advanced-title", "advanced"], ["diagnostics-title", "diagnostics"],
      ["settings-help", "settingsHelp"], ["save", "save"], ["replay", "replay"], ["udp-label", "udp"],
      ["device-label", "device"], ["queue-label", "queue"], ["latency-label", "latency"]])
      assert.equal(el(id).textContent, messages[language][key]);
    assert.equal(el("bar-out").attributes["aria-label"], messages[language].out);
    assert.deepEqual(app.readForm(), values);
    assert.equal(el("diagnostics").open, true);
    assert.equal(el("advanced").open, false);
  }
  assert.equal(server.calls.length, count);
});
test("diagnostics show actual providers and the fallback reason instead of claiming CUDA", async t => {
  const {app, server, el} = setup(t);
  Object.assign(server.status, {engine: "onnx", device: "dml", model: {
    providers: ["DmlExecutionProvider", "CPUExecutionProvider"],
    fallbacks: [{engine: "onnxgraph", device: "cuda", error: "CUDA initialization failed"}]
  }});
  await app.init();
  assert.equal(el("engine-info").textContent, "onnx / dml");
  assert.equal(el("provider-info").textContent, "DmlExecutionProvider → CPUExecutionProvider");
  assert.equal(el("fallback-info").textContent, "onnxgraph/cuda: CUDA initialization failed");
  app.setLanguage("zh-CN");
  assert.equal(el("providers-label").textContent, messages["zh-CN"].providers);
  assert.equal(el("fallback-label").textContent, messages["zh-CN"].fallback);
  server.status.model.fallbacks = [];
  await app.pollStatus();
  assert.equal(el("fallback-info").textContent, messages["zh-CN"].noFallback);
});
test("slow Save serializes config/start/stop operations and preserves unsaved values on failure", async t => {
  const {app, server, el} = setup(t);
  await app.init();
  el("device").value = "cpu";
  el("settings").emit("input");
  let resolve;
  server.before = (path, opts) => path === "/api/config" && opts.method === "POST" ?
    new Promise(done => { resolve = done; }) : null;
  const pending = app.save();
  assert.equal(el("device").disabled, true);
  const count = server.calls.length;
  await app.save(); await app.start(); await app.stop(); await app.pollStatus();
  assert.equal(server.calls.length, count);
  resolve(Response.json({error: "save failed"}, {status: 400})); await pending;
  assert.equal(el("device").value, "cpu");
  assert.equal(app.state.dirty, true);
  assert.equal(el("device").disabled, false);
  assert.match(el("error").textContent, /save failed/);
});
test("a poll started before Save cannot restore an obsolete running snapshot", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  const old = structuredClone(server.status);
  let resolve;
  server.before = path => path === "/api/status" ? new Promise(done => { resolve = done; }) : null;
  const pending = app.pollStatus();
  server.before = null;
  el("device").value = "cpu";
  el("settings").emit("input");
  await app.save();
  resolve(Response.json(old)); await pending;
  assert.equal(el("status").textContent, "Stopped");
  assert.equal(app.state.status.running, false);
});
test("preview 204, HTTP error and image decode failure are handled without affecting inference", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  server.previewStatus = 204; await app.refreshPreview();
  assert.equal(el("preview").hidden, true);
  server.previewStatus = 500; await app.refreshPreview();
  assert.equal(el("placeholder").textContent, messages.en.previewError);
  assert.equal(server.status.running, true);
  el("preview").emit("error");
  app.setLanguage("zh-CN");
  assert.equal(el("placeholder").textContent, messages["zh-CN"].previewError);
});
test("one preview request at a time; hidden pages do not request preview", async t => {
  const {app, server, doc} = setup(t);
  await app.init(); await app.start();
  let resolve;
  server.before = path => path.startsWith("/api/preview.jpg") ? new Promise(done => { resolve = done; }) : null;
  const pending = app.refreshPreview();
  await app.refreshPreview();
  assert.equal(server.calls.filter(c => c.path.startsWith("/api/preview.jpg")).length, 1);
  resolve(new Response(null, {status: 204})); await pending;
  doc.hidden = true; await app.refreshPreview();
  assert.equal(server.calls.filter(c => c.path.startsWith("/api/preview.jpg")).length, 1);
});
test("HTTP diagnostics retain route, status and raw error", async () => {
  await assert.rejects(requestJson(async () => Response.json({error: "model missing"}, {status: 422}), "/api/start", {}), /POST \/api\/start.*HTTP 422.*model missing/);
  await assert.rejects(requestJson(async () => new Response("not json"), "/api/status"), /GET \/api\/status.*HTTP 200.*not json/);
});

test("personalization stays folded; opening or translating never records or speaks", async t => {
  const speech = {calls: [], cancel() {}, speak(u) { this.calls.push(u); }};
  const {app, el, server} = setup(t, {speech, Utterance: class { constructor(text) { this.text = text; } }});
  await app.init();
  el("personalization").open = true;
  el("record-consent").checked = true;
  app.setLanguage("zh-CN");
  assert.equal(speech.calls.length, 0);
  assert.ok(server.calls.every(c => c.method === "GET"));
  assert.equal(el("personalization").open, true);
  assert.equal(el("record-consent").checked, true);
  assert.equal(el("calibrate").disabled, true);
  assert.equal(el("personal-title").textContent, messages["zh-CN"].personalTitle);
});
test("calibration requires saved fresh live video and does not save settings or start input", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  const count = server.calls.length;
  await app.personalize("calibrate");
  assert.deepEqual(JSON.parse(server.calls[count].body), {action: "calibrate"});
  assert.equal(server.calls[count].path, "/api/personalization");
  assert.equal(el("guide").hidden, false);
  assert.equal(el("calibrate").disabled, true);
  assert.equal(el("personal-cancel").disabled, false);
  await app.personalize("cancel");
  assert.equal(el("guide").hidden, true);
  el("settings").emit("input");
  assert.equal(el("calibrate").disabled, true);
  await app.save();
  server.status.source_mode = "replay"; await app.pollStatus();
  assert.equal(el("calibrate").disabled, true);
});
test("retraining requires a separate consent checkbox and clears it after starting", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  await app.personalize("retrain");
  assert.ok(!server.calls.some(c => c.path === "/api/personalization"));
  el("record-consent").checked = true; el("record-consent").emit("change");
  await app.personalize("retrain");
  assert.deepEqual(JSON.parse(server.calls.find(c => c.path === "/api/personalization").body), {action: "retrain", confirmed: true});
  assert.equal(el("record-consent").checked, false);
  assert.equal(server.config.output_enabled, false);
});
test("speech announces each step once, can be disabled and switches languages without posting", async t => {
  const speech = {calls: [], cancelled: 0, cancel() { this.cancelled++; }, speak(u) { this.calls.push(u); }};
  const {app, el, server} = setup(t, {speech, Utterance: class { constructor(text) { this.text = text; } }});
  await app.init(); await app.start(); await app.personalize("calibrate");
  assert.equal(speech.calls.length, 1);
  assert.equal(speech.calls[0].text, messages.en.step_fit);
  await app.pollStatus(); assert.equal(speech.calls.length, 1);
  server.status.personalization.wizard.step = "closed"; await app.pollStatus();
  assert.equal(speech.calls[1].text, messages.en.step_closed);
  const count = server.calls.length;
  app.setLanguage("zh-CN");
  assert.equal(server.calls.length, count);
  assert.equal(speech.calls[2].lang, "zh-CN");
  el("voice-enabled").checked = false; el("voice-enabled").emit("change");
  server.status.personalization.wizard.step = "open"; await app.pollStatus();
  assert.equal(speech.calls.length, 3);
  assert.equal(el("voice-info").textContent, messages["zh-CN"].voiceOff);
});
test("personal status, metrics, calibration and errors translate; stale snapshots hide the guide", async t => {
  const {app, server, el, advance} = setup(t);
  await app.init(); await app.start();
  server.status.personalization = {state: "rejected", profile: {head: "test-personal.pt",
    calibration: {left: 1.11, center: .66, right: .33, min_area: 120}},
    metrics: {before: {score: .5, fp: .01}, after: {score: .51, fp: .02}}, error: "Camera session changed during calibration"};
  await app.pollStatus();
  for (const lang of ["zh-CN", "en"]) {
    app.setLanguage(lang);
    assert.equal(el("personal-state").textContent, messages[lang].personal_rejected);
    assert.ok(el("profile-info").textContent.includes(messages[lang].personalProfile));
    assert.ok(el("personal-error").textContent.includes(messages[lang].cameraChanged));
    assert.match(el("training-metrics").textContent, /0.500 \/ 0.010 → 0.510 \/ 0.020/);
  }
  advance(1600); app.setLanguage("en");
  assert.equal(el("personal-state").textContent, messages.en.personalOffline);
  assert.equal(el("guide").hidden, true);
});
test("hysteresis saves as a boolean without changing live input or losing edits on translation", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  assert.equal(el("size_hysteresis").checked, true);
  el("size_hysteresis").checked = false; el("settings").emit("change");
  app.setLanguage("zh-CN");
  assert.equal(el("size_hysteresis").checked, false);
  await app.save();
  assert.equal(server.config.size_hysteresis, false);
  assert.equal(server.status.running, true);
});
test("restore changes the preview output epoch and late preview cannot reappear", async t => {
  const {app, server, el} = setup(t);
  await app.init(); await app.start();
  let resolve;
  server.before = path => path.startsWith("/api/preview.jpg") ? new Promise(done => { resolve = done; }) : null;
  const pending = app.refreshPreview();
  server.status.output_session_id = "new-profile-epoch";
  await app.personalize("restore");
  resolve(new Response(new Uint8Array([1]))); await pending;
  assert.equal(el("preview").hidden, true);
  assert.equal(server.status.personalization.state, "restored");
  assert.equal(server.calls.filter(c => c.path === "/api/start").length, 1);
});
