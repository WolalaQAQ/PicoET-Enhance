import {errorMessage, initialLanguage, LANGUAGE_KEY, messages} from "./i18n.mjs";

const TEXT_FIELDS = ["headset_host"];
const NUMBER_FIELDS = ["headset_port", "eye_left_port", "eye_right_port", "preview_fps"];
const RELAY_OPTION_IDS = [
  "cymple-host", "cymple-port", "cymple-left-port", "cymple-right-port",
  "cymple-left-rotate", "cymple-left-hflip", "cymple-left-vflip",
  "cymple-right-rotate", "cymple-right-hflip", "cymple-right-vflip",
];
export const PAGES = ["tracking", "preferences"];
const PAGE_NAV = {tracking: "workspace", preferences: "advanced"};
export function routePage(hash = "") { const page = hash.replace(/^#\/?/, ""); return PAGES.includes(page) ? page : "tracking"; }

const LABELS = {
  "host-label": "host",
  capture: "capture",
  stop: "stop",
  "advanced-title": "advanced",
  "diagnostics-title": "diagnostics",
  "port-label": "port",
  "eye-left-port-label": "eyeLeftPort",
  "eye-right-port-label": "eyeRightPort",
  "fps-label": "fps",
  save: "save",
  "preview-label": "preview",
  "camera-mouth-option": "cameraMouth",
  "camera-left-option": "cameraLeft",
  "camera-right-option": "cameraRight",
  "source-label": "source",
  "source-age-label": "sourceAge",
  "receive-age-label": "receiveAge",
  "rate-label": "rates",
  "drop-label": "drops",
  "nav-caption": "navCaption",
  "nav-live-label": "navLive",
  "nav-settings-label": "navSettings",
  "local-label": "localLabel",
  "privacy-note": "privacyNote",
  "workspace-caption": "workspaceCaption",
  "page-eyebrow": "pageEyebrow",
  "headset-title": "headsetTitle",
  "headset-select-label": "headsetSelect",
  "run-help": "runHelp",
  "camera-eyebrow": "cameraEyebrow",
  "camera-title": "cameraTitle",
  "camera-privacy": "cameraPrivacy",
  "footer-note": "footerNote",
  "technical-error-title": "technicalError",
  "quit-service": "quitService",
  "update-dismiss": "updateDismiss",
  "cymple-relay-eyebrow": "cympleRelayEyebrow",
  "cymple-relay-title": "cympleRelayTitle",
  "cymple-relay-help": "cympleRelayHelp",
  "cymple-relay-start": "cympleRelayStart",
  "cymple-relay-stop": "cympleRelayStop",
  "cymple-left-title": "cympleLeftEye",
  "cymple-right-title": "cympleRightEye",
  "cymple-relay-options-title": "cympleRelayOptions",
  "cymple-host-label": "cympleHost",
  "cymple-port-label": "cymplePort",
  "cymple-left-port-label": "cympleLeftPort",
  "cymple-right-port-label": "cympleRightPort",
  "cymple-left-orient-title": "cympleLeftEye",
  "cymple-right-orient-title": "cympleRightEye",
  "cymple-left-rotate-label": "cympleRotate",
  "cymple-right-rotate-label": "cympleRotate",
  "cymple-left-hflip-label": "cympleHflip",
  "cymple-right-hflip-label": "cympleHflip",
  "cymple-left-vflip-label": "cympleVflip",
  "cymple-right-vflip-label": "cympleVflip",
};

const display = x => x == null || x === "" ? "—" : String(x);
const fixed = (x, n = 1) => x != null && Number.isFinite(Number(x)) ? Number(x).toFixed(n) : "—";

function jsonResponse(data, status = 200) {
  const payload = data === undefined ? "" : JSON.stringify(data);
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() { return payload; },
    async json() { return payload ? JSON.parse(payload) : null; },
  };
}

function invokeWithSignal(tauriCore, command, args, signal) {
  if (signal?.aborted) {
    const err = new Error("Aborted");
    err.name = "AbortError";
    return Promise.reject(err);
  }
  const promise = args === undefined ? tauriCore.invoke(command) : tauriCore.invoke(command, args);
  if (!signal) return promise;
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      const err = new Error("Aborted");
      err.name = "AbortError";
      reject(err);
    };
    signal.addEventListener("abort", onAbort, {once: true});
    Promise.resolve(promise).then(
      value => { signal.removeEventListener("abort", onAbort); resolve(value); },
      error => { signal.removeEventListener("abort", onAbort); reject(error); },
    );
  });
}

export function createTauriFetcher(tauriCore) {
  if (!tauriCore || typeof tauriCore.invoke !== "function") {
    throw new TypeError("tauriCore.invoke is required");
  }
  return async function tauriFetch(path, opts = {}) {
    const method = String(opts.method || "GET").toUpperCase();
    const url = new URL(path, "http://tauri.local");
    const pathname = url.pathname;
    const params = url.searchParams;
    const parseBody = () => {
      if (opts.body == null || opts.body === "") return {};
      return typeof opts.body === "string" ? JSON.parse(opts.body) : opts.body;
    };
    const run = async (command, args) => {
      try {
        const result = await invokeWithSignal(tauriCore, command, args, opts.signal);
        return jsonResponse(result, 200);
      } catch (err) {
        if (err?.name === "AbortError") throw err;
        return {
          ok: false,
          status: 400,
          async text() { return JSON.stringify({error: String(err)}); },
        };
      }
    };

    if (method === "GET" && pathname === "/api/status") return run("get_status");
    if (method === "GET" && pathname === "/api/config") return run("get_config");
    if (method === "POST" && pathname === "/api/config") return run("set_config", {changes: parseBody()});
    if (method === "POST" && pathname === "/api/start") return run("start_camera", {payload: parseBody()});
    if (method === "POST" && pathname === "/api/stop") return run("stop_camera");
    if (method === "GET" && pathname === "/api/discovery") {
      const rawHost = params.get("host");
      const rawPort = params.get("port");
      const host = rawHost && rawHost.trim() ? rawHost.trim() : null;
      const parsedPort = rawPort != null && rawPort.trim() !== "" ? Number(rawPort) : null;
      const port = Number.isInteger(parsedPort) ? parsedPort : null;
      return run("discover_sources", {host, port});
    }
    if (method === "POST" && pathname === "/api/cymple-relay") return run("cymple_relay_action", {payload: parseBody()});
    if (method === "GET" && pathname === "/api/update-check") {
      const rawForce = params.get("force");
      const force = rawForce === "1" || rawForce === "true";
      return run("check_updates", {force});
    }
    if (method === "POST" && pathname === "/api/shutdown") return run("quit_app");
    if (method === "GET" && pathname === "/api/preview.jpg") {
      try {
        const raw = await invokeWithSignal(tauriCore, "get_preview_frame", undefined, opts.signal);
        const bytes = raw instanceof Uint8Array
          ? raw
          : raw instanceof ArrayBuffer
            ? new Uint8Array(raw)
            : ArrayBuffer.isView(raw)
              ? new Uint8Array(raw.buffer, raw.byteOffset, raw.byteLength)
              : Array.isArray(raw)
                ? new Uint8Array(raw)
                : new Uint8Array(0);
        if (bytes.byteLength === 0) {
          return {
            ok: true,
            status: 204,
            async blob() { return new Blob([], {type: "image/jpeg"}); },
            async text() { return ""; },
          };
        }
        return {
          ok: true,
          status: 200,
          async blob() { return new Blob([bytes], {type: "image/jpeg"}); },
          async text() { return ""; },
        };
      } catch (err) {
        if (err?.name === "AbortError") throw err;
        return {
          ok: false,
          status: 400,
          async text() { return JSON.stringify({error: String(err)}); },
        };
      }
    }
    return {
      ok: false,
      status: 404,
      async text() { return JSON.stringify({error: `Unknown route: ${method} ${pathname}`}); },
    };
  };
}

export async function requestJson(fetcher, path, body, timeoutMs = 3500) {
  const method = body === undefined ? "GET" : "POST", controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetcher(path, {
      method,
      cache: "no-store",
      signal: controller.signal,
      ...(body === undefined ? {} : {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}),
    });
    const text = await response.text();
    let data;
    try { data = text ? JSON.parse(text) : null; } catch { throw Error(`${method} ${path} · HTTP ${response.status}: ${text.slice(0, 240)}`); }
    if (!response.ok) throw Error(`${method} ${path} · HTTP ${response.status}: ${data?.error || text}`);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw Error(`${method} ${path}: request timed out (${timeoutMs} ms)`);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export function deriveView(status, online, ageMs = 0) {
  const running = status?.running === true, current = online && ageMs < 4000;
  const connection = status?.connected === true;
  const video = current && running && connection && status?.metrics?.video_fresh === true;
  return {
    running,
    current,
    video,
    key: !current ? "offline" : !running ? "stopped" : !connection ? "connecting" : video ? "recordingOnly" : "stale",
  };
}

export function createApp(doc, options = {}) {
  const fetcher = options.fetch ?? globalThis.fetch.bind(globalThis), timers = options.timers ?? globalThis;
  const now = options.now ?? Date.now, urls = options.urlApi ?? globalThis.URL, storage = options.storage;
  const navigation = options.navigation, tauriCore = options.tauriCore;
  function syncTrayLanguage(language) {
    try {
      const p = tauriCore?.invoke?.("set_tray_language", {language});
      if (p && typeof p.catch === "function") p.catch(() => {});
    } catch {}
  }
  function isSafeExternalUrl(href) {
    if (typeof href !== "string") return false;
    const trimmed = href.trim();
    if (!/^https:\/\//i.test(trimmed) || trimmed.length <= 8 || trimmed[8] === "/") return false;
    if (/[\u0000-\u001f\u007f\s"\\]/.test(trimmed)) return false;
    return true;
  }
  function wireExternalLink(link, href) {
    if (!tauriCore?.invoke || !isSafeExternalUrl(href)) return;
    const open = event => {
      event?.preventDefault?.();
      try {
        const p = tauriCore.invoke("open_external_url", {url: href});
        if (p && typeof p.catch === "function") p.catch(() => {});
      } catch {}
    };
    link.addEventListener("click", open);
    link.addEventListener("auxclick", event => {
      if (event?.button === 1) open(event);
    });
  }
  const get = id => doc.getElementById(id), state = {
    language: initialLanguage(storage, options.languages ?? []),
    config: null,
    status: null,
    online: false,
    at: 0,
    busy: "",
    dirty: false,
    error: "",
    errorKey: "",
    polling: false,
    pollPromise: null,
    pollError: "",
    destroyed: false,
    stoppedByUser: false,
    generation: 0,
    previewGeneration: 0,
    previewBusy: false,
    previewAbort: null,
    previewUrl: null,
    previewError: false,
    previewOwner: null,
    discovering: false,
    discovered: [],
    discoveryError: "",
    discoveryDone: false,
    discoveryKey: "",
    selectedHeadset: "",
    headsetCleared: false,
    cameraPicked: false,
    page: routePage(navigation?.location.hash),
    relayDirty: false,
    relayPortsManual: false,
    relayPortsAuto: false,
    relayError: "",
    relayErrorKey: "",
    updateInfo: null,
    checkingUpdate: false,
    updateManual: false,
    updateDismissed: false,
    updateKey: "",
  };
  const t = key => messages[state.language][key] ?? key, view = () => deriveView(state.status, state.online, now() - state.at);

  function clearPreview() {
    state.previewGeneration++;
    state.previewAbort?.abort();
    get("preview").hidden = true;
    get("preview").removeAttribute("src");
    if (state.previewUrl) urls.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
    get("placeholder").hidden = false;
    get("empty-camera").hidden = false;
  }

  function discoveredPorts(device) {
    const streams = device?.streams ?? [];
    return {
      mouthPort: streams.find(s => s.id === "mouth")?.port ?? device.port ?? (Number(get("headset_port").value) || 45681),
      leftPort: streams.find(s => s.id === "eye_left")?.port ?? (Number(get("eye_left_port").value) || 45682),
      rightPort: streams.find(s => s.id === "eye_right")?.port ?? (Number(get("eye_right_port").value) || 45683),
    };
  }

  function activeTarget() {
    const manualHost = get("headset_host").value.trim();
    const formMouth = Number(get("headset_port").value) || 45681;
    const formLeft = Number(get("eye_left_port").value) || 45682;
    const formRight = Number(get("eye_right_port").value) || 45683;
    const selected = state.selectedHeadset;
    if (selected && selected !== "manual") {
      const device = state.discovered.find(d => `${d.host}:${d.port}` === selected);
      if (device) return {kind: "discovered", host: device.host, ...discoveredPorts(device)};
    }
    if (manualHost && !state.headsetCleared && (selected === "manual" || !selected)) {
      const matched = state.discovered.some(d => d.host === manualHost && d.port === formMouth);
      return {kind: matched ? "discovered" : "manual", host: manualHost, mouthPort: formMouth, leftPort: formLeft, rightPort: formRight};
    }
    return {kind: "none", host: "", mouthPort: formMouth, leftPort: formLeft, rightPort: formRight};
  }

  function activeCameraPort(camera, target = activeTarget()) {
    if (camera === "eye_left") return target.leftPort;
    if (camera === "eye_right") return target.rightPort;
    return target.mouthPort;
  }

  function renderUpdateBanner() {
    get("check-update").textContent = t(state.checkingUpdate ? "checkingUpdate" : "checkUpdate");
    get("check-update").disabled = state.checkingUpdate || state.stoppedByUser;
    const banner = get("update-banner"), info = state.updateInfo;
    const show = Boolean(info && !state.updateDismissed && (info.update_available || state.updateManual));
    banner.hidden = !show;
    if (!show) return;
    const notesEl = get("update-notes"), linksEl = get("update-links");
    if (info.error) {
      get("update-message").textContent = `${t("updateFailed")} ${info.error}`;
      if (state.updateKey !== "error") {
        state.updateKey = "error";
        notesEl.hidden = true;
        notesEl.replaceChildren();
        linksEl.replaceChildren();
      }
      return;
    }
    const addLink = (href, labelKey, primary = false) => {
      if (!isSafeExternalUrl(href)) return;
      const safeHref = href.trim();
      const link = doc.createElement("a");
      link.setAttribute("href", safeHref);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      if (primary) link.setAttribute("class", "primary-link");
      link.textContent = t(labelKey);
      wireExternalLink(link, safeHref);
      linksEl.append(link);
    };
    if (info.update_available) {
      get("update-message").textContent = `${t("updateAvailable")} v${info.latest_version} (${t("updateCurrent")}: v${info.current_version})`;
      const key = JSON.stringify(["available", state.language, info.latest_version, info.notes, info.installer_url, info.portable_url, info.module_url, info.release_url]);
      if (key !== state.updateKey) {
        state.updateKey = key;
        notesEl.replaceChildren();
        for (const line of info.notes ?? []) {
          const li = doc.createElement("li");
          li.textContent = line;
          notesEl.append(li);
        }
        notesEl.hidden = !(info.notes?.length);
        linksEl.replaceChildren();
        addLink(info.installer_url, "downloadInstaller", true);
        addLink(info.portable_url, "downloadPortable");
        addLink(info.module_url, "downloadModule");
        addLink(info.release_url, "openReleasePage");
      }
      return;
    }
    get("update-message").textContent = `${t("updateUpToDate")} v${info.current_version}`;
    const key = JSON.stringify(["uptodate", state.language, info.release_url]);
    if (key !== state.updateKey) {
      state.updateKey = key;
      notesEl.hidden = true;
      notesEl.replaceChildren();
      linksEl.replaceChildren();
      addLink(info.release_url, "openReleasePage");
    }
  }

  function renderPages() {
    for (const page of PAGES) {
      const active = state.page === page;
      get(`page-${page}`).hidden = !active;
      get(`page-${page}`).setAttribute("aria-hidden", String(!active));
      const link = get(`nav-${PAGE_NAV[page]}`);
      link.setAttribute("class", `nav-link${active ? " active" : ""}`);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    }
    get("title").textContent = t(`${state.page}Title`);
    get("page-intro").textContent = t(`${state.page}Intro`);
    get("connection-setup").hidden = state.page !== "tracking";
  }

  function navigate(page, updateHash = true) {
    if (!PAGES.includes(page) || state.destroyed) return;
    state.page = page;
    render();
    if (updateHash && navigation && navigation.location.hash !== `#/${page}`) navigation.location.hash = `#/${page}`;
    if (updateHash) { navigation?.scrollTo?.({top: 0, behavior: "auto"}); get("title").focus?.({preventScroll: true}); }
  }
  const hashChanged = () => navigate(routePage(navigation.location.hash), false);

  function renderConnection(current) {
    const manualHost = get("headset_host").value.trim();
    const formMouth = Number(get("headset_port").value) || 45681;
    const discoveredValues = state.discovered.map(d => `${d.host}:${d.port}`);
    let selected = "";
    if (discoveredValues.includes(state.selectedHeadset)) {
      selected = state.selectedHeadset;
    } else if (manualHost && !state.headsetCleared && (state.selectedHeadset === "manual" || !state.selectedHeadset)) {
      const match = state.discovered.find(d => d.host === manualHost && d.port === formMouth);
      selected = match ? `${match.host}:${match.port}` : "manual";
    }
    const key = JSON.stringify([state.discovered, manualHost, formMouth, selected, state.language]);
    if (key !== state.discoveryKey) {
      state.discoveryKey = key;
      const selector = get("headset-select");
      selector.replaceChildren();
      const add = (value, title) => { const option = doc.createElement("option"); option.value = value; option.textContent = title; selector.append(option); };
      add("", t(state.discovered.length ? "chooseHeadset" : "noHeadset"));
      for (const device of state.discovered) add(`${device.host}:${device.port}`, `${device.name} · ${device.host}`);
      if (manualHost && !state.discovered.some(item => item.host === manualHost && item.port === formMouth)) {
        add("manual", `${t("manualHeadset")} · ${manualHost}`);
      }
      selector.value = selected;
    }
    const target = activeTarget();
    get("discover").textContent = t(state.discovering ? "discovering" : "discover");
    get("discover").disabled = state.discovering || !current.current || Boolean(state.busy) || current.running;
    get("headset-select").disabled = state.discovering || Boolean(state.busy) || current.running || !current.current;
    get("discovery-badge").textContent = t(
      target.kind === "discovered" ? "headsetFound" : target.kind === "manual" ? "manualHeadset" : "autoDiscovery"
    );
    get("discovery-info").textContent = state.discoveryError ? errorMessage(state.language, state.discoveryError) :
      t(state.discovering ? "discoverySearching" : state.discovered.length ? "discoveryFound" : state.discoveryDone ? "discoveryEmpty" : "discoveryHelp");
  }

  function applyConfig(config) {
    state.config = config;
    if (!state.dirty) {
      for (const key of [...TEXT_FIELDS, ...NUMBER_FIELDS]) get(key).value = config[key] == null ? "" : String(config[key]);
      if (!state.cameraPicked) get("preview-camera").value = config.preview_camera ?? "mouth";
      if (config.headset_host && !state.selectedHeadset && !state.headsetCleared) state.selectedHeadset = "manual";
    }
  }

  function applyCympleRelay(relay) {
    if (!relay?.config || state.relayDirty) return;
    const cfg = relay.config;
    get("cymple-host").value = cfg.cymple_host ?? "127.0.0.1";
    get("cymple-port").value = String(cfg.cymple_port ?? 23303);
    if (!state.relayPortsAuto) {
      get("cymple-left-port").value = String(relay.left_port ?? cfg.left_port ?? 45682);
      get("cymple-right-port").value = String(relay.right_port ?? cfg.right_port ?? 45683);
    }
    get("cymple-left-rotate").value = String(cfg.left_rotate ?? 0);
    get("cymple-right-rotate").value = String(cfg.right_rotate ?? 0);
    get("cymple-left-hflip").checked = cfg.left_hflip === true;
    get("cymple-left-vflip").checked = cfg.left_vflip === true;
    get("cymple-right-hflip").checked = cfg.right_hflip === true;
    get("cymple-right-vflip").checked = cfg.right_vflip === true;
  }

  function applyDiscoveredEyePorts(device) {
    if (!device?.streams || state.relayPortsManual || state.status?.cymple_relay?.running) return;
    const left = device.streams.find(s => s.id === "eye_left")?.port;
    const right = device.streams.find(s => s.id === "eye_right")?.port;
    if (left) { get("cymple-left-port").value = String(left); state.relayPortsAuto = true; }
    if (right) { get("cymple-right-port").value = String(right); state.relayPortsAuto = true; }
  }

  function readCympleRelayOptions() {
    return {
      headset_host: activeTarget().host,
      cymple_host: get("cymple-host").value.trim(),
      cymple_port: Number(get("cymple-port").value),
      left_port: Number(get("cymple-left-port").value),
      right_port: Number(get("cymple-right-port").value),
      left_rotate: Number(get("cymple-left-rotate").value),
      left_hflip: get("cymple-left-hflip").checked === true,
      left_vflip: get("cymple-left-vflip").checked === true,
      right_rotate: Number(get("cymple-right-rotate").value),
      right_hflip: get("cymple-right-hflip").checked === true,
      right_vflip: get("cymple-right-vflip").checked === true,
    };
  }

  function renderCympleRelay(current) {
    const relay = state.status?.cymple_relay;
    const relayRunning = relay?.running === true;
    const blocked = !current.current || Boolean(state.busy) || state.stoppedByUser;
    const relayState = current.current ? (relay?.state ?? "idle") : "offline";
    const statusKey = {
      idle: "cympleStateIdle",
      connecting: "cympleStateConnecting",
      running: "cympleStateRunning",
      reconnecting: "cympleStateReconnecting",
      offline: "offline",
    }[relayState] ?? "cympleStateIdle";
    get("cymple-relay-status").textContent = t(statusKey);
    get("cymple-relay-status").setAttribute("data-state", !current.current ? "offline" :
      relayState === "running" ? "running" : ["connecting", "reconnecting"].includes(relayState) ? "connecting" : "stopped");
    get("cymple-relay-start").hidden = relayRunning;
    get("cymple-relay-stop").hidden = !relayRunning;
    get("cymple-relay-start").disabled = blocked || relayRunning;
    get("cymple-relay-stop").disabled = blocked || !relayRunning;
    for (const id of RELAY_OPTION_IDS) get(id).disabled = blocked || relayRunning;
    get("cymple-relay-endpoint").textContent = current.current && relayRunning && relay?.source_host ?
      `${relay.source_host} (${relay.left_port}/${relay.right_port}) → ${relay.cymple_host}:${relay.cymple_port}` :
      `→ ${get("cymple-host").value.trim() || "127.0.0.1"}:${get("cymple-port").value || 23303}`;
    for (const [side, key] of [["left", "eye_left"], ["right", "eye_right"]]) {
      const eye = current.current ? relay?.eyes?.[key] : null;
      const eyeStateKey = {
        idle: "cympleStateIdle",
        connecting: "cympleStateConnecting",
        streaming: "cympleStateStreaming",
        reconnecting: "cympleStateReconnecting",
      }[eye?.state ?? "idle"] ?? "cympleStateIdle";
      get(`cymple-${side}-state`).textContent = t(eyeStateKey);
      get(`cymple-${side}-metrics`).textContent = !eye || (!relayRunning && !(eye.frames > 0)) ? "—" :
        `${fixed(eye.fps)} fps · ${fixed(eye.avg_kb)} KB/f · ${eye.frames} ${t("cympleRelayFrames")}`;
    }
    const eyeErrors = current.current ? ["eye_left", "eye_right"].map(k => relay?.eyes?.[k]?.error).filter(Boolean) : [];
    const rawErr = state.relayError || (current.current ? relay?.error : "") || eyeErrors.join(" · ");
    get("cymple-relay-error").hidden = !state.relayErrorKey && !rawErr;
    get("cymple-relay-error").textContent = state.relayErrorKey ? t(state.relayErrorKey) : rawErr ? errorMessage(state.language, rawErr) : "";
  }

  function render() {
    const current = view();
    doc.documentElement.lang = state.language;
    doc.title = t("title");
    for (const [id, key] of Object.entries(LABELS)) get(id).textContent = t(key);
    get("language").value = state.language;
    get("language").setAttribute("aria-label", t("language"));
    get("preview").setAttribute("alt", t("previewAlt"));
    get("status").textContent = state.stoppedByUser ? t("serviceStopped") :
      t(state.busy || (!state.config && !state.pollError ? "loading" : current.key));
    get("status").setAttribute("data-state", state.stoppedByUser ? "offline" : current.key);
    const versionText = state.status?.version ? `v${state.status.version}` : "";
    get("version-badge").textContent = versionText;
    get("version-badge").hidden = !versionText;
    get("camera-panel").setAttribute("data-live", String(current.video));
    get("service-state").textContent = t(current.current && !state.stoppedByUser ? "serviceOnline" : "serviceOffline");
    get("service-state").setAttribute("data-online", String(current.current && !state.stoppedByUser));
    get("capture").disabled = !state.config || !current.current || Boolean(state.busy) || current.running || state.stoppedByUser;
    get("stop").disabled = Boolean(state.busy) || !current.running || state.stoppedByUser;
    get("capture").hidden = current.running;
    get("stop").hidden = !current.running;
    get("preview-camera").disabled = Boolean(state.busy) || !state.config || state.stoppedByUser;
    get("save").disabled = !state.config || Boolean(state.busy) || !state.dirty || state.stoppedByUser;
    for (const id of [...TEXT_FIELDS, ...NUMBER_FIELDS]) get(id).disabled = Boolean(state.busy) || !state.config || state.stoppedByUser;
    get("quit-service").disabled = Boolean(state.busy) || state.stoppedByUser;
    get("config-state").textContent = state.config ? t(state.dirty ? "unsaved" : "saved") : "";
    const target = activeTarget(), cam = get("preview-camera").value || "mouth";
    get("camera-stream-url").textContent = target.host ? `http://${target.host}:${activeCameraPort(cam, target)}/stream.mjpg` : "";
    renderConnection(current);
    renderPages();
    renderUpdateBanner();
    const error = state.error || state.pollError || state.status?.last_error;
    get("error").hidden = state.stoppedByUser || (!error && !state.errorKey);
    get("error").textContent = state.errorKey ? t(state.errorKey) : error ? errorMessage(state.language, error) : "";
    const relayEyeErrors = ["eye_left", "eye_right"].map(k => state.status?.cymple_relay?.eyes?.[k]?.error).filter(Boolean);
    const technicalParts = [...new Set([error, state.discoveryError, state.relayError, state.status?.cymple_relay?.error, ...relayEyeErrors].filter(Boolean))];
    get("technical-error").textContent = technicalParts.join("\n\n") || "—";
    const owner = `${state.status?.source?.session_id}/${state.status?.output_session_id}`;
    if (owner !== state.previewOwner) { state.previewError = false; clearPreview(); state.previewOwner = owner; }
    if (!current.running) state.previewError = false;
    if (!current.video || state.busy || !get("preview-enabled").checked || state.stoppedByUser) clearPreview();
    const waitingIdle = Boolean(current.running && state.status?.connected && !current.video && get("preview-enabled").checked && !state.previewError);
    get("placeholder").textContent = t(!get("preview-enabled").checked ? "previewOff" : state.previewError ? "previewError" : waitingIdle ? "waitingHeadsetWake" : "noVideo");
    get("camera-empty-help").textContent = t(waitingIdle ? "cameraWakeHelp" : "cameraEmptyHelp");
    const identity = value => typeof value === "number" && !Number.isSafeInteger(value) ? t("unsafeInteger") : display(value);
    const source = state.status?.source ?? {}, m = current.current ? state.status?.metrics ?? {} : {};
    get("source-info").textContent = `${identity(source.session_id)} / ${identity(source.source_seq)}`;
    get("raw-identity").textContent = identity(source.raw_identity);
    get("time-mouth").textContent = identity(source.mouth_sensor_ns);
    get("source-age").textContent = `${fixed(m.source_age_ms)} ms`;
    get("receive-age").textContent = `${fixed(m.receive_age_ms)} ms`;
    get("rates").textContent = `${fixed(m.received_hz)} / ${fixed(m.decoded_hz)} Hz`;
    get("drops").textContent = `${display(m.source_gaps)} / ${display(m.pending_dropped)}`;
    get("snapshot-state").textContent = t(current.current ? "snapshot" : "expiredSnapshot");
    renderCympleRelay(current);
  }

  async function discover() {
    if (state.discovering || state.destroyed || view().running) return;
    state.discovering = true;
    state.discoveryError = "";
    render();
    try {
      const result = await requestJson(fetcher, "/api/discovery", undefined, 10000);
      if (state.destroyed) return;
      state.discovered = result.devices;
      state.discoveryDone = true;
      if (result.devices.length === 1 && !get("headset_host").value.trim() && !view().running) {
        const device = result.devices[0];
        state.selectedHeadset = `${device.host}:${device.port}`;
        applyDiscoveredEyePorts(device);
      } else if (state.selectedHeadset && state.selectedHeadset !== "manual") {
        const stillThere = result.devices.some(d => `${d.host}:${d.port}` === state.selectedHeadset);
        if (!stillThere) state.selectedHeadset = get("headset_host").value.trim() ? "manual" : "";
      }
    } catch (error) {
      if (!state.destroyed) { state.discovered = []; state.discoveryError = error.message; }
    } finally {
      state.discovering = false;
      if (!state.destroyed) render();
    }
  }

  async function pollStatus() {
    if (state.busy || state.destroyed) return;
    if (state.pollPromise) return state.pollPromise;
    state.polling = true;
    const generation = state.generation;
    const wasOnline = state.online;
    const promise = (async () => {
      try {
        const status = await requestJson(fetcher, "/api/status");
        if (generation !== state.generation || state.destroyed) return;
        state.status = status;
        applyConfig(status.config ?? await requestJson(fetcher, "/api/config"));
        applyCympleRelay(status.cymple_relay);
        state.online = true;
        state.at = now();
        state.pollError = "";
        if (!wasOnline || (status.running && status.connected && !status.last_error)) state.error = "";
      } catch (error) {
        if (generation === state.generation && !state.destroyed) { state.online = false; state.pollError = error.message; }
      } finally {
        state.polling = false;
        if (state.pollPromise === promise) state.pollPromise = null;
        if (!state.destroyed) render();
      }
    })();
    state.pollPromise = promise;
    return promise;
  }

  function readForm() {
    const config = Object.fromEntries(TEXT_FIELDS.map(key => [key, get(key).value.trim()]));
    for (const key of NUMBER_FIELDS) config[key] = Number(get(key).value);
    return {...config, preview_camera: get("preview-camera").value || "mouth"};
  }

  async function control(key, action, {reportError = true} = {}) {
    if (state.busy || state.destroyed) return;
    state.busy = key;
    state.generation++;
    if (reportError) {
      state.error = "";
      state.errorKey = "";
    }
    render();
    try {
      await action();
      state.status = await requestJson(fetcher, "/api/status");
      applyConfig(state.status.config ?? state.config);
      applyCympleRelay(state.status.cymple_relay);
      state.online = true;
      state.at = now();
      state.pollError = "";
    } catch (error) {
      if (reportError) state.error = error.message;
      try {
        const status = await requestJson(fetcher, "/api/status", undefined, 2000);
        if (!state.destroyed) {
          state.status = status;
          applyConfig(status.config ?? state.config);
          applyCympleRelay(status.cymple_relay);
          state.online = true;
          state.at = now();
          state.pollError = "";
        }
      } catch {}
    } finally {
      state.busy = "";
      if (!state.destroyed) render();
    }
  }

  async function persistConfig() {
    const saved = await requestJson(fetcher, "/api/config", readForm(), 20000);
    state.dirty = false;
    state.cameraPicked = false;
    applyConfig(saved);
  }

  function validate(mode = "live") {
    if (mode === "live" && !activeTarget().host) state.errorKey = "required";
    else if (!get("settings").checkValidity()) state.errorKey = "invalidSettings";
    else return true;
    render();
    return false;
  }

  async function save() { if (!get("save").disabled && validate("save")) await control("saving", persistConfig); }
  async function start(mode = "capture") {
    if (get("capture").disabled || !validate("live")) return;
    const target = activeTarget();
    const camera = get("preview-camera").value || "mouth";
    state.previewError = false;
    await control("starting", async () => {
      if (state.dirty) await persistConfig();
      await requestJson(fetcher, "/api/start", {
        mode,
        camera,
        headset_host: target.host,
        headset_port: target.mouthPort,
        eye_left_port: target.leftPort,
        eye_right_port: target.rightPort,
      }, 60000);
      state.cameraPicked = false;
    });
  }
  async function stop() {
    if (get("stop").disabled) return;
    state.previewError = false;
    await control("stopping", () => requestJson(fetcher, "/api/stop", {}, 20000));
  }

  async function switchCamera() {
    if (state.busy || state.destroyed) return;
    state.previewError = false;
    if (!view().running) {
      state.cameraPicked = true;
      state.errorKey = "";
      render();
      return;
    }
    if (!validate("live")) return;
    const target = activeTarget();
    const camera = get("preview-camera").value || "mouth";
    await control("starting", async () => {
      await requestJson(fetcher, "/api/stop", {}, 20000);
      if (state.dirty) await persistConfig();
      await requestJson(fetcher, "/api/start", {
        mode: "capture",
        camera,
        headset_host: target.host,
        headset_port: target.mouthPort,
        eye_left_port: target.leftPort,
        eye_right_port: target.rightPort,
      }, 60000);
      state.cameraPicked = false;
    });
  }

  async function cympleRelay(action) {
    const btn = get(action === "start" ? "cymple-relay-start" : "cymple-relay-stop");
    if (btn.disabled) return;
    state.relayError = "";
    state.relayErrorKey = "";
    const options = action === "start" ? readCympleRelayOptions() : null;
    const validPort = p => Number.isInteger(p) && p >= 1 && p <= 65535;
    if (action === "start" && !(options.cymple_host && validPort(options.cymple_port) && validPort(options.left_port) && validPort(options.right_port) && [0, 90, 180, 270].includes(options.left_rotate) && [0, 90, 180, 270].includes(options.right_rotate))) {
      state.relayErrorKey = "cympleInvalidConfig";
      render();
      return;
    }
    await control(action === "start" ? "starting" : "stopping", async () => {
      try {
        await requestJson(fetcher, "/api/cymple-relay", {action, ...(options ?? {})}, 15000);
        state.relayDirty = false;
        state.relayPortsAuto = false;
      } catch (error) {
        state.relayError = error.message;
        throw error;
      }
    }, {reportError: false});
  }

  async function checkUpdates(manual = false) {
    if (state.checkingUpdate || state.destroyed) return;
    state.checkingUpdate = true;
    state.updateManual = manual;
    if (manual) state.updateDismissed = false;
    render();
    try {
      const info = await requestJson(fetcher, manual ? "/api/update-check?force=1" : "/api/update-check", undefined, 8000);
      if (!state.destroyed) state.updateInfo = info;
    } catch (error) {
      if (!state.destroyed && manual) {
        state.updateInfo = {update_available: false, error: error.message};
      }
    } finally {
      state.checkingUpdate = false;
      if (!state.destroyed) render();
    }
  }

  async function quitService() {
    if (get("quit-service").disabled || state.destroyed) return;
    state.busy = "quittingService";
    state.generation++;
    state.error = "";
    state.errorKey = "";
    render();
    try {
      await requestJson(fetcher, "/api/shutdown", {}, 5000);
      state.busy = "";
      state.online = false;
      state.stoppedByUser = true;
      destroy();
      render();
    } catch (error) {
      state.busy = "";
      state.error = error.message;
      if (!state.destroyed) render();
    }
  }

  async function refreshPreview() {
    if (state.destroyed || state.busy || state.previewBusy || doc.hidden || state.page !== "tracking" || !get("preview-enabled").checked || !view().video) return;
    state.previewBusy = true;
    const generation = state.previewGeneration, controller = new AbortController();
    state.previewAbort = controller;
    const timeout = timers.setTimeout(() => controller.abort(), 2500);
    try {
      const response = await fetcher(`/api/preview.jpg?t=${now()}`, {cache: "no-store", signal: controller.signal});
      if (generation !== state.previewGeneration || state.destroyed || !view().video) return;
      if (response.status === 204) { clearPreview(); return; }
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      if (generation !== state.previewGeneration || state.destroyed || !view().video) return;
      if (state.previewUrl) urls.revokeObjectURL(state.previewUrl);
      state.previewUrl = urls.createObjectURL(blob);
      state.previewError = false;
      get("preview").src = state.previewUrl;
      get("preview").hidden = false;
      get("placeholder").hidden = true;
      get("empty-camera").hidden = true;
    } catch {
      if (generation === state.previewGeneration && !state.destroyed) { state.previewError = true; clearPreview(); render(); }
    } finally {
      timers.clearTimeout(timeout);
      if (state.previewAbort === controller) {
        state.previewBusy = false;
        state.previewAbort = null;
      }
    }
  }

  function schedulePreview() {
    if (state.destroyed || doc.hidden || state.previewTimer != null) return;
    const delay = 1000 / Math.min(30, Math.max(1, state.config?.preview_fps ?? 10));
    state.previewTimer = timers.setTimeout(previewLoop, delay);
  }

  function previewLoop() {
    state.previewTimer = null;
    if (state.destroyed || doc.hidden) return;
    void refreshPreview();
    schedulePreview();
  }

  function startPreviewLoop() {
    if (state.destroyed || doc.hidden || state.previewTimer != null) return;
    previewLoop();
  }

  function startStatusInterval() {
    if (state.destroyed || doc.hidden || state.interval != null) return;
    state.interval = timers.setInterval(() => {
      if (doc.hidden || state.destroyed) return;
      render();
      void pollStatus();
    }, 500);
  }

  function pauseBackgroundLoops() {
    if (state.interval != null) {
      timers.clearInterval(state.interval);
      state.interval = null;
    }
    if (state.previewTimer != null) {
      timers.clearTimeout(state.previewTimer);
      state.previewTimer = null;
    }
    state.previewGeneration++;
    state.previewAbort?.abort();
  }

  const onVisibilityChange = () => {
    if (state.destroyed) return;
    if (doc.hidden) {
      pauseBackgroundLoops();
      return;
    }
    if (state.online) state.at = now();
    return pollStatus().finally(() => {
      if (!state.destroyed && !doc.hidden) {
        startStatusInterval();
        startPreviewLoop();
      }
    });
  };
  doc.addEventListener?.("visibilitychange", onVisibilityChange);

  function setLanguage(language) {
    if (!Object.hasOwn(messages, language)) return;
    state.language = language;
    try { storage?.setItem(LANGUAGE_KEY, language); } catch {}
    syncTrayLanguage(language);
    render();
  }

  get("settings").addEventListener("submit", event => { event.preventDefault(); return start("capture"); });
  for (const event of ["input", "change"]) {
    const changed = target => {
      state.dirty = true;
      state.errorKey = "";
      if (!target || target.id === "headset_host") {
        const manualHost = get("headset_host").value.trim();
        state.headsetCleared = false;
        state.selectedHeadset = manualHost ? "manual" : (
          state.discovered.length === 1 ? `${state.discovered[0].host}:${state.discovered[0].port}` : ""
        );
      }
      render();
    };
    get("settings").addEventListener(event, e => { if (!e.target || [...TEXT_FIELDS, ...NUMBER_FIELDS].includes(e.target.id)) changed(e.target); });
    get("page-preferences").addEventListener(event, e => { if (!e.target || [...TEXT_FIELDS, ...NUMBER_FIELDS].includes(e.target.id)) changed(e.target); });
  }
  get("discover").addEventListener("click", discover);
  get("headset-select").addEventListener("change", () => {
    const val = get("headset-select").value;
    state.selectedHeadset = val;
    state.headsetCleared = val === "";
    const device = state.discovered.find(item => `${item.host}:${item.port}` === val);
    if (device) applyDiscoveredEyePorts(device);
    state.errorKey = "";
    render();
  });
  for (const page of PAGES) get(`nav-${PAGE_NAV[page]}`).addEventListener("click", event => { event.preventDefault(); navigate(page); });
  navigation?.addEventListener("hashchange", hashChanged);
  get("language").addEventListener("change", () => setLanguage(get("language").value));
  get("preview-camera").addEventListener("change", switchCamera);
  get("preview-enabled").addEventListener("change", render);
  get("preview").addEventListener("error", () => { state.previewError = true; clearPreview(); render(); });
  for (const [id, fn] of [
    ["save", save],
    ["stop", stop],
    ["check-update", () => checkUpdates(true)],
    ["update-dismiss", () => { state.updateDismissed = true; render(); }],
    ["quit-service", quitService],
    ["cymple-relay-start", () => cympleRelay("start")],
    ["cymple-relay-stop", () => cympleRelay("stop")],
  ]) get(id).addEventListener("click", fn);
  for (const id of RELAY_OPTION_IDS) {
    for (const event of ["input", "change"]) {
      get(id).addEventListener(event, () => {
        state.relayDirty = true;
        if (id === "cymple-left-port" || id === "cymple-right-port") state.relayPortsManual = true;
        state.relayError = "";
        state.relayErrorKey = "";
        render();
      });
    }
  }

  async function init() {
    syncTrayLanguage(state.language);
    render();
    await pollStatus();
    if (!state.destroyed) {
      if (!doc.hidden) {
        startStatusInterval();
        startPreviewLoop();
      }
      void checkUpdates(false);
      await discover();
    }
  }

  function destroy() {
    state.destroyed = true;
    state.generation++;
    navigation?.removeEventListener("hashchange", hashChanged);
    doc.removeEventListener?.("visibilitychange", onVisibilityChange);
    pauseBackgroundLoops();
    clearPreview();
  }

  return {
    init, destroy, start, stop, save, switchCamera, cympleRelay, checkUpdates, quitService,
    readForm, pollStatus, refreshPreview, setLanguage, discover, navigate, state,
  };
}

if (typeof document !== "undefined") {
  let storage; try { storage = window.localStorage; } catch {}
  const tauriCore = window.__TAURI__?.core;
  const app = createApp(document, {
    storage,
    navigation: window,
    languages: navigator.languages ?? [navigator.language],
    ...(tauriCore?.invoke ? {fetch: createTauriFetcher(tauriCore), tauriCore} : {}),
  });
  void app.init();
  window.addEventListener("pagehide", () => app.destroy(), {once: true});
}
