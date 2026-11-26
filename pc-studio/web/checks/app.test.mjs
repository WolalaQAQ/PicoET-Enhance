import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createApp, createTauriFetcher, deriveView, requestJson, PAGES, routePage} from "../app.mjs";
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

  const docListeners = new Map();
  const doc = {
    documentElement: {},
    hidden: false,
    title: "",
    createElement: () => new Element(),
    getElementById: id => { assert.ok(elements.has(id), `Missing element #${id}`); return elements.get(id); },
    addEventListener(type, fn) { docListeners.set(type, fn); },
    removeEventListener(type, fn) { if (docListeners.get(type) === fn) docListeners.delete(type); },
    emit(type) { return docListeners.get(type)?.(); },
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

  const timerCalls = [], clearedIntervals = [], clearedTimeouts = [], timers = {
    setInterval: (fn, ms) => { const id = timerCalls.length + 1; timerCalls.push({id, kind: "interval", fn, ms}); return id; },
    setTimeout: (fn, ms) => { const id = timerCalls.length + 1; timerCalls.push({id, kind: "timeout", fn, ms}); return id; },
    clearInterval(id) { clearedIntervals.push(id); },
    clearTimeout(id) { clearedTimeouts.push(id); },
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
  return {app, server, doc, el: id => doc.getElementById(id), saved, urls, timers: timerCalls, clearedIntervals, clearedTimeouts, advance: ms => time += ms};
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
  assert.equal(errorMessage("zh-CN", "[WinError 10053] Connection aborted"), messages["zh-CN"].cameraStreamClosed);
  assert.equal(errorMessage("zh-CN", "[WinError 10065] No route to host"), messages["zh-CN"].connectionTimeout);
  assert.equal(errorMessage("zh-CN", "POST /api/start: request timed out (3500 ms)"), messages["zh-CN"].requestTimeout);
});

test("preview-camera selection survives 500ms pollStatus when stopped and previewError resets cleanly", async t => {
  const f = setup(t);
  await f.app.init();

  // Picking eye_left while stopped must NOT snap back to mouth on pollStatus().
  f.el("preview-camera").value = "eye_left";
  await f.app.switchCamera();
  await f.app.pollStatus();
  assert.equal(f.el("preview-camera").value, "eye_left");

  // Starting uses eye_left, then preview error resets when stopped or restarted.
  await f.app.start("capture");
  f.server.previewStatus = 500;
  await f.app.refreshPreview();
  assert.equal(f.app.state.previewError, true);
  assert.equal(f.el("placeholder").textContent, messages.en.previewError);
  await f.app.stop();
  assert.equal(f.app.state.previewError, false);
  assert.equal(f.el("placeholder").textContent, messages.en.noVideo);

  // Cymple relay errors stay inside #cymple-relay-error without duplicating into top #error.
  f.server.before = (path) => {
    if (path === "/api/cymple-relay") return Response.json({error: "No PICO camera service found"}, {status: 400});
  };
  await f.el("cymple-relay-start").emit("click");
  assert.equal(f.el("cymple-relay-error").hidden, false);
  assert.equal(f.el("error").hidden, true);
  f.server.before = null;

  // Selecting "" in #headset-select clears manual fallback without snapping back to "manual".
  f.el("headset-select").value = "";
  f.el("headset-select").emit("change");
  assert.equal(f.el("headset-select").value, "");

  // deriveView tolerates up to 3999ms status age (matching 3500ms /api/status timeout).
  assert.equal(deriveView(f.server.status, true, 2000).current, true);
  assert.equal(deriveView(f.server.status, true, 4000).current, false);
});

test("index.html uses relative asset paths for Tauri embedded protocol", () => {
  assert.ok(html.includes('href="./icon.png"'));
  assert.ok(html.includes('href="./favicon.ico"'));
  assert.ok(html.includes('href="./styles.css"'));
  assert.ok(html.includes('src="./app.mjs"'));
  assert.doesNotMatch(html, /["']\/static\/|["']\/favicon\.ico/);
});

test("createTauriFetcher maps all REST routes, query params, binary preview 200/204, and errors", async () => {
  const calls = [];
  let previewPayload = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]).buffer;
  let nextError = null;
  const tauriCore = {
    async invoke(command, args) {
      calls.push({command, args});
      if (nextError !== null) {
        const err = nextError;
        nextError = null;
        throw err;
      }
      switch (command) {
        case "get_status": return {running: false, version: "2.6.2"};
        case "get_config": return {headset_port: 45681};
        case "set_config": return args.changes;
        case "start_camera": return {running: true, started: args.payload};
        case "stop_camera": return {running: false};
        case "discover_sources": return {devices: [], queried: args};
        case "cymple_relay_action": return {running: args.payload.action === "start"};
        case "check_updates": return {update_available: false, forced: args.force};
        case "quit_app": return undefined;
        case "get_preview_frame": return previewPayload;
        default: throw new Error(`unexpected command ${command}`);
      }
    },
  };

  const fetcher = createTauriFetcher(tauriCore);

  assert.deepEqual(await requestJson(fetcher, "/api/status"), {running: false, version: "2.6.2"});
  assert.deepEqual(await requestJson(fetcher, "/api/config"), {headset_port: 45681});
  assert.deepEqual(await requestJson(fetcher, "/api/config", {headset_port: 45690}), {headset_port: 45690});
  assert.deepEqual(await requestJson(fetcher, "/api/start", {mode: "capture", camera: "mouth"}), {
    running: true,
    started: {mode: "capture", camera: "mouth"},
  });
  assert.deepEqual(await requestJson(fetcher, "/api/stop", {}), {running: false});
  assert.deepEqual(await requestJson(fetcher, "/api/discovery"), {
    devices: [],
    queried: {host: null, port: null},
  });
  assert.deepEqual(await requestJson(fetcher, "/api/discovery?host=198.51.100.9&port=45681"), {
    devices: [],
    queried: {host: "198.51.100.9", port: 45681},
  });
  assert.deepEqual(await requestJson(fetcher, "/api/cymple-relay", {action: "start", cymple_port: 23303}), {
    running: true,
  });
  assert.deepEqual(await requestJson(fetcher, "/api/update-check"), {update_available: false, forced: false});
  assert.deepEqual(await requestJson(fetcher, "/api/update-check?force=1"), {update_available: false, forced: true});
  assert.equal(await requestJson(fetcher, "/api/shutdown", {}), null);

  // Verify exact command names and argument shapes.
  assert.deepEqual(calls.map(c => c.command), [
    "get_status",
    "get_config",
    "set_config",
    "start_camera",
    "stop_camera",
    "discover_sources",
    "discover_sources",
    "cymple_relay_action",
    "check_updates",
    "check_updates",
    "quit_app",
  ]);
  assert.deepEqual(calls[2].args, {changes: {headset_port: 45690}});
  assert.deepEqual(calls[3].args, {payload: {mode: "capture", camera: "mouth"}});
  assert.deepEqual(calls[7].args, {payload: {action: "start", cymple_port: 23303}});

  // Binary preview: ArrayBuffer non-empty -> 200 JPEG blob.
  const res200Buf = await fetcher("/api/preview.jpg?t=1001");
  assert.equal(res200Buf.ok, true);
  assert.equal(res200Buf.status, 200);
  const blobBuf = await res200Buf.blob();
  assert.equal(blobBuf.type, "image/jpeg");
  assert.deepEqual([...new Uint8Array(await blobBuf.arrayBuffer())], [0xff, 0xd8, 0xff, 0xd9]);

  // Binary preview: Uint8Array non-empty -> 200 JPEG blob.
  previewPayload = new Uint8Array([0xff, 0xd8, 0x01, 0xff, 0xd9]);
  const res200Arr = await fetcher("/api/preview.jpg?t=1002");
  assert.equal(res200Arr.ok, true);
  assert.equal(res200Arr.status, 200);
  assert.equal((await res200Arr.blob()).size, 5);

  // Binary preview: empty ArrayBuffer & empty Uint8Array -> 204.
  previewPayload = new ArrayBuffer(0);
  const res204Buf = await fetcher("/api/preview.jpg?t=1003");
  assert.equal(res204Buf.ok, true);
  assert.equal(res204Buf.status, 204);

  previewPayload = new Uint8Array(0);
  const res204Arr = await fetcher("/api/preview.jpg?t=1004");
  assert.equal(res204Arr.ok, true);
  assert.equal(res204Arr.status, 204);

  // Rejected invoke maps to HTTP 400 JSON { error } and integrates with requestJson + errorMessage.
  nextError = "No PICO camera service found";
  await assert.rejects(
    () => requestJson(fetcher, "/api/cymple-relay", {action: "start"}),
    err => {
      assert.match(err.message, /HTTP 400: No PICO camera service found/);
      assert.equal(errorMessage("en", err.message), messages.en.cympleNoDevice);
      assert.equal(errorMessage("zh-CN", err.message), messages["zh-CN"].cympleNoDevice);
      return true;
    },
  );

  // Unknown route -> 404.
  const res404 = await fetcher("/api/does-not-exist");
  assert.equal(res404.ok, false);
  assert.equal(res404.status, 404);
});

test("Tauri mode syncs tray language and intercepts external update links", async t => {
  const invokes = [];
  const tauriCore = {
    async invoke(command, args) {
      invokes.push({command, args});
      if (command === "set_tray_language" && args.language === "zh-CN") {
        throw new Error("ignored tray failure");
      }
    },
  };
  const f = setup(t, {tauriCore});
  f.server.updateResponse = {
    current_version: "2.6.2",
    latest_version: "2.7.0",
    update_available: true,
    release_url: "https://github.com/WolalaQAQ/PicoET-Enhance/releases/tag/v2.7.0",
    installer_url: "https://example.invalid/Setup.exe",
    portable_url: "",
    module_url: "",
    notes: ["Faster startup"],
    error: null,
  };
  await f.app.init();
  assert.deepEqual(invokes.find(c => c.command === "set_tray_language"), {
    command: "set_tray_language",
    args: {language: "en"},
  });

  f.app.setLanguage("zh-CN");
  assert.deepEqual(invokes.filter(c => c.command === "set_tray_language").at(-1), {
    command: "set_tray_language",
    args: {language: "zh-CN"},
  });

  await f.app.checkUpdates(true);
  const firstLink = f.el("update-links").children[0];
  let prevented = false;
  firstLink.listeners.click({preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.deepEqual(invokes.find(c => c.command === "open_external_url"), {
    command: "open_external_url",
    args: {url: "https://example.invalid/Setup.exe"},
  });

  // Middle click (auxclick with button === 1) must also be intercepted via open_external_url.
  let auxPrevented = false;
  firstLink.listeners.auxclick({button: 1, preventDefault() { auxPrevented = true; }});
  assert.equal(auxPrevented, true);
  assert.equal(invokes.filter(c => c.command === "open_external_url").length, 2);

  // Right click (button === 2) must not trigger open_external_url.
  let rightPrevented = false;
  firstLink.listeners.auxclick({button: 2, preventDefault() { rightPrevented = true; }});
  assert.equal(rightPrevented, false);
  assert.equal(invokes.filter(c => c.command === "open_external_url").length, 2);
});

test("createTauriFetcher handles Array preview fallback and invokeWithSignal abort/timeout", async () => {
  let invokeCount = 0;
  let previewPayload = [0xff, 0xd8, 0x02, 0xff, 0xd9];
  const tauriCore = {
    invoke(command) {
      invokeCount++;
      if (command === "get_preview_frame") return Promise.resolve(previewPayload);
      if (command === "get_status") return new Promise(() => {});
      return Promise.resolve({});
    },
  };
  const fetcher = createTauriFetcher(tauriCore);

  // Array preview payload fallback -> 200 JPEG blob with exact bytes.
  const resArr = await fetcher("/api/preview.jpg?t=1");
  assert.equal(resArr.ok, true);
  assert.equal(resArr.status, 200);
  assert.deepEqual([...new Uint8Array(await (await resArr.blob()).arrayBuffer())], [0xff, 0xd8, 0x02, 0xff, 0xd9]);

  // Empty Array preview payload fallback -> 204.
  previewPayload = [];
  const resEmpty = await fetcher("/api/preview.jpg?t=2");
  assert.equal(resEmpty.ok, true);
  assert.equal(resEmpty.status, 204);

  // Pre-aborted signal rejects immediately with AbortError without invoking backend.
  const preAborted = new AbortController();
  preAborted.abort();
  const callsBeforeAbort = invokeCount;
  await assert.rejects(
    () => fetcher("/api/status", {signal: preAborted.signal}),
    err => err.name === "AbortError",
  );
  assert.equal(invokeCount, callsBeforeAbort);

  // In-flight hanging invoke rejects cleanly on timeout via requestJson AbortController.
  await assert.rejects(
    () => requestJson(fetcher, "/api/status", undefined, 25),
    /request timed out \(25 ms\)/,
  );
});

test("up-to-date update banner memoizes #update-links DOM and filters unsafe URLs", async t => {
  const f = setup(t, {tauriCore: {invoke: async () => {}}});
  await f.app.init();

  // Manual check when up-to-date renders one safe link and preserves exact DOM node across polls.
  await f.app.checkUpdates(true);
  assert.equal(f.el("update-banner").hidden, false);
  assert.equal(f.el("update-links").children.length, 1);
  const originalLink = f.el("update-links").children[0];

  await f.app.pollStatus();
  await f.app.pollStatus();
  assert.strictEqual(f.el("update-links").children[0], originalLink);

  // Changing language updates the link label cleanly.
  f.app.setLanguage("zh-CN");
  assert.equal(f.el("update-links").children.length, 1);
  assert.equal(f.el("update-links").children[0].textContent, messages["zh-CN"].openReleasePage);

  // Non-https, hostless, whitespace, or backslash URLs must never be attached as links.
  f.server.updateResponse = {
    current_version: "2.6.0",
    latest_version: "2.7.0",
    update_available: true,
    release_url: "javascript:alert(1)",
    installer_url: "https:///C:/Windows/win.ini",
    portable_url: "https://example.invalid/has space.zip",
    module_url: "https://example.invalid/back\\slash.zip",
    notes: [],
    error: null,
  };
  await f.app.checkUpdates(true);
  assert.equal(f.el("update-links").children.length, 0);
});

test("background status polling and preview loop pause while doc.hidden and resume on visibilitychange", async t => {
  const f = setup(t);
  await f.app.init();
  await f.app.start("capture");

  const intervalEntry = f.timers.find(entry => entry.kind === "interval" && entry.ms === 500);
  assert.ok(intervalEntry, "expected 500ms status interval");

  // Hide window (e.g. minimized to tray): timers are cleared and manual tick is skipped.
  const callsBeforeHide = f.server.calls.length;
  f.doc.hidden = true;
  f.doc.emit("visibilitychange");
  assert.ok(f.clearedIntervals.includes(intervalEntry.id));
  assert.equal(f.app.state.interval, null);
  assert.equal(f.app.state.previewTimer, null);

  intervalEntry.fn();
  await Promise.resolve();
  assert.equal(f.server.calls.length, callsBeforeHide);

  // Restore window: immediately triggers status poll + restarts interval and preview loop.
  f.doc.hidden = false;
  f.doc.emit("visibilitychange");
  await Promise.resolve();
  assert.notEqual(f.app.state.interval, null);
  assert.notEqual(f.app.state.previewTimer, null);
  assert.ok(
    f.server.calls.slice(callsBeforeHide).some(c => c.path === "/api/status"),
    "expected immediate /api/status refresh on visibilitychange",
  );
});

test("version badge has no hardcoded version in index.html and topbar CSS avoids clipping", () => {
  assert.doesNotMatch(html, /id="version-badge"[^>]*>v2\.6\.2/);
  assert.match(css, /\.topbar\s*\{[^}]*min-height:\s*64px/);
  assert.doesNotMatch(css, /@media\s*\(max-width:1050px\)[^{]*\{[^}]*\.preview-switch\s+span\s*\{\s*display:\s*none/);
});
