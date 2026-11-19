import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createApp, deriveView, requestJson, PAGES, routePage} from "../app.mjs";
import {messages, preferredLanguage, initialLanguage, errorMessage, LANGUAGE_KEY} from "../i18n.mjs";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const css = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

class Element {
  value = "";
  textContent = "";
  hidden = false;
  disabled = false;
  attributes = {};
  listeners = {};
  children = [];
  checked = false;
  open = false;
  valid = true;
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
  elements.get("preview-enabled").checked = true;
  elements.get("preview-camera").value = "mouth";
  elements.get("cymple-host").value = "127.0.0.1";
  elements.get("cymple-port").value = "23303";
  elements.get("cymple-left-port").value = "45682";
  elements.get("cymple-right-port").value = "45683";
  elements.get("cymple-left-rotate").value = "0";
  elements.get("cymple-right-rotate").value = "0";

  const doc = {
    documentElement: {},
    hidden: false,
    title: "",
    createElement: () => new Element(),
    getElementById: id => { assert.ok(elements.has(id), `Missing element #${id}`); return elements.get(id); },
  };
  const config = {
    enabled: false,
    headset_host: "headset.example",
    headset_port: 45681,
    eye_left_port: 45682,
    eye_right_port: 45683,
    preview_camera: "mouth",
    stale_ms: 1000,
    preview_fps: 10,
  };
  const server = {
    config,
    calls: [],
    before: null,
    failStatus: false,
    previewStatus: 200,
    updateResponse: {
      current_version: "2.6.0",
      latest_version: "2.6.0",
      update_available: false,
      release_url: "https://github.com/WolalaQAQ/PicoET-Enhance/releases",
      installer_url: "",
      portable_url: "",
      module_url: "",
      notes: [],
      error: null,
    },
    status: {
      version: "2.6.0",
      running: false,
      connected: false,
      capture_only: true,
      preview_camera: "mouth",
      config,
      source: {session_id: "18446744073709551000", source_seq: "9007199254740993"},
      output_session_id: "test-output",
      cymple_relay: {
        running: false,
        state: "idle",
        source_host: "",
        left_port: 45682,
        right_port: 45683,
        cymple_host: "127.0.0.1",
        cymple_port: 23303,
        error: "",
        config: {
          cymple_host: "127.0.0.1",
          cymple_port: 23303,
          left_port: 45682,
          right_port: 45683,
          left_rotate: 0,
          left_hflip: false,
          left_vflip: false,
          right_rotate: 0,
          right_hflip: false,
          right_vflip: false,
        },
        eyes: {
          eye_left: {channel: 0, port: 45682, state: "idle", connected: false, fps: 0, frames: 0, avg_kb: 0, error: null},
          eye_right: {channel: 1, port: 45683, state: "idle", connected: false, fps: 0, frames: 0, avg_kb: 0, error: null},
        },
      },
      metrics: {video_fresh: false, received_hz: 0, decoded_hz: 0},
    },
  };

  function active(camera = "mouth") {
    Object.assign(server.status, {running: true, connected: true, source_mode: "live", capture_only: true, preview_camera: camera});
    config.enabled = true;
    config.preview_camera = camera;
    server.status.metrics.video_fresh = true;
  }

  const fetcher = async (path, opts = {}) => {
    server.calls.push({path, ...opts});
    const custom = await server.before?.(path, opts);
    if (custom) return custom;
    if (path === "/api/discovery") return Response.json({devices: server.discovered ?? []});
    if (path.startsWith("/api/update-check")) return Response.json(server.updateResponse);
    if (path === "/api/status") {
      if (server.failStatus) throw TypeError("Failed to fetch");
      return Response.json(server.status);
    }
    if (path === "/api/config") {
      if (opts.method === "POST") {
        const changes = JSON.parse(opts.body);
        Object.assign(config, changes);
      }
      return Response.json(config);
    }
    if (path === "/api/start") {
      const {camera} = JSON.parse(opts.body);
      active(camera || config.preview_camera);
      return Response.json(server.status);
    }
    if (path === "/api/stop") {
      Object.assign(server.status, {running: false, connected: false});
      server.status.metrics.video_fresh = false;
      config.enabled = false;
      return Response.json(server.status);
    }
    if (path === "/api/shutdown") {
      return Response.json({stopping: true});
    }
    if (path === "/api/cymple-relay") {
      const req = JSON.parse(opts.body);
      if (req.action === "start") {
        const {action, headset_host, ...cfg} = req;
        Object.assign(server.status.cymple_relay.config, cfg);
        Object.assign(server.status.cymple_relay, {
          running: true,
          state: "running",
          source_host: headset_host || "192.0.2.10",
          left_port: cfg.left_port ?? 45682,
          right_port: cfg.right_port ?? 45683,
          cymple_host: cfg.cymple_host ?? "127.0.0.1",
          cymple_port: cfg.cymple_port ?? 23303,
          error: "",
        });
        server.status.cymple_relay.eyes = {
          eye_left: {channel: 0, port: cfg.left_port ?? 45682, state: "streaming", connected: true, fps: 90.0, frames: 45, avg_kb: 28.4, error: null},
          eye_right: {channel: 1, port: cfg.right_port ?? 45683, state: "streaming", connected: true, fps: 89.8, frames: 44, avg_kb: 27.9, error: null},
        };
      } else if (req.action === "stop") {
        Object.assign(server.status.cymple_relay, {running: false, state: "idle", source_host: ""});
        server.status.cymple_relay.eyes.eye_left.state = "idle";
        server.status.cymple_relay.eyes.eye_right.state = "idle";
      }
      return Response.json(server.status.cymple_relay);
    }
    if (path.startsWith("/api/preview.jpg")) {
      return server.previewStatus === 204
        ? new Response(null, {status: 204})
        : new Response(new Uint8Array([255, 216, 255, 217]), {status: server.previewStatus});
    }
    throw Error(path);
  };

  const timerCalls = [], timers = {
    setInterval: (fn, ms) => { timerCalls.push({fn, ms}); return timerCalls.length; },
    setTimeout: (fn, ms) => { timerCalls.push({fn, ms}); return timerCalls.length; },
    clearInterval() {},
    clearTimeout() {},
  };
  const urls = {
    created: [],
    revoked: [],
    createObjectURL() { const url = `blob:${this.created.length}`; this.created.push(url); return url; },
    revokeObjectURL(url) { this.revoked.push(url); },
  };
  const saved = new Map(), storage = {getItem: k => saved.get(k), setItem: (k, v) => saved.set(k, v)};
  let time = 10000;
  const app = createApp(doc, {fetch: fetcher, timers, urlApi: urls, storage, languages: ["en-US"], now: () => time, ...options});
  t.after(() => app.destroy());
  return {app, server, doc, el: id => doc.getElementById(id), saved, urls, timers: timerCalls, advance: ms => time += ms};
}

test("restored UI preserves the original green palette and excludes unfinished model/capture/training pages", () => {
  assert.match(css, /background:\s*#f4f6f5/);
  assert.match(css, /background:\s*#172e2c/);
  assert.match(css, /--accent:\s*#167d72/);
  assert.doesNotMatch(css, /#stop\s*\{\s*border-color:\s*transparent/);
  assert.match(css, /#stop,\s*#cymple-relay-stop\s*\{[^}]*border:\s*1\.5px\s+solid\s+#8ab0a4/);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ["cymple-relay", "diagnostics", "capture", "stop", "preview", "preview-camera", "camera-stream-url", "check-update", "quit-service", "update-banner"]) {
    assert.ok(html.includes(`id="${id}"`), `Missing #${id}`);
  }
  for (const field of ["headset_host", "headset_port", "eye_left_port", "eye_right_port", "preview_fps"]) {
    assert.match(html, new RegExp(`id="${field}" form="settings"`));
  }
  assert.doesNotMatch(html, /page-recording|page-training|model_path|face50|record-start|train-start/);
});

test("both languages are complete and browser defaults respect the first preference", () => {
  assert.deepEqual(Object.keys(messages.en).sort(), Object.keys(messages["zh-CN"]).sort());
  for (const text of Object.values(messages.en)) assert.ok(!/[\p{Script=Han}]/u.test(text), text);
  assert.equal(preferredLanguage(["en-US", "zh-CN"]), "en");
  assert.equal(preferredLanguage(["zh-Hans-CN"]), "zh-CN");
  assert.equal(initialLanguage({getItem: () => "en"}, ["zh-CN"]), "en");
});

test("navigation switches between Cameras & relay and Settings without starting streams", async t => {
  const {app, el, server} = setup(t);
  await app.init();
  const count = server.calls.length;
  el("nav-advanced").emit("click");
  assert.deepEqual(PAGES.filter(id => !el(`page-${id}`).hidden), ["preferences"]);
  assert.equal(el("connection-setup").hidden, true);
  assert.equal(el("nav-advanced").attributes["aria-current"], "page");
  el("nav-workspace").emit("click");
  assert.deepEqual(PAGES.filter(id => !el(`page-${id}`).hidden), ["tracking"]);
  assert.equal(el("connection-setup").hidden, false);
  assert.equal(el("nav-workspace").attributes["aria-current"], "page");
  assert.equal(server.calls.length, count);
  assert.equal(routePage("#/preferences"), "preferences");
  assert.equal(routePage("#unexpected"), "tracking");
});

test("discovery selects a 3-camera headset without filling manual address or dirtying settings", async t => {
  const {app, server, el} = setup(t);
  server.config.headset_host = "";
  server.discovered = [{
    host: "198.51.100.8",
    port: 45681,
    name: "PICO Cameras",
    streams: [
      {id: "mouth", port: 45681, path: "/stream.mjpg"},
      {id: "eye_left", port: 46101, path: "/stream.mjpg"},
      {id: "eye_right", port: 46102, path: "/stream.mjpg"},
    ],
  }];
  await app.init();
  assert.equal(el("headset_host").value, "");
  assert.equal(app.state.dirty, false);
  assert.equal(el("headset-select").value, "198.51.100.8:45681");
  assert.equal(el("cymple-left-port").value, "46101");
  assert.equal(el("cymple-right-port").value, "46102");
  assert.equal(el("camera-stream-url").textContent, "http://198.51.100.8:45681/stream.mjpg");

  el("preview-camera").value = "eye_left";
  await app.switchCamera();
  assert.equal(el("camera-stream-url").textContent, "http://198.51.100.8:46101/stream.mjpg");
  assert.ok(server.calls.every(c => c.method === "GET"));

  await app.start("capture");
  assert.equal(server.config.headset_host, "");
  assert.ok(!server.calls.some(c => c.path === "/api/config" && c.method === "POST"));
  assert.deepEqual(JSON.parse(server.calls.find(c => c.path === "/api/start").body), {
    mode: "capture",
    camera: "eye_left",
    headset_host: "198.51.100.8",
    headset_port: 45681,
    eye_left_port: 46101,
    eye_right_port: 46102,
  });

  await app.stop();
  el("headset_host").value = "192.0.2.55";
  el("page-preferences").emit("input");
  assert.equal(el("headset-select").value, "manual");
  assert.equal(app.state.dirty, true);
  await app.save();
  assert.equal(server.config.headset_host, "192.0.2.55");
});

test("camera preview starts, switches cameras while running, and stops cleanly", async t => {
  const {app, server, el} = setup(t);
  await app.init();
  await app.start("capture");
  await app.refreshPreview();
  assert.deepEqual(JSON.parse(server.calls.find(c => c.path === "/api/start").body), {
    mode: "capture",
    camera: "mouth",
    headset_host: "headset.example",
    headset_port: 45681,
    eye_left_port: 45682,
    eye_right_port: 45683,
  });
  assert.equal(el("preview").hidden, false);
  assert.equal(el("status").textContent, messages.en.recordingOnly);

  el("preview-camera").value = "eye_right";
  await app.switchCamera();
  assert.equal(el("camera-stream-url").textContent, "http://headset.example:45683/stream.mjpg");
  const startBodies = server.calls.filter(c => c.path === "/api/start").map(c => JSON.parse(c.body));
  assert.equal(startBodies.at(-1).camera, "eye_right");

  await app.stop();
  assert.equal(el("preview").hidden, true);
  assert.equal(el("status").textContent, messages.en.stopped);
});

test("Cymple eye relay starts folded and idle, translates labels, and works independently", async t => {
  const f = setup(t);
  await f.app.init();
  assert.ok(f.server.calls.every(c => c.method === "GET"));
  assert.equal(f.el("cymple-relay").hidden, false);
  assert.equal(f.el("cymple-relay").open, false);
  assert.equal(f.el("cymple-relay-options").open, false);
  assert.equal(f.el("cymple-relay-status").textContent, messages.en.cympleStateIdle);

  f.el("cymple-left-rotate").value = "90";
  f.el("cymple-left-rotate").emit("change");
  f.el("cymple-port").value = "23309";
  f.el("cymple-port").emit("input");
  assert.equal(f.app.state.relayDirty, true);
  assert.equal(f.app.state.dirty, false);

  await f.el("cymple-relay-start").emit("click");
  assert.equal(f.el("cymple-relay-status").textContent, messages.en.cympleStateRunning);
  assert.match(f.el("cymple-left-metrics").textContent, /90\.0 fps · 28\.4 KB\/f · 45 frames/);

  f.app.setLanguage("zh-CN");
  assert.equal(f.el("cymple-relay-status").textContent, messages["zh-CN"].cympleStateRunning);
  assert.equal(f.saved.get(LANGUAGE_KEY), "zh-CN");

  await f.el("cymple-relay-stop").emit("click");
  assert.equal(f.el("cymple-relay-status").textContent, messages["zh-CN"].cympleStateIdle);
});

test("update check stays silent when current, shows green banner on update, and quit shuts down", async t => {
  const f = setup(t);
  await f.app.init();
  await f.app.checkUpdates(false);
  assert.equal(f.el("update-banner").hidden, true);

  await f.el("check-update").emit("click");
  assert.equal(f.el("update-banner").hidden, false);
  assert.match(f.el("update-message").textContent, /latest version/i);

  f.server.updateResponse = {
    current_version: "2.6.0",
    latest_version: "2.7.0",
    update_available: true,
    release_url: "https://github.com/WolalaQAQ/PicoET-Enhance/releases/tag/v2.7.0",
    installer_url: "https://example.invalid/Setup.exe",
    portable_url: "https://example.invalid/studio.zip",
    module_url: "https://example.invalid/magisk.zip",
    notes: ["New feature"],
    error: null,
  };
  await f.app.checkUpdates(true);
  assert.equal(f.el("update-banner").hidden, false);
  assert.match(f.el("update-message").textContent, /v2\.7\.0/);
  assert.equal(f.el("update-links").children.length, 4);

  f.el("update-dismiss").emit("click");
  assert.equal(f.el("update-banner").hidden, true);

  await f.el("quit-service").emit("click");
  assert.ok(f.server.calls.some(c => c.path === "/api/shutdown"));
  assert.equal(f.el("status").textContent, messages.en.serviceStopped);
  assert.equal(f.el("quit-service").disabled, true);
});

test("connected-idle headset shows wake hint and timeout/refused errors map cleanly", async t => {
  const f = setup(t);
  await f.app.init();
  Object.assign(f.server.status, {running: true, connected: true, last_error: null});
  f.server.status.metrics.video_fresh = false;
  await f.app.pollStatus();
  assert.equal(f.el("placeholder").textContent, messages.en.waitingHeadsetWake);
  assert.equal(f.el("camera-empty-help").textContent, messages.en.cameraWakeHelp);
  assert.equal(f.el("error").hidden, true);

  f.app.setLanguage("zh-CN");
  assert.equal(errorMessage("zh-CN", "timed out"), messages["zh-CN"].connectionTimeout);
  assert.equal(errorMessage("zh-CN", "[WinError 10061] Connection refused"), messages["zh-CN"].connectionRefused);
  assert.equal(errorMessage("zh-CN", "POST /api/start: request timed out (3500 ms)"), messages["zh-CN"].requestTimeout);
});
