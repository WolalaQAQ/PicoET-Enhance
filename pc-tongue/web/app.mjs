import {errorMessage, initialLanguage, LANGUAGE_KEY, messages} from "./i18n.mjs";

const TEXT_FIELDS = ["headset_host", "model_path", "device"];
const NUMBER_FIELDS = ["headset_port", "smoothing_ms", "uncertain_hold_ms", "stale_ms", "preview_fps", "output_port"];
const CHECK_FIELDS = ["output_enabled", "size_hysteresis"];
const LABELS = {title: "title", "host-label": "host", "model-label": "model", start: "start", stop: "stop",
  "out-label": "out", "left-label": "left", "right-label": "right", "advanced-title": "advanced",
  "diagnostics-title": "diagnostics", "port-label": "port", "device-label": "device", "smoothing-label": "smoothing",
  "hold-label": "hold", "expiry-label": "expiry", "fps-label": "fps", "udp-label": "udp",
  "output-host-label": "outputHost", "output-port-label": "outputPort", "settings-help": "settingsHelp",
  save: "save", replay: "replay", "preview-label": "preview", "engine-label": "engine", "source-label": "source",
  "result-label": "result", "source-age-label": "sourceAge", "receive-age-label": "receiveAge", "rate-label": "rates",
  "drop-label": "drops", "order-label": "order", "queue-label": "queue", "decode-label": "decode",
  "inference-label": "inference", "pc-label": "pc", "latency-label": "latency", "clock-label": "clock", "mask-label": "mask",
  "providers-label": "providers", "fallback-label": "fallback", "personal-title": "personalTitle",
  "personal-help": "personalHelp", "hysteresis-label": "hysteresis", calibrate: "calibrate",
  "personal-cancel": "personalCancel", "restore-standard": "restoreStandard", "voice-label": "voice",
  "retrain-title": "retrainTitle", "retrain-warning": "retrainWarning", retrain: "retrain", "consent-label": "consent"};
const display = value => value === undefined || value === null || value === "" ? "—" : String(value);
const fixed = (value, digits = 1) => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
const ms = value => `${fixed(value)} ms`;
const percentiles = value => value ? `${fixed(value.p50)} / ${fixed(value.p95)} ms` : "—";

export async function requestJson(fetcher, path, body, timeoutMs = 3500) {
  const method = body === undefined ? "GET" : "POST";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetcher(path, {method, cache: "no-store", signal: controller.signal,
      ...(body === undefined ? {} : {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})});
    const text = await response.text();
    let data;
    try { data = text ? JSON.parse(text) : null; }
    catch { throw new Error(`${method} ${path} · HTTP ${response.status}: ${text.slice(0, 240)}`); }
    if (!response.ok) throw new Error(`${method} ${path} · HTTP ${response.status}: ${data?.error || text}`);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error(`${method} ${path}: request timed out (${timeoutMs} ms)`);
    throw error;
  } finally { clearTimeout(timeout); }
}

export function deriveView(status, online, ageMs = 0) {
  const running = status?.running === true;
  const connection = status?.connected === true || running && status?.source_mode === "replay";
  const current = online && ageMs < 1500;
  const p = status?.prediction;
  const valid = current && running && connection && p?.fresh === true && p?.override === true &&
    p?.stale !== true &&
    status?.metrics?.source_fresh !== false && status?.source?.fresh !== false &&
    !["stale", "invalid", "stopped"].includes(p?.state) &&
    !(Number(status?.metrics?.receive_age_ms) > Number(status?.config?.stale_ms));
  return {running, current, valid, key: !current ? "offline" : !running ? "stopped" : !connection ? "connecting" : valid ? "running" : "stale"};
}

export function createApp(doc, options = {}) {
  const fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);
  const timers = options.timers ?? globalThis;
  const now = options.now ?? Date.now;
  const urls = options.urlApi ?? globalThis.URL;
  const storage = options.storage;
  const speech = options.speech ?? globalThis.speechSynthesis;
  const Utterance = options.Utterance ?? globalThis.SpeechSynthesisUtterance;
  const get = id => doc.getElementById(id);
  const state = {language: initialLanguage(storage, options.languages ?? []), config: null, status: null,
    online: false, at: 0, busy: "", dirty: false, error: "", pollError: "", errorKey: "", destroyed: false,
    polling: false, generation: 0, interval: null, previewTimer: null, previewBusy: false,
    previewGeneration: 0, previewAbort: null, previewUrl: null, previewError: false, previewOwner: null, spoken: null};
  const t = key => messages[state.language][key];
  const view = () => deriveView(state.status, state.online, now()-state.at);

  function renderPersonal(current) {
    const personal = state.status?.personalization;
    const active = ["collecting", "training", "applying"].includes(personal?.state);
    const ready = current.valid && state.status?.source_mode === "live" && !active &&
      !personal?.training_worker_alive && !state.busy && !state.dirty;
    get("calibrate").disabled = !ready;
    get("retrain").disabled = !ready || !get("record-consent").checked;
    get("personal-cancel").disabled = !current.current || !active || Boolean(state.busy);
    get("restore-standard").disabled = !current.current || Boolean(state.busy);
    get("record-consent").disabled = active || Boolean(state.busy);
    const cal = personal?.profile?.calibration;
    const standardCal = cal && cal.left === .33 && cal.center === .66 && cal.right === 1.11 && cal.min_area === 100;
    get("profile-info").textContent = personal ? `${t(personal.profile?.head ? "personalProfile" : "standardProfile")} · ${t(standardCal ? "standardCalibration" : "personalCalibration")} · ${t("calibrationValues")}: ${cal ? `${fixed(cal.left, 3)} / ${fixed(cal.center, 3)} / ${fixed(cal.right, 3)} rad; ${cal.min_area}` : "—"}` : "—";
    get("personal-state").textContent = !current.current ? t("personalOffline") : t(`personal_${personal?.state ?? "idle"}`);
    const wizard = personal?.wizard;
    const showGuide = current.current && active;
    get("guide").hidden = !showGuide;
    get("guide-step").textContent = wizard ? t(`step_${wizard.step}`) : t("trainingProgress");
    get("guide-hint").textContent = wizard ? t(`hint_${wizard.hint}`) || t(`step_${wizard.step}`) : "";
    get("guide-progress").value = wizard?.progress ?? personal?.progress ?? 0;
    get("guide-progress").setAttribute("aria-label", t("trainingProgress"));
    get("guide-count").textContent = wizard ? `${wizard.collected} / ${wizard.target} ${t("samples")}` : `${fixed((personal?.progress ?? 0)*100, 0)}%`;
    get("personal-error").hidden = !personal?.error;
    get("personal-error").textContent = personal?.error ? errorMessage(state.language, personal.error) : "";
    const metrics = personal?.metrics;
    get("training-metrics").textContent = metrics ? `${t("qualityMetrics")}: ${fixed(metrics.before?.score, 3)} / ${fixed(metrics.before?.fp, 3)} → ${fixed(metrics.after?.score, 3)} / ${fixed(metrics.after?.fp, 3)}` : "";
    const voiceAvailable = Boolean(speech && Utterance);
    get("voice-enabled").disabled = !voiceAvailable;
    get("voice-info").textContent = t(!voiceAvailable ? "voiceUnavailable" : get("voice-enabled").checked ? "voiceReady" : "voiceOff");
    const prompt = wizard ? `step_${wizard.step}` : `personal_${personal?.state}`;
    const announce = current.current && voiceAvailable && get("voice-enabled").checked && personal?.job_id &&
      (active || ["calibrated", "accepted", "rejected", "cancelled", "restored", "failed"].includes(personal?.state));
    const key = announce ? `${personal.job_id}/${prompt}/${state.language}` : null;
    if (key !== state.spoken) {
      speech?.cancel();
      state.spoken = key;
      if (key) {
        const utterance = new Utterance(t(prompt));
        utterance.lang = state.language === "en" ? "en-US" : "zh-CN";
        speech.speak(utterance);
      }
    }
  }

  function clearPreview() {
    state.previewGeneration++;
    state.previewAbort?.abort();
    get("preview").hidden = true;
    get("preview").removeAttribute("src");
    if (state.previewUrl) urls.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
    get("placeholder").hidden = false;
  }
  function render() {
    const current = view();
    doc.documentElement.lang = state.language;
    doc.title = t("title");
    for (const [id, key] of Object.entries(LABELS)) get(id).textContent = t(key);
    get("language").value = state.language;
    get("language").setAttribute("aria-label", t("language"));
    get("preview").setAttribute("alt", t("previewAlt"));
    get("outputs").setAttribute("aria-label", t("output"));
    const statusKey = current.running && current.key === "running" && state.status.source_mode === "replay" ? "replaying" : current.key;
    get("status").textContent = t(state.busy || (!state.config && !state.pollError ? "loading" : statusKey));
    get("start").disabled = !state.config || !current.current || Boolean(state.busy) || current.running;
    get("stop").disabled = Boolean(state.busy) || !current.running;
    get("replay").disabled = get("start").disabled;
    get("save").disabled = !state.config || Boolean(state.busy) || !state.dirty;
    for (const id of [...TEXT_FIELDS, ...NUMBER_FIELDS, ...CHECK_FIELDS]) get(id).disabled = Boolean(state.busy) || !state.config;
    get("config-state").textContent = state.config ? t(state.dirty ? "unsaved" : "saved") : "";
    const backendError = state.status?.last_error;
    const error = state.error || state.pollError || (typeof backendError === "string" ? backendError : backendError ? JSON.stringify(backendError) : "");
    get("error").hidden = !error && !state.errorKey;
    get("error").textContent = state.errorKey ? t(state.errorKey) : error ? errorMessage(state.language, error) : "";
    get("placeholder").textContent = t(!get("preview-enabled").checked ? "previewOff" : state.previewError ? "previewError" : "noVideo");
    for (const key of ["out", "left", "right"]) {
      const value = state.status?.prediction?.[key];
      get(key).textContent = current.valid && Number.isFinite(value) ? Math.max(0, Math.min(1, value)).toFixed(3) : "—";
      get(`bar-${key}`).value = current.valid && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
      get(`bar-${key}`).setAttribute("aria-label", t(key));
    }
    const owner = `${state.status?.source?.session_id ?? ""}/${state.status?.output_session_id ?? ""}`;
    if (state.previewOwner !== owner) { clearPreview(); state.previewOwner = owner; }
    if (!current.valid || state.busy || !get("preview-enabled").checked) clearPreview();
    const source = state.status?.source ?? {}, result = state.status?.prediction_source ?? {};
    const identity = value => typeof value === "number" && !Number.isSafeInteger(value) ? t("unsafeInteger") : display(value);
    get("source-info").textContent = `${identity(source.session_id)} / ${identity(source.source_seq)}`;
    get("result-info").textContent = `${identity(result.session_id)} / ${identity(result.source_seq)}`;
    get("raw-identity").textContent = identity(source.raw_identity);
    for (const [id, key] of [["time-eye0", "eye0_sensor_ns"], ["time-eye1", "eye1_sensor_ns"], ["time-mouth", "mouth_sensor_ns"], ["time-sensor", "sensor_ns"], ["time-vendor", "vendor_ns"]]) get(id).textContent = identity(source[key]);
    const metrics = current.current ? state.status?.metrics ?? {} : {};
    get("engine-info").textContent = `${display(state.status?.engine)} / ${display(state.status?.device)}`;
    get("provider-info").textContent = state.status?.model?.providers?.join(" → ") || "—";
    const fallbacks = state.status?.model?.fallbacks ?? [];
    get("fallback-info").textContent = fallbacks.length ? fallbacks.map(row => `${row.engine}/${row.device}: ${row.error}`).join("\n") : t("noFallback");
    get("source-age").textContent = ms(metrics.source_age_ms);
    get("receive-age").textContent = ms(metrics.receive_age_ms);
    get("rates").textContent = `${fixed(metrics.received_hz)} / ${fixed(metrics.inference_hz)} Hz`;
    get("drops").textContent = `${display(metrics.source_gaps)} / ${display(metrics.pending_dropped)}`;
    get("order").textContent = `${display(metrics.duplicates)} / ${display(metrics.out_of_order)}`;
    get("queue").textContent = `${display(metrics.queue_depth)} / ${ms(metrics.queue_ms)}`;
    const sameClock = current.current && source.encode_end_ns != null && source.encode_begin_ns != null &&
      !(typeof source.encode_end_ns === "number" && !Number.isSafeInteger(source.encode_end_ns)) &&
      !(typeof source.encode_begin_ns === "number" && !Number.isSafeInteger(source.encode_begin_ns));
    get("encode-label").textContent = t(state.status?.source_mode === "replay" ? "encodeReplay" : "encode");
    get("encode-time").textContent = ms(metrics.encode_ms ?? (sameClock ? Number(BigInt(source.encode_end_ns)-BigInt(source.encode_begin_ns))/1e6 : null));
    for (const [id, key] of [["decode-time", "decode_ms"], ["inference-time", "predict_ms"], ["latency", "source_copy_to_result_ms"]]) get(id).textContent = percentiles(metrics.latency_ms?.[key]);
    get("pc-time").textContent = ms(metrics.pc_total_ms);
    get("clock").textContent = ms(metrics.clock_uncertainty_ms);
    const prediction = current.current ? state.status?.prediction : null;
    get("mask-info").textContent = `${display(prediction?.mask_area)} / ${fixed(prediction?.angle, 3)} rad`;
    get("snapshot-state").textContent = t(current.current ? "snapshot" : "expiredSnapshot");
    const replay = state.status?.replay;
    get("replay-info").textContent = replay?.path ? `${replay.path} · ${display(replay.position)} / ${display(replay.total)} ${t("frames")}${replay.finished ? ` · ${t("replayDone")}` : ""}` : t("replayReady");
    renderPersonal(current);
  }
  function applyConfig(config) {
    state.config = config;
    if (!state.dirty) {
      for (const key of [...TEXT_FIELDS, ...NUMBER_FIELDS]) get(key).value = config[key] == null ? "" : String(config[key]);
      for (const key of CHECK_FIELDS) get(key).checked = config[key] === true;
      get("output_host").value = "127.0.0.1";
    }
  }
  async function pollStatus() {
    if (state.polling || state.busy || state.destroyed) return;
    state.polling = true;
    const generation = state.generation;
    try {
      const status = await requestJson(fetcher, "/api/status");
      const config = state.config ?? await requestJson(fetcher, "/api/config");
      if (generation !== state.generation || state.destroyed) return;
      state.status = status;
      applyConfig(status.config ?? config);
      state.online = true; state.at = now(); state.pollError = "";
    } catch (error) {
      if (generation !== state.generation || state.destroyed) return;
      state.online = false; state.pollError = error.message;
    } finally {
      state.polling = false;
      if (!state.destroyed) render();
    }
  }
  function readForm() {
    const config = Object.fromEntries(TEXT_FIELDS.map(key => [key, get(key).value.trim()]));
    for (const key of NUMBER_FIELDS) config[key] = Number(get(key).value);
    for (const key of CHECK_FIELDS) config[key] = get(key).checked;
    config.output_host = "127.0.0.1";
    return config;
  }
  function validateForm() {
    if (!get("headset_host").value.trim() || !get("model_path").value.trim()) state.errorKey = "required";
    else if (!get("settings").checkValidity()) state.errorKey = "invalidSettings";
    else return true;
    render(); return false;
  }
  async function persistConfig() {
    const saved = await requestJson(fetcher, "/api/config", readForm(), 20000);
    state.dirty = false; applyConfig(saved);
  }
  async function save() {
    if (get("save").disabled || state.destroyed || !validateForm()) return;
    state.busy = "saving"; state.generation++; state.error = ""; state.errorKey = ""; render();
    try {
      await persistConfig();
      state.status = await requestJson(fetcher, "/api/status");
      state.online = true; state.at = now(); state.pollError = "";
    } catch (error) { state.error = error.message; state.online = false; }
    finally { state.busy = ""; if (!state.destroyed) render(); }
  }
  async function start(mode = "live") {
    if (get("start").disabled || state.destroyed || !validateForm()) return;
    state.busy = "starting"; state.generation++; state.error = ""; state.errorKey = ""; render();
    try {
      await persistConfig();
      state.status = await requestJson(fetcher, "/api/start", {mode}, 60000);
      state.dirty = false; state.online = true; state.at = now(); state.pollError = "";
    } catch (error) { state.error = error.message; state.online = false; }
    finally { state.busy = ""; if (!state.destroyed) render(); }
  }
  async function stop() {
    if (get("stop").disabled || state.destroyed) return;
    state.busy = "stopping"; state.generation++; state.error = ""; state.errorKey = ""; render();
    try {
      state.status = await requestJson(fetcher, "/api/stop", {}, 20000);
      state.online = true; state.at = now(); state.pollError = "";
    } catch (error) { state.error = error.message; state.online = false; }
    finally { state.busy = ""; if (!state.destroyed) render(); }
  }
  async function personalize(action) {
    const id = {calibrate: "calibrate", retrain: "retrain", cancel: "personal-cancel", restore: "restore-standard"}[action];
    if (!id || get(id).disabled || state.destroyed) return;
    state.busy = "personalWorking"; state.generation++; state.error = ""; state.errorKey = ""; render();
    try {
      await requestJson(fetcher, "/api/personalization", {action, ...(action === "retrain" ? {confirmed: get("record-consent").checked} : {})}, 20000);
      get("record-consent").checked = false;
      state.status = await requestJson(fetcher, "/api/status");
      state.online = true; state.at = now(); state.pollError = "";
    } catch (error) { state.error = error.message; }
    finally { state.busy = ""; if (!state.destroyed) render(); }
  }
  async function refreshPreview() {
    if (state.destroyed || state.busy || state.previewBusy || doc.hidden || !get("preview-enabled").checked || !view().valid) return;
    state.previewBusy = true;
    const generation = state.previewGeneration;
    const controller = new AbortController(); state.previewAbort = controller;
    const timeout = timers.setTimeout(() => controller.abort(), 2500);
    try {
      const response = await fetcher(`/api/preview.jpg?t=${now()}`, {cache: "no-store", signal: controller.signal});
      if (generation !== state.previewGeneration || state.destroyed || !view().valid) return;
      if (response.status === 204) { clearPreview(); return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      if (generation !== state.previewGeneration || state.destroyed || !view().valid) return;
      const url = urls.createObjectURL(blob);
      if (state.previewUrl) urls.revokeObjectURL(state.previewUrl);
      state.previewUrl = url; state.previewError = false;
      get("preview").src = url; get("preview").hidden = false; get("placeholder").hidden = true;
    } catch {
      if (generation === state.previewGeneration && !state.destroyed) { state.previewError = true; clearPreview(); render(); }
    } finally { timers.clearTimeout(timeout); state.previewBusy = false; state.previewAbort = null; }
  }
  function previewLoop() {
    void refreshPreview();
    const fps = Math.min(10, Math.max(1, Number(state.config?.preview_fps) || 10));
    if (!state.destroyed) state.previewTimer = timers.setTimeout(previewLoop, 1000/fps);
  }
  function setLanguage(language) {
    if (!Object.hasOwn(messages, language)) return;
    state.language = language;
    try { storage?.setItem(LANGUAGE_KEY, language); } catch {}
    render();
  }
  get("settings").addEventListener("submit", event => { event.preventDefault(); return start(); });
  get("settings").addEventListener("input", () => { state.dirty = true; state.errorKey = ""; render(); });
  get("settings").addEventListener("change", () => { state.dirty = true; state.errorKey = ""; render(); });
  get("language").addEventListener("change", () => setLanguage(get("language").value));
  get("stop").addEventListener("click", stop);
  get("save").addEventListener("click", save);
  get("replay").addEventListener("click", () => start("replay"));
  get("preview-enabled").addEventListener("change", render);
  get("record-consent").addEventListener("change", render);
  get("voice-enabled").addEventListener("change", render);
  for (const [id, action] of [["calibrate", "calibrate"], ["retrain", "retrain"], ["personal-cancel", "cancel"], ["restore-standard", "restore"]])
    get(id).addEventListener("click", () => personalize(action));
  get("preview").addEventListener("error", () => { state.previewError = true; clearPreview(); render(); });
  async function init() {
    render(); await pollStatus();
    if (state.destroyed) return;
    state.interval = timers.setInterval(() => { render(); void pollStatus(); }, 500);
    previewLoop();
  }
  function destroy() {
    state.destroyed = true; state.generation++;
    speech?.cancel();
    timers.clearInterval(state.interval); timers.clearTimeout(state.previewTimer); clearPreview();
  }
  return {init, destroy, start, stop, save, personalize, readForm, pollStatus, refreshPreview, setLanguage, state};
}

if (typeof document !== "undefined") {
  let storage;
  try { storage = window.localStorage; } catch {}
  const app = createApp(document, {storage, languages: navigator.languages ?? [navigator.language]});
  void app.init();
  window.addEventListener("pagehide", () => app.destroy(), {once: true});
}
