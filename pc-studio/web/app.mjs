import {DIAGNOSTIC_GROUPS, errorMessage, initialLanguage, LANGUAGE_KEY, messages, MORPH_GROUPS, PINNED_CHANNELS_KEY} from "./i18n.mjs";

const TEXT_FIELDS = ["headset_host", "model_path", "device", "replay_path"];
const FILTER_NUMBER_FIELDS = ["filter_min_cutoff", "filter_beta", "filter_d_cutoff"];
const NUMBER_FIELDS = ["headset_port", "stale_ms", "preview_fps", "output_port", ...FILTER_NUMBER_FIELDS];
const CHECK_FIELDS = ["output_enabled", "filter_enabled"];
const RELAY_OPTION_IDS = ["cymple-host", "cymple-port", "cymple-left-port", "cymple-right-port", "cymple-left-rotate", "cymple-left-hflip", "cymple-left-vflip", "cymple-right-rotate", "cymple-right-hflip", "cymple-right-vflip"];
export const PRIMARY_CHANNELS = ["JawOpen", "MouthClose", "MouthPucker", "LipFunnelLower", "MouthSmileL", "MouthSmileR", "MouthRollLower", "TongueOut"];
export const EXPRESSION_POLL_MS = 33;
export const STOCK_CHANNELS = ["CheekPuff", "CheekSquintL", "CheekSquintR", "JawForward", "JawLeft", "JawOpen", "JawRight", "MouthClose", "MouthDimpleL", "MouthDimpleR", "MouthFrownL", "MouthFrownR", "MouthFunnel", "MouthLeft", "MouthLowerDownL", "MouthLowerDownR", "MouthPressL", "MouthPressR", "MouthPucker", "MouthRight", "MouthRollLower", "MouthRollUpper", "MouthShrugLower", "MouthShrugUpper", "MouthSmileL", "MouthSmileR", "MouthStretchL", "MouthStretchR", "MouthUpperUpL", "MouthUpperUpR", "NoseSneerL", "NoseSneerR", "TongueOut"];
export const CURATED_MODEL_TARGETS = [
  "TongueLeft", "TongueRight", "TongueUp", "TongueDown", "TongueRoll",
  "CheekSuckLeft", "CheekSuckRight", "MouthTightenerLeft", "MouthTightenerRight",
  "TongueOut", "CheekPuffLeft", "CheekPuffRight",
  "MouthSmileL", "MouthSmileR", "MouthFrownL", "MouthFrownR", "MouthUpperUpL", "MouthUpperUpR",
];
const CURATED_MODEL_SET = new Set(CURATED_MODEL_TARGETS);
const STOCK_AGGREGATE_PARENTS = {CheekPuffLeft: "CheekPuff", CheekPuffRight: "CheekPuff", LipFunnelUpper: "MouthFunnel", LipFunnelLower: "MouthFunnel", MouthUpperLeft: "MouthLeft", MouthLowerLeft: "MouthLeft", MouthUpperRight: "MouthRight", MouthLowerRight: "MouthRight"};
const AGGREGATE_CHILDREN_MAP = {CheekPuff: ["CheekPuffLeft", "CheekPuffRight"], MouthFunnel: ["LipFunnelUpper", "LipFunnelLower"], MouthLeft: ["MouthUpperLeft", "MouthLowerLeft"], MouthRight: ["MouthUpperRight", "MouthLowerRight"]};
const DEFAULT_DIAG_CHANNELS = DIAGNOSTIC_GROUPS.flatMap(group => group.items.map(item => item.name));
const MORPH_META = new Map(DIAGNOSTIC_GROUPS.flatMap(group => group.items.map(item => [item.name, item])));
export const ROUTABLE_CHANNELS = MORPH_GROUPS.flatMap(group => group.items.map(item => item.name));
export const CURATED_STOCK_TARGETS = ROUTABLE_CHANNELS.filter(name => !CURATED_MODEL_SET.has(name));
const PURE_EXTENDED_SET = new Set(MORPH_GROUPS.flatMap(group => group.items.filter(item => item.tag === "missing").map(item => item.name)));
const PRESET_MODES = ["curated", "hybrid", "model_only"];
export const MOUTH_PRESETS = {
  curated: Object.fromEntries(ROUTABLE_CHANNELS.map(name => [name, CURATED_MODEL_SET.has(name) ? "model" : "stock"])),
  hybrid: Object.fromEntries(ROUTABLE_CHANNELS.map(name => [name, PURE_EXTENDED_SET.has(name) ? "model" : "stock"])),
  model_only: Object.fromEntries(ROUTABLE_CHANNELS.map(name => [name, "model"])),
};
export function matchingPreset(sources) {
  for (const mode of PRESET_MODES) {
    const target = MOUTH_PRESETS[mode];
    if (ROUTABLE_CHANNELS.every(name => sources?.[name] === target[name])) return mode;
  }
  return "custom";
}
export function resolveChannelSources(mouthMode = "curated", hybridChannels = null, channelSources = null) {
  const baseKey = PRESET_MODES.includes(mouthMode) ? mouthMode : "curated";
  const resolved = {...MOUTH_PRESETS[baseKey]};
  if (!channelSources && mouthMode === "hybrid" && Array.isArray(hybridChannels)) {
    const allowed = new Set(hybridChannels);
    for (const name of ROUTABLE_CHANNELS) {
      if (PURE_EXTENDED_SET.has(name)) continue;
      const stockKey = STOCK_AGGREGATE_PARENTS[name] ?? name;
      resolved[name] = allowed.has(stockKey) ? "stock" : "model";
    }
  }
  if (channelSources && typeof channelSources === "object") {
    for (const name of ROUTABLE_CHANNELS) {
      const value = channelSources[name];
      const valid = PURE_EXTENDED_SET.has(name) ? (value === "model" || value === "off") : (value === "stock" || value === "model" || value === "off");
      if (valid) resolved[name] = value;
    }
  }
  return resolved;
}
export const PAGES = ["tracking", "recording", "training", "diagnostics", "preferences"];
const PAGE_NAV = {tracking: "workspace", recording: "recording", training: "training", diagnostics: "diagnostics", preferences: "advanced"};
export function routePage(hash = "") { const page = hash.replace(/^#\/?/, ""); return PAGES.includes(page) ? page : "tracking"; }
const LABELS = {title: "title", "host-label": "host", "model-label": "model", "model-help": "modelHelp", start: "start", capture: "capture", stop: "stop",
  "advanced-title": "advanced", "diagnostics-title": "diagnostics", "port-label": "port", "device-label": "device", "expiry-label": "expiry", "expiry-help": "expiryHelp", "fps-label": "fps",
  "filtering-title": "filteringTitle", "filtering-help": "filteringHelp", "filter-enabled-label": "filterEnabled",
  "filter-min-cutoff-label": "filterMinCutoff", "filter-min-cutoff-help": "filterMinCutoffHelp",
  "filter-beta-label": "filterBeta", "filter-beta-help": "filterBetaHelp",
  "filter-d-cutoff-label": "filterDCutoff", "filter-d-cutoff-help": "filterDCutoffHelp",
  "inference-settings-title": "inferenceSettingsTitle", "inference-settings-help": "inferenceSettingsHelp", "connection-settings-help": "connectionSettingsHelp",
  "udp-label": "udp", "output-port-label": "outputPort", "settings-help": "settingsHelp", save: "save", replay: "replay", "replay-path-label": "replayPath",
  "preview-label": "preview", "all-channels-title": "allChannels", "unknown-help": "unknownHelp", migration: "migration",
  "recording-title": "recordingTitle", "record-help": "recordHelp", "person-label": "person", "guided-label": "guided", "voice-label": "voice", "consent-label": "consent",
  "record-start": "recordStart", "record-stop": "recordStop", "record-cancel": "cancel", "training-title": "trainingTitle", "training-help": "trainingHelp",
  "guide-ready": "guideReady", "guide-finish": "guideFinish", "guide-confirm": "guideConfirm", "guide-skip": "guideSkip",
  "guide-prev": "guidePrevStep", "guide-next": "guideNextStep", "guide-select-label": "guideSelect",
  "guide-mode-label": "guideMode", "guide-auto-option": "guideAuto", "guide-manual-option": "guideManual",
  "guide-prepare-label": "guidePrepareSeconds", "record-continue": "guideContinue",
  "label-stage-label": "labelStage", "raw-only-option": "rawOnly", "instant-option": "instantLabels",
  "receive-results-label": "receiveResults", "receive-results-hint": "receiveResultsHint",
  "review-title": "reviewTitle", "review-help": "reviewHelp", "review-all-label": "reviewAll", "review-load": "reviewLoad", "review-prev": "previous", "review-next": "next", "review-select-page": "selectPage",
  "review-proposal": "reviewProposal", "review-targets-label": "reviewTargets", "reviewer-label": "reviewer", "observation-label": "observation", "visible-option": "visible", "uncertain-option": "uncertain", "occluded-option": "occluded", "no-mouth-option": "noMouth", "review-consent-label": "reviewConsent", "review-apply": "reviewApply",
  "train-action-label": "trainAction", "train-start": "trainStart", "train-cancel": "cancel", "engine-label": "engine", "providers-label": "providers",
  "fallback-label": "fallback", "source-label": "source", "result-label": "result", "source-age-label": "sourceAge", "receive-age-label": "receiveAge",
  "rate-label": "rates", "drop-label": "drops", "queue-label": "queue", "inference-label": "inference", "latency-label": "latency", "stock-label": "stock",
  "reference-title": "referenceTitle", "reference-select-label": "referenceSelect", "reference-prev": "previousPose", "reference-next": "nextPose",
  "reference-neutral-caption": "referenceNeutral", "reference-target-caption": "referenceTarget", "reference-profile-caption": "referenceProfile",
  "reference-directions": "referenceDirections", "reference-details-title": "referenceDetails", "reference-license": "referenceLicense",
  "nav-caption": "navCaption", "nav-live-label": "navLive", "nav-record-label": "navRecord", "nav-train-label": "navTrain", "nav-diagnostics-label": "navDiagnostics", "nav-settings-label": "navSettings",
  "local-label": "localLabel", "privacy-note": "privacyNote", "workspace-caption": "workspaceCaption", "page-eyebrow": "pageEyebrow", "page-intro": "pageIntro",
  "headset-title": "headsetTitle", "headset-select-label": "headsetSelect", "model-path-label": "modelPath", "run-help": "runHelp", "go-diagnostics": "goDiagnostics",
  "settings-diag-text": "settingsDiagText", "settings-go-diagnostics": "settingsGoDiagnostics",
  "diag-tab-expressions-label": "diagTabExpressions", "diag-tab-connection-label": "diagTabConnection",
  "expr-active-only": "exprActiveOnly", "expr-compare-toggle": "exprCompareToggle", "expr-src-all": "exprSrcAll", "expr-src-native": "exprSrcNative", "expr-src-model": "exprSrcModel", "expr-empty": "exprEmptyFilter",
  "expr-pins-clear": "exprPinsClear", "expr-pins-reset": "exprPinsReset", "expr-pins-empty": "exprPinsEmpty",
  "conn-card-rates-title": "connCardRates", "conn-card-engine-title": "connCardEngine", "conn-card-stream-title": "connCardStream", "conn-card-session-title": "connCardSession", "live-stock-label": "liveStock",
  "camera-eyebrow": "cameraEyebrow", "camera-title": "cameraTitle", "camera-empty-help": "cameraEmptyHelp", "camera-privacy": "cameraPrivacy",
  "output-eyebrow": "outputEyebrow", "output-title": "outputTitle", "output-note": "outputNote", "footer-note": "footerNote",
  "capture-stop": "closeCamera", "first-model-text": "firstModelText", "go-capture": "goCapture",
  "reference-picker-title": "referencePicker", "record-options-title": "recordOptions", "replay-tools-title": "replayTools",
  "train-go-record": "goCapture", "train-go-review": "goReview",
  "train-step-one": "trainStepOne", "train-step-one-help": "trainStepOneHelp", "train-step-two": "trainStepTwo", "train-step-two-help": "trainStepTwoHelp",
  "train-step-three": "trainStepThree", "train-step-three-help": "trainStepThreeHelp", "training-tools-title": "trainingTools", "training-tools-help": "trainingToolsHelp", "technical-error-title": "technicalError",
  "cymple-relay-eyebrow": "cympleRelayEyebrow", "cymple-relay-title": "cympleRelayTitle", "cymple-relay-help": "cympleRelayHelp",
  "cymple-relay-start": "cympleRelayStart", "cymple-relay-stop": "cympleRelayStop", "cymple-left-title": "cympleLeftEye", "cymple-right-title": "cympleRightEye",
  "cymple-relay-options-title": "cympleRelayOptions", "cymple-host-label": "cympleHost", "cymple-port-label": "cymplePort",
  "cymple-left-port-label": "cympleLeftPort", "cymple-right-port-label": "cympleRightPort",
  "cymple-left-orient-title": "cympleLeftEye", "cymple-right-orient-title": "cympleRightEye",
  "cymple-left-rotate-label": "cympleRotate", "cymple-right-rotate-label": "cympleRotate",
  "cymple-left-hflip-label": "cympleHflip", "cymple-right-hflip-label": "cympleHflip",
  "cymple-left-vflip-label": "cympleVflip", "cymple-right-vflip-label": "cympleVflip",
  "mouth-mode-label": "mouthMode", "mouth-mode-help": "mouthModeHelp",
  "preset-curated-title": "presetCuratedTitle", "preset-curated-desc": "presetCuratedDesc",
  "preset-hybrid-title": "presetHybridTitle", "preset-hybrid-desc": "presetHybridDesc",
  "preset-model-title": "presetModelTitle", "preset-model-desc": "presetModelDesc",
  "hybrid-channels-title": "hybridChannelsTitle", "hybrid-channels-help": "hybridChannelsHelp",
  "filter-all": "filterAll", "filter-stock": "filterStock", "filter-model": "filterModel", "filter-good": "filterGood"};
const MOVING_STEPS = new Set(["tongue_sweep", "negative_pucker_release", "negative_speech"]);
const display = x => x == null || x === "" ? "—" : String(x);
const fixed = (x, n = 1) => x != null && Number.isFinite(Number(x)) ? Number(x).toFixed(n) : "—";
const percentile = x => x ? `${fixed(x.p50)} / ${fixed(x.p95)} ms` : "—";
export async function requestJson(fetcher, path, body, timeoutMs = 3500) {
  const method = body === undefined ? "GET" : "POST", controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetcher(path, {method, cache: "no-store", signal: controller.signal,
      ...(body === undefined ? {} : {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})});
    const text = await response.text(); let data;
    try { data = text ? JSON.parse(text) : null; } catch { throw Error(`${method} ${path} · HTTP ${response.status}: ${text.slice(0, 240)}`); }
    if (!response.ok) throw Error(`${method} ${path} · HTTP ${response.status}: ${data?.error || text}`);
    return data;
  } catch (error) { if (error.name === "AbortError") throw Error(`${method} ${path}: request timed out (${timeoutMs} ms)`); throw error; }
  finally { clearTimeout(timeout); }
}
export function deriveView(status, online, ageMs = 0) {
  const running = status?.running === true, current = online && ageMs < 1500;
  const connection = status?.connected === true || running && status?.source_mode === "replay";
  const video = current && running && connection && status?.metrics?.video_fresh === true;
  const valid = video && status?.prediction?.fresh === true && status?.prediction?.state === "valid" && status?.metrics?.source_fresh === true;
  return {running, current, video, valid, key: !current ? "offline" : !running ? "stopped" : !connection ? "connecting" :
    status?.capture_only ? "recordingOnly" : valid ? "running" : "stale"};
}
export function channelValue(status, name, valid) {
  if (!valid || (!status?.model?.trained_channels?.includes(name) && !status?.prediction?.native_channels?.includes(name))) return null;
  const i = status.contract?.channels?.indexOf(name) ?? -1;
  if (i < 0 || !(BigInt(status.prediction.valid_mask ?? 0) & (1n << BigInt(i)))) return null;
  const value = status.prediction.channels?.[name];
  return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
}
export function stockChannelValue(status, name, valid) {
  if (!valid) return null;
  const stockMap = status?.prediction?.stock_channels;
  if (stockMap && Object.hasOwn(stockMap, name)) {
    const value = stockMap[name];
    return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
  }
  if (status?.prediction?.native_channels?.includes(name)) return channelValue(status, name, valid);
  return null;
}
export function modelChannelValue(status, name, valid) {
  if (!valid || !status?.model?.trained_channels?.includes(name)) return null;
  const modelMap = status?.prediction?.model_channels;
  if (modelMap) {
    const i = status.contract?.channels?.indexOf(name) ?? -1;
    if (i >= 0 && status.prediction.model_valid_mask != null &&
        !(BigInt(status.prediction.model_valid_mask) & (1n << BigInt(i)))) return null;
    const value = modelMap[name];
    return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
  }
  if (!status?.prediction?.native_channels?.includes(name)) return channelValue(status, name, valid);
  return null;
}
export function captureProgress(status, preset, preview = false) {
  const external = status?.external_guide, recording = status?.recording;
  const active = external?.running === true || ["recording","collecting"].includes(recording?.state);
  const guide = !preview && (external && preset?.id === external.case ? external :
    recording?.guide && (active || recording.guide.preset_id === preset?.id) ? recording.guide : null);
  if (active && !guide) return {total:0,current:0,active,phaseKey:"captureFree"};
  const total = guide?.steps?.length ?? guide?.total ?? preset?.steps?.length ?? 0;
  const phase = guide?.phase ?? "practice", done = ["done","finished"].includes(phase);
  const current = total && guide && phase !== "practice" ? done ? total : Math.min((guide.index ?? 0)+1,total) : 0;
  const phaseKey = !guide ? "captureIdle" : guide.paused || recording?.state === "cancelled" ? "capturePaused" :
    !active && !external && !done ? "capturePaused" :
    ({prepare:"capturePrepare",hold:"captureHold",review:"captureReview",finished:"visualdone"})[phase] ?? "visual"+phase;
  return {total,current,active,phaseKey};
}
export function initialPinnedChannels(storage) {
  let raw = null;
  try { raw = storage?.getItem(PINNED_CHANNELS_KEY); } catch {}
  if (raw == null) return [...PRIMARY_CHANNELS];
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return [...new Set(parsed.filter(name => typeof name === "string" && name.trim()))];
  } catch {}
  return [...PRIMARY_CHANNELS];
}
export function createApp(doc, options = {}) {
  const fetcher = options.fetch ?? globalThis.fetch.bind(globalThis), timers = options.timers ?? globalThis;
  const now = options.now ?? Date.now, urls = options.urlApi ?? globalThis.URL, storage = options.storage;
  const speech = options.speech ?? globalThis.speechSynthesis, Utterance = options.Utterance ?? globalThis.SpeechSynthesisUtterance;
  const navigation = options.navigation;
  const get = id => doc.getElementById(id), state = {language: initialLanguage(storage, options.languages ?? []), config: null, status: null,
    online: false, at: 0, busy: "", dirty: false, error: "", errorKey: "", polling: false, pollError: "", destroyed: false, generation: 0,
    previewGeneration: 0, previewBusy: false, previewAbort: null, previewUrl: null, previewError: false, previewOwner: null, spoken: null,
    reviews: null, reviewFocus: null, reviewSelected: new Set(), reference: null, referenceLoading: true, referenceSelected: "neutral",
    referenceKey: "", referencePose: null, referenceAnimated: false, referenceImageError: false,
    discovering: false, discovered: [], discoveryError: "", discoveryDone: false, discoveryKey: "", pickingModel: false,
    page: routePage(navigation?.location.hash), cameraPage: "", referencePaintedKey: "", visualReporting: false,
    presets: [], selectedPreset: null, presetPreview: false, presetOptionsKey: "", presetListKey: "", externalCase: null,
    startStep: 0, archiveKey: "", relayDirty: false, relayPortsManual: false, relayPortsAuto: false, relayError: "", relayErrorKey: "",
    channelSources: {...MOUTH_PRESETS.curated}, channelFilter: "all", channelQuery: "",
    diagView: "expressions", exprRegion: "all", exprActiveOnly: false, exprCompare: true, exprSourceFilter: "all", exprQuery: "", exprFrozen: false, frozenSnapshot: null,
    pinnedChannels: initialPinnedChannels(storage), exprPolling: false, exprEndpointMissing: false};
  const t = key => messages[state.language][key] ?? key, view = () => deriveView(state.status, state.online, now()-state.at);
  const choiceLabel = value => messages[state.language][`choice-${value}`] ?? value;
  const fieldLabel = name => t(name === "device" ? "trainDevice" : name === "split" ? "splitPath" : name);
  const rows = new Map(), trainInputs = new Map(), morphControls = new Map(), morphGroups = [], exprGroups = [], exprRegionBtns = new Map(); let rowKey = "", pinnedKey = "", specKey = "";
  function makeMorphControls() {
    if (morphControls.size) return;
    const list = get("hybrid-channels-list");
    for (const group of MORPH_GROUPS) {
      const section = doc.createElement("div");
      section.setAttribute("class", "morph-group");
      const head = doc.createElement("div");
      head.setAttribute("class", "morph-group-head");
      const titleWrap = doc.createElement("div");
      titleWrap.setAttribute("class", "morph-group-title-wrap");
      const titleEl = doc.createElement("strong");
      const countEl = doc.createElement("span");
      countEl.setAttribute("class", "morph-group-count");
      titleWrap.append(titleEl, countEl);
      const groupActions = doc.createElement("div");
      groupActions.setAttribute("class", "actions morph-group-actions");
      const stockCapableItems = group.items.filter(item => item.tag !== "missing");
      let allStockBtn = null;
      if (stockCapableItems.length) {
        allStockBtn = doc.createElement("button");
        allStockBtn.type = "button";
        allStockBtn.addEventListener("click", () => {
          for (const item of stockCapableItems) state.channelSources[item.name] = "stock";
          state.dirty = true; state.errorKey = ""; render();
        });
        groupActions.append(allStockBtn);
      }
      const allModelBtn = doc.createElement("button");
      allModelBtn.type = "button";
      allModelBtn.addEventListener("click", () => {
        for (const item of group.items) state.channelSources[item.name] = "model";
        state.dirty = true; state.errorKey = ""; render();
      });
      groupActions.append(allModelBtn);
      head.append(titleWrap, groupActions);
      const grid = doc.createElement("div");
      grid.setAttribute("class", "morph-grid");
      const itemEntries = [];
      for (const item of group.items) {
        const rowEl = doc.createElement("div");
        rowEl.setAttribute("class", "morph-row");
        rowEl.setAttribute("data-channel", item.name);
        const info = doc.createElement("div");
        info.setAttribute("class", "morph-info");
        const titleSpan = doc.createElement("span");
        titleSpan.setAttribute("class", "morph-title");
        const meta = doc.createElement("div");
        meta.setAttribute("class", "morph-meta");
        const codeEl = doc.createElement("code");
        codeEl.textContent = item.parent ? `${item.name} ← ${item.parent}` : item.name;
        const tagEl = doc.createElement("span");
        tagEl.setAttribute("class", "morph-tag");
        tagEl.setAttribute("data-quality", item.tag);
        meta.append(codeEl, tagEl);
        info.append(titleSpan, meta);
        const seg = doc.createElement("div");
        seg.setAttribute("class", "source-seg");
        seg.setAttribute("role", "group");
        const choices = item.tag === "missing" ? ["off", "model"] : ["stock", "model"];
        const buttons = {};
        for (const choice of choices) {
          const btn = doc.createElement("button");
          btn.type = "button";
          btn.setAttribute("data-choice", choice);
          btn.addEventListener("click", () => {
            state.channelSources[item.name] = choice;
            state.dirty = true; state.errorKey = ""; render();
          });
          seg.append(btn);
          buttons[choice] = btn;
        }
        rowEl.append(info, seg);
        grid.append(rowEl);
        const entry = {item, rowEl, titleSpan, tagEl, seg, buttons, stockBtn: buttons.stock ?? null, offBtn: buttons.off ?? null, modelBtn: buttons.model};
        morphControls.set(item.name, entry);
        itemEntries.push(entry);
      }
      section.append(head, grid);
      list.append(section);
      morphGroups.push({group, section, titleEl, countEl, allStockBtn, allModelBtn, items: itemEntries});
    }
  }
  function clearPreview() {
    state.previewGeneration++; state.previewAbort?.abort(); get("preview").hidden = true; get("preview").removeAttribute("src");
    if (state.previewUrl) urls.revokeObjectURL(state.previewUrl); state.previewUrl = null; get("placeholder").hidden = false; get("empty-camera").hidden = false;
  }
  function persistPins() {
    try { storage?.setItem(PINNED_CHANNELS_KEY, JSON.stringify(state.pinnedChannels)); } catch {}
  }
  function togglePin(name) {
    if (typeof name !== "string" || !name) return;
    if (state.pinnedChannels.includes(name)) state.pinnedChannels = state.pinnedChannels.filter(item => item !== name);
    else state.pinnedChannels = [...state.pinnedChannels, name];
    persistPins();
    render();
  }
  function clearPins() {
    state.pinnedChannels = [];
    persistPins();
    render();
  }
  function resetPins() {
    state.pinnedChannels = [...PRIMARY_CHANNELS];
    try { storage?.removeItem?.(PINNED_CHANNELS_KEY); } catch {}
    persistPins();
    render();
  }
  function makeCompareRow(name) {
    const wrap = doc.createElement("div");
    wrap.setAttribute("class", "expr-compare");
    const makeItem = kind => {
      const item = doc.createElement("div"), head = doc.createElement("div"), labelEl = doc.createElement("span"), valEl = doc.createElement("span");
      const barWrap = doc.createElement("div"), bar = doc.createElement("progress");
      item.setAttribute("class", "expr-cmp-item"); item.setAttribute("data-kind", kind);
      head.setAttribute("class", "expr-cmp-head"); labelEl.setAttribute("class", "expr-cmp-label"); valEl.setAttribute("class", "expr-cmp-val");
      head.append(labelEl, valEl);
      barWrap.setAttribute("class", "expr-cmp-bar"); bar.max = 1; bar.value = 0; bar.setAttribute("aria-label", `${name} ${kind}`);
      barWrap.append(bar); item.append(head, barWrap);
      return {item, labelEl, valEl, bar};
    };
    const native = makeItem("native"), model = makeItem("model");
    wrap.append(native.item, model.item);
    return {wrap, native, model};
  }
  function makePinnedRows() {
    const key = state.pinnedChannels.join("/");
    if (pinnedKey === key) return;
    pinnedKey = key;
    for (const rowId of [...rows.keys()]) if (rowId.startsWith("outputs/")) rows.delete(rowId);
    get("outputs").replaceChildren();
    for (const name of state.pinnedChannels) {
      const meta = MORPH_META.get(name);
      const card = doc.createElement("label"), head = doc.createElement("div"), titleEl = doc.createElement("span");
      const headRight = doc.createElement("div"), originEl = doc.createElement("span"), pinBtn = doc.createElement("button");
      const reading = doc.createElement("div"), codeEl = doc.createElement("span"), output = doc.createElement("output");
      const meter = doc.createElement("div"), bar = doc.createElement("progress"), compare = makeCompareRow(name);
      card.setAttribute("class", "expr-spot-card"); card.setAttribute("data-channel", name);
      head.setAttribute("class", "expr-spot-head"); titleEl.setAttribute("class", "expr-spot-name");
      headRight.setAttribute("class", "expr-spot-head-right"); originEl.setAttribute("class", "expr-origin-pill");
      pinBtn.type = "button"; pinBtn.setAttribute("class", "expr-pin-btn"); pinBtn.setAttribute("data-channel", name);
      pinBtn.setAttribute("aria-pressed", "true"); pinBtn.textContent = "📌";
      pinBtn.addEventListener("click", event => { event?.preventDefault?.(); event?.stopPropagation?.(); togglePin(name); });
      headRight.append(originEl, pinBtn);
      head.append(titleEl, headRight);
      reading.setAttribute("class", "expr-spot-reading"); codeEl.setAttribute("class", "expr-spot-code"); codeEl.textContent = name;
      output.setAttribute("class", "expr-spot-val"); reading.append(codeEl, output);
      meter.setAttribute("class", "expr-meter-slot"); bar.max = 1; bar.value = 0; bar.setAttribute("aria-label", name);
      meter.append(bar); card.append(head, reading, meter, compare.wrap); get("outputs").append(card);
      rows.set(`outputs/${name}`, {output, bar, compare, name, cardEl: card, titleEl, originEl, pinBtn, meta, kind: "spot"});
    }
  }
  function makeRows() {
    makePinnedRows();
    const channels = state.status?.contract?.channels ?? DEFAULT_DIAG_CHANNELS, key = channels.join("/");
    if (rowKey === key) return; rowKey = key;
    for (const rowId of [...rows.keys()]) if (rowId.startsWith("channel-table/")) rows.delete(rowId);
    exprGroups.length = 0; exprRegionBtns.clear();
    get("channel-table").replaceChildren(); get("expr-regions").replaceChildren();
    const channelSet = new Set(channels), covered = new Set(), partitions = [];
    for (const group of DIAGNOSTIC_GROUPS) {
      const items = group.items.filter(item => channelSet.has(item.name));
      if (items.length) {
        for (const item of items) covered.add(item.name);
        partitions.push({id: group.id, shortKey: group.shortKey, title: group.title, items});
      }
    }
    const extra = channels.filter(name => !covered.has(name));
    if (extra.length) partitions.push({id: "other", shortKey: "regionOther", title: null, items: extra.map(name => ({name, label: null}))});
    if (state.exprRegion !== "all" && !partitions.some(g => g.id === state.exprRegion)) state.exprRegion = "all";
    const addRegionBtn = (id, labelKey) => {
      const btn = doc.createElement("button"); btn.type = "button";
      btn.setAttribute("class", "expr-region-btn"); btn.setAttribute("data-region", id);
      btn.addEventListener("click", () => { state.exprRegion = id; render(); });
      get("expr-regions").append(btn); exprRegionBtns.set(id, {btn, labelKey});
    };
    addRegionBtn("all", "regionAll");
    for (const g of partitions) addRegionBtn(g.id, g.shortKey);
    for (const g of partitions) {
      const groupEl = doc.createElement("section"), head = doc.createElement("div"), titleEl = doc.createElement("strong"), badgeEl = doc.createElement("span"), grid = doc.createElement("div");
      groupEl.setAttribute("class", "expr-group-card"); groupEl.setAttribute("data-group", g.id);
      head.setAttribute("class", "expr-group-head"); badgeEl.setAttribute("class", "expr-group-badge");
      head.append(titleEl, badgeEl); grid.setAttribute("class", "expr-pair-grid");
      const cells = [];
      for (const item of g.items) {
        const cell = doc.createElement("label"), top = doc.createElement("div"), metaWrap = doc.createElement("div");
        const itemTitle = doc.createElement("span"), codeEl = doc.createElement("span"), right = doc.createElement("div");
        const originEl = doc.createElement("span"), output = doc.createElement("output"), pinBtn = doc.createElement("button");
        const meter = doc.createElement("div"), bar = doc.createElement("progress"), compare = makeCompareRow(item.name);
        cell.setAttribute("class", "expr-cell"); cell.setAttribute("data-channel", item.name);
        top.setAttribute("class", "expr-cell-top"); metaWrap.setAttribute("class", "expr-cell-meta");
        itemTitle.setAttribute("class", "expr-cell-title"); codeEl.setAttribute("class", "expr-cell-code"); codeEl.textContent = item.name;
        metaWrap.append(itemTitle, codeEl);
        right.setAttribute("class", "expr-cell-right"); originEl.setAttribute("class", "expr-origin-pill"); output.setAttribute("class", "expr-cell-val");
        pinBtn.type = "button"; pinBtn.setAttribute("class", "expr-pin-btn"); pinBtn.setAttribute("data-channel", item.name);
        pinBtn.textContent = "📌";
        pinBtn.addEventListener("click", event => { event?.preventDefault?.(); event?.stopPropagation?.(); togglePin(item.name); });
        right.append(originEl, output, pinBtn); top.append(metaWrap, right);
        meter.setAttribute("class", "expr-meter-slot"); bar.max = 1; bar.value = 0; bar.setAttribute("aria-label", item.name);
        meter.append(bar); cell.append(top, meter, compare.wrap); grid.append(cell);
        const entry = {output, bar, compare, name: item.name, cellEl: cell, titleEl: itemTitle, originEl, pinBtn, meta: item, groupId: g.id, kind: "cell"};
        rows.set(`channel-table/${item.name}`, entry);
        cells.push(entry);
      }
      groupEl.append(head, grid); get("channel-table").append(groupEl);
      exprGroups.push({group: g, groupEl, titleEl, badgeEl, cells});
    }
  }
  function renderTraining(current) {
    const training = state.status?.training, spec = training?.operations ?? {}, key = JSON.stringify(spec);
    if (key !== specKey) {
      specKey = key; get("train-action").replaceChildren();
      for (const action of Object.keys(spec)) { const option = doc.createElement("option"); option.value = action; option.textContent = t(action); get("train-action").append(option); }
      if (Object.keys(spec).length) get("train-action").value = Object.keys(spec)[0];
      buildTrainInputs();
    }
    for (const option of get("train-action").children) option.textContent = t(option.value);
    for (const [name, item] of trainInputs) { item.label.textContent = fieldLabel(name); for (const option of item.input.children ?? []) option.textContent = choiceLabel(option.value); }
    const active = training?.running === true;
    get("train-start").disabled = !current.current || !Object.keys(spec).length || active || Boolean(state.busy);
    get("train-cancel").disabled = !current.current || !active || Boolean(state.busy);
    get("train-state").textContent = !Object.keys(spec).length ? t("trainUnavailable") : t(training?.state ?? "idle");
    get("train-error").hidden = !training?.error; get("train-error").textContent = training?.error ? errorMessage(state.language, training.error) : "";
    get("train-result").textContent = training?.result ? JSON.stringify(training.result) : "";
    get("training-status").hidden = !active && !training?.error && !training?.result;
    get("train-cancel").hidden = !active;
  }
  function buildTrainInputs() {
    const previous = Object.fromEntries([...trainInputs].map(([key, item]) => [key, item.input.value]));
    trainInputs.clear(); get("train-inputs").replaceChildren();
    for (const field of state.status?.training?.operations?.[get("train-action").value] ?? []) {
      const wrapper = doc.createElement("label"), label = doc.createElement("span"), input = doc.createElement(field.choices ? "select" : "input");
      if (field.choices) for (const value of field.choices) { const option = doc.createElement("option"); option.value = value; option.textContent = choiceLabel(value); input.append(option); }
      label.textContent = fieldLabel(field.name); input.value = previous[field.name] ?? field.default ?? ""; if (!field.choices) input.type = field.type ?? "text";
      input.required = field.required === true; wrapper.append(label, input); get("train-inputs").append(wrapper); trainInputs.set(field.name, {input, label});
    }
  }
  function availablePresets() {
    const external = state.status?.external_guide;
    if (!external) return state.presets;
    const name = {zh:`本轮研究采集（${external.role}）`,en:`Research round ${external.role}`};
    const steps = external.steps.map(step => ({id:step.action, demo_id:step.visual.demo_prompt_ids[0],
      title:{zh:step.text,en:state.reference?.poses?.find(p=>p.id===step.visual.demo_prompt_ids[0])?.title?.en ?? step.action}}));
    return [{id:external.case,name,description:{zh:"这一轮的动作和顺序已经固定。其他预设可以预览，不影响本轮。",
      en:"This round's actions and order are fixed. You can preview other presets without affecting it."},steps},...state.presets];
  }
  function selectedPreset() { return availablePresets().find(p=>p.id===state.selectedPreset); }
  function renderCapturePlan(current) {
    const presets = availablePresets(), external = state.status?.external_guide, record = state.status?.recording;
    const active = external?.running === true || ["recording","collecting"].includes(record?.state);
    if (external && (state.externalCase !== external.case || active)) {
      state.selectedPreset=external.case; state.externalCase=external.case; state.presetPreview=false;
    } else if (active && record?.guide?.preset_id) { state.selectedPreset=record.guide.preset_id; state.presetPreview=false; }
    if (!state.selectedPreset && presets.length) state.selectedPreset=presets.some(p=>p.id==="complete")?"complete":presets[0].id;
    const language=state.language==="en"?"en":"zh", selector=get("capture-preset");
    const key=JSON.stringify([state.language,presets.map(p=>[p.id,p.name,p.steps.length])]);
    if (key!==state.presetOptionsKey) {
      state.presetOptionsKey=key; selector.replaceChildren();
      for(const preset of presets) {const option=doc.createElement("option");option.value=preset.id;
        option.textContent=`${preset.name[language]} · ${preset.steps.length} ${t("captureSteps")}`;selector.append(option);}
    }
    selector.value=state.selectedPreset ?? ""; selector.disabled=active ||
      state.status?.capture_control?.sealed_c || ["starting","stopping"].includes(state.status?.capture_control?.state) || Boolean(state.busy) || !presets.length;
    const preset=selectedPreset(), progress=captureProgress(current.current?state.status:null,preset,state.presetPreview);
    get("capture-progress-label").textContent=t("captureProgress");
    get("capture-preset-label").textContent=t("capturePreset");
    get("capture-progress-phase").textContent=t(current.current?progress.phaseKey:"offline");
    get("capture-progress-count").textContent=progress.phaseKey==="captureFree"?"—":`${progress.current} / ${progress.total}`;
    get("capture-progress").max=Math.max(1,progress.total); get("capture-progress").value=progress.current;
    get("capture-progress").setAttribute("aria-label",`${t("captureProgress")}: ${progress.current} / ${progress.total}`);
    get("capture-preset-description").textContent=(preset?.description?.[language] ?? "")+
      (state.status?.read_only && preset?.id!==external?.case ? ` ${t("capturePreviewOnly")}` : "");
    get("capture-plan-order-title").textContent=`${t("captureOrder")} · ${preset?.steps.length ?? 0} ${t("captureSteps")}`;
    const listKey=JSON.stringify([state.selectedPreset,state.language,preset?.steps]);
    if (listKey!==state.presetListKey) {
      state.presetListKey=listKey; get("capture-action-list").replaceChildren();
      get("guide-select").replaceChildren();
      for (const [index, step] of (preset?.steps ?? []).entries()) {
        const item=doc.createElement("li"), option=doc.createElement("option");
        item.textContent=step.title?.[language] ?? step.prepare?.[language] ?? step.id;
        option.value=String(index); option.textContent=`${index+1}. ${item.textContent}`;
        get("capture-action-list").append(item); get("guide-select").append(option);
      }
    }
    const guide=record?.guide, total=preset?.steps.length ?? 0;
    const index=active ? guide?.index ?? 0 : state.startStep;
    const show=total>0 && !external && !state.status?.read_only && !state.status?.capture_control?.sealed_c &&
      (active ? Boolean(guide?.step) && !record.evaluation_only : get("guide-enabled").checked);
    get("capture-step-nav").hidden=!show;
    get("guide-select").value=String(index);
    get("guide-select").disabled=!show || !current.current || Boolean(state.busy) ||
      ["starting","stopping"].includes(state.status?.capture_control?.state);
    get("guide-prev").disabled=get("guide-select").disabled || index<=0;
    get("guide-next").disabled=get("guide-select").disabled || index+1>=total;
    for(const [index,item] of [...get("capture-action-list").children].entries()) {
      if(progress.current===index+1)item.setAttribute("aria-current","step");else item.removeAttribute("aria-current");
    }
  }
  function selectPreset(id) {
    if (get("capture-preset").disabled || !availablePresets().some(p=>p.id===id)) return;
    previewPreset(id);
    render();
  }
  function previewPreset(id) {
    state.selectedPreset=id; state.presetPreview=true; state.startStep=0;
    const first=selectedPreset().steps[0]; state.referenceSelected=first.demo_id ?? first.id;
    state.referenceAnimated=false;
    if (id==="continuation" && state.continuation) {
      get("person_id").value=state.continuation.person_id;
      get("label-stage").value=state.continuation.label_stage;
      get("guide-enabled").checked=true;
    }
  }
  async function loadPresets() {
    try {
      const result=await requestJson(fetcher,"/api/presets");
      if (!state.destroyed) {
        state.presets=result.presets;
        state.continuation=result.continuation;
        if ((!state.selectedPreset && result.default==="continuation") ||
            (result.sealed_c && state.selectedPreset!==result.default)) previewPreset(result.default);
        if (state.selectedPreset==="continuation" && !result.continuation &&
            state.status?.recording?.state!=="recording") previewPreset(result.default);
      }
    }
    catch(error) {if(!state.destroyed)state.error=error.message;}
    finally {if(!state.destroyed)render();}
  }
  function selectStep(index) {
    const step=selectedPreset()?.steps[index];
    if (get("guide-select").disabled || !step) return;
    if (state.status?.recording?.state==="recording") return record("select_step",index);
    state.startStep=index; state.referenceSelected=step.demo_id ?? step.id;
    state.referenceAnimated=false; render();
  }
  function renderRecord(current) {
    const record = state.status?.recording, active = record?.state === "recording" || record?.state === "collecting";
    const managed = state.status?.managed_capture, control = state.status?.capture_control;
    const pending = ["starting","recording","stopping"].includes(control?.state);
    const sealed = control?.sealed_c, spent = sealed && control.state!=="idle";
    const incompatibleInput = managed && current.running && state.status?.source_mode !== "live";
    const automatic=get("guide-mode").value!=="manual", guided=get("guide-enabled").checked;
    const prepare=Number(get("guide-prepare").value || 7);
    get("guide-setup").hidden=active || pending || !guided || state.status?.read_only===true;
    get("guide-prepare-field").hidden=!automatic;
    get("record-start").disabled = !current.current || incompatibleInput || !managed && (!current.running || state.status?.connected !== true || state.status?.source_mode !== "live") || active || pending || Boolean(state.busy) || state.dirty && !managed || !get("record-consent").checked || !get("person_id").value.trim() || guided && automatic && !(prepare>=3 && prepare<=30);
    if (sealed && (spent || state.selectedPreset!==control.preset_id)) get("record-start").disabled = true;
    get("record-stop").disabled = !current.current || !(active || pending) || control?.state==="stopping" || Boolean(state.busy); get("record-cancel").disabled = get("record-stop").disabled;
    get("record-start").hidden = active || pending; get("record-stop").hidden = !(active || pending); get("record-cancel").hidden = !(active || pending);
    get("record-consent").disabled = active || pending || spent || Boolean(state.busy); get("person_id").disabled = get("record-consent").disabled;
    get("guide-enabled").disabled = sealed || active || pending || Boolean(state.busy);
    get("label-stage").disabled = sealed || active || pending || Boolean(state.busy);
    get("receive-results").disabled = get("label-stage").disabled || get("label-stage").value === "instant";
    if (get("label-stage").value === "instant") get("receive-results").checked = true;
    const controlText = {starting:"captureStarting",stopping:"captureSaving",completed:"captureComplete",error:"captureError"};
    get("record-state").textContent = current.current ? t(controlText[control?.state] ?? record?.state ?? "idle") : t("offline");
    get("capture-stop").disabled = get("stop").disabled;
    const next = incompatibleInput ? "recordCloseInput" : managed ? "captureSelfService" : !get("headset_host").value.trim() ? "recordNeedHeadset" : !current.running ? "recordOpenCamera" :
      !current.video ? "recordWaitCamera" : !get("record-consent").checked ? "recordNeedConsent" : "recordReady";
    get("record-continue").hidden=!current.current || !managed || active || pending || !state.continuation || state.selectedPreset==="continuation";
    get("record-continue").disabled=Boolean(state.busy);
    get("record-next").textContent = active ? "" : t(state.selectedPreset==="continuation" && state.continuation ? "guidePickThenStart" : current.current && control?.state==="completed" && record?.guide?.phase==="finished" ? "recordRefit" : next);
    if (sealed) {
      const finished = control.state==="completed" && record?.guide?.phase==="finished" && !record.guide.skipped_indices?.length;
      get("record-next").textContent = active || pending ? "" : t(!spent ? "sealedReady" : !record?.path ? "sealedNoArchive" : finished ? "sealedSaved" : "sealedInterrupted");
      get("consent-label").textContent = t("sealedConsent");
    }
    get("record-path").textContent = display(record?.directory ?? record?.path);
    get("record-error").hidden = !(record?.error || control?.error);
    get("record-error").textContent = record?.error || control?.error ? errorMessage(state.language, record?.error || control.error) : "";
    get("receive-results-warning").hidden = !active || !state.status?.stock?.error;
    get("receive-results-warning").textContent = t("receiveResultsWarning");
    const guide = record?.guide, step = guide?.current ?? guide?.step;
    const phase = guide?.phase === "review" ? "confirm" : guide?.phase;
    const auto = guide?.automatic === true;
    const prompt = auto && step ? `${t(guide.paused?"autoPaused":phase==="hold"?"autoHold":"autoPrepare")} ${step.title?.[state.language==="en"?"en":"zh"] ?? step.id}` :
      step && typeof step === "object" ? (step[phase]?.[state.language === "en" ? "en" : "zh"] ?? step.id) : step;
    get("guide").hidden = !active || !current.current || !prompt;
    const holdHelp = guide?.index+1 === guide?.total ? "autoFinalHoldHelp" : "autoHoldHelp";
    get("guide-step").textContent = prompt ?? ""; get("guide-hint").textContent = guide?.paused ? t("autoPausedHelp") : auto ? t(!guide.display_ready?"autoShowImage":phase==="hold"?holdHelp:"autoPrepareHelp") : t(phase==="hold"?"guideManualHold":phase==="confirm"?"guideManualReview":"guideManualPrepare");
    get("guide-countdown").hidden = !auto || guide?.paused || !guide?.display_ready;
    get("guide-countdown").textContent = `${Math.ceil(guide?.remaining_seconds ?? 0)} ${t("guideSeconds")}`;
    get("guide-pause").hidden = !active;
    get("guide-pause").disabled = Boolean(state.busy) || !current.current;
    get("guide-pause").textContent = t(guide?.paused?"guideResume":"guidePause");
    get("guide-progress").value = guide?.progress ?? 0;
    for (const [action, enabled] of [["ready", guide?.phase === "prepare"], ["finish", guide?.ready_to_finish], ["confirm", guide?.phase === "review"], ["skip", Boolean(step)]]) {
      get(`guide-${action}`).disabled = !active || !current.current || !enabled || guide?.paused || Boolean(state.busy);
      get(`guide-${action}`).hidden = !enabled || auto && action!=="skip";
    }
    const available = Boolean(speech && Utterance); get("voice-enabled").disabled = !available;
    get("voice-info").textContent = t(!available ? "voiceUnavailable" : get("voice-enabled").checked ? "voiceReady" : "voiceOff");
    const voiceKey = active && current.current && available && get("voice-enabled").checked && prompt ? `${record?.session_id}/${guide?.index}/${guide?.phase}/${prompt}/${state.language}` : null;
    if (voiceKey !== state.spoken) {
      speech?.cancel(); state.spoken = voiceKey;
      if (voiceKey) { const utterance = new Utterance(auto ? prompt : `${prompt}. ${get("guide-hint").textContent}`); utterance.lang = state.language === "en" ? "en-US" : "zh-CN"; speech.speak(utterance); }
    }
  }
  function referenceStep(current) {
    const external = current.current && state.status?.external_guide;
    if (external && (external.running || state.selectedPreset===external.case)) {
      const step = external.steps[external.index];
      return {following: external.running, external, step,
        id: external.running ? step.visual.demo_prompt_ids[0] : state.referenceSelected};
    }
    const record = state.status?.recording, active = current.current && ["recording", "collecting"].includes(record?.state) && (record?.guide?.current ?? record?.guide?.step);
    return {following: Boolean(active), step: active || null, id: active?.demo_id ?? active?.id ?? state.referenceSelected,
      automatic: record?.guide?.automatic, phase: record?.guide?.phase, frame:record?.guide?.frame};
  }
  function practicePoses() { return (state.reference?.poses ?? []).filter(item => !item.legacy_only); }
  function renderReference(current) {
    const poses = practicePoses(), guide = referenceStep(current);
    const pose = state.reference?.poses?.find(item => item.id === guide.id), hasImage = Boolean(pose?.front);
    const selector = get("reference-select"), language = state.language === "en" ? "en" : "zh";
    if (selector.children.length !== poses.length) {
      selector.replaceChildren();
      for (const item of poses) { const option = doc.createElement("option"); option.value = item.id; selector.append(option); }
    }
    for (const option of selector.children) {
      const item = poses.find(pose => pose.id === option.value);
      option.textContent = (item?.title?.[language] ?? option.value)+(item?.front ? "" : t("referenceTextOnly"));
    }
    selector.value = guide.id; selector.disabled = !poses.length || guide.following;
    const index = poses.findIndex(item => item.id === guide.id);
    get("reference-prev").disabled = guide.following || index <= 0;
    get("reference-next").disabled = guide.following || index < 0 || index >= poses.length-1;
    const external = guide.external;
    get("reference-mode").textContent = external?.running ? `${external.role} · ${external.index+1}/${external.steps.length} · ${t("visual"+external.phase)}` : external ? t("visualpractice") : t(guide.following ? "referenceFollowing" : "referencePractice");
    get("reference-pose").textContent = pose?.title?.[language] ?? guide.id;
    get("reference-instruction").textContent = external && guide.id === guide.step.visual.demo_prompt_ids[0] ? `${guide.step.text} ${guide.step.visual.note}` :
      guide.following && guide.automatic ? guide.step.title?.[language] ?? "" : guide.following ? "" : pose?.instruction?.[language] ?? "";
    if (state.referencePose !== guide.id) { state.referencePose = guide.id; state.referenceAnimated = false; }
    const animated = external?.running ? !["hold", "done", "paused", "interrupted"].includes(external.phase) :
      guide.following ? guide.phase==="prepare" || MOVING_STEPS.has(guide.id) : state.referenceAnimated;
    const anchorId = external && guide.id === guide.step.visual.demo_prompt_ids[0] ? guide.step.visual.demo_prompt_ids[1] : null;
    const anchor = poses.find(item => item.id === anchorId)?.front ?? state.reference?.neutral;
    const key = `${guide.id}/${animated}/${pose?.front}/${pose?.profile}/${anchor}/${external?.frame ?? guide.frame ?? ""}`;
    get("reference-views").hidden = !hasImage || state.referenceImageError;
    get("reference-profile").hidden = !hasImage || !pose?.profile;
    if (key !== state.referenceKey) {
      state.referenceKey = key; state.referenceImageError = false;
      for (const id of ["reference-image", "reference-neutral-image", "reference-profile-image"]) get(id).removeAttribute("src");
      if (hasImage) {
        const asset = file => `/static/guide-reference/${encodeURIComponent(file)}`;
        get("reference-image").src = asset(animated && pose.animation ? pose.animation : pose.front);
        get("reference-neutral-image").src = asset(anchor);
        if (pose.profile) get("reference-profile-image").src = asset(pose.profile);
      }
      if ((external || guide.following) && state.page === "recording") get("pose-reference").scrollIntoView?.({block:"start"});
    }
    get("reference-neutral-caption").textContent = t(anchorId ? "referenceOpening" : "referenceNeutral");
    get("reference-image").setAttribute("alt", `${t("referenceTarget")}: ${pose?.title?.[language] ?? guide.id}`);
    get("reference-neutral-image").setAttribute("alt", t("referenceNeutral"));
    get("reference-profile-image").setAttribute("alt", `${t("referenceProfile")}: ${pose?.title?.[language] ?? guide.id}`);
    get("reference-views").hidden = !hasImage || state.referenceImageError;
    get("reference-profile").hidden = !hasImage || !pose?.profile;
    get("reference-play").hidden = !hasImage || !pose?.animation;
    get("reference-play").disabled = state.referenceImageError || Boolean(external?.running);
    get("reference-play").textContent = t(animated ? "referenceStill" : "referencePlay");
    get("reference-play").setAttribute("aria-pressed", String(animated));
    get("reference-message").textContent = guide.following && guide.automatic && !hasImage ? guide.step.title?.[language] ?? "" :
      !hasImage && pose?.unavailable_reason?.[language] ? pose.unavailable_reason[language] :
      t(state.referenceLoading ? "referenceLoading" : !state.reference ? "referenceMissing" : !hasImage ? "referencePoseMissing" : state.referenceImageError ? "referenceImageError" : "referenceHelp");
    const authored = pose?.provenance?.some(kind => kind === "original_tongue_mesh_with_authored_skin_binding_and_post_skinning_controls" || kind === "original_mouth_shift_deltas_with_authored_independent_lip_masks");
    get("reference-origin").textContent = hasImage ? t(authored ? "referenceAuthored" : "referenceOriginal") : "";
    get("reference-source").textContent = state.reference?.model ? `${state.reference.model.name} · ${state.reference.model.copyright}` : "";
  }
  function selectReference(id) {
    if (referenceStep(view()).following || !practicePoses().some(pose => pose.id === id)) return;
    state.referenceSelected = id; state.referenceAnimated = false; render();
  }
  async function loadReference() {
    try { const manifest = await requestJson(fetcher, "/static/guide-reference/manifest.json"); if (!state.destroyed) state.reference = manifest; }
    catch { if (!state.destroyed) state.reference = null; }
    finally { state.referenceLoading = false; if (!state.destroyed) render(); }
  }
  async function reportVisual() {
    if (state.visualReporting || state.destroyed || !view().current) return;
    const external = state.status?.external_guide;
    const record=state.status?.recording, owner=state.status?.capture_control?.viewer;
    const automatic=record?.guide?.automatic && record.state==="recording" && record.guide.step &&
      (!owner || owner===options.viewer);
    if (!(external && options.viewer) && !automatic) return;
    state.visualReporting = true;
    try {
      const current = referenceStep(view()), key = state.referenceKey;
      const images = ["reference-image", "reference-neutral-image", ...(get("reference-profile").hidden ? [] : ["reference-profile-image"])].map(get);
      const pose=state.reference?.poses?.find(p=>p.id===current.id), textOnly=Boolean(automatic && pose && !pose.front);
      const rect = get(textOnly?"reference-message":"reference-image").getBoundingClientRect?.();
      const height = doc.documentElement.clientHeight ?? Infinity;
      const inView = !rect || rect.height > 0 && (Math.min(rect.bottom,height)-Math.max(rect.top,0))/rect.height >= .8;
      let ready = (automatic ? current.id===(record.guide.step.demo_id ?? record.guide.step.id) : state.selectedPreset===external.case && current.id === current.step?.visual?.demo_prompt_ids?.[0]) && state.page === "recording" &&
        inView && !state.referenceImageError && (textOnly ? Boolean(get("reference-message").textContent) :
          !get("reference-views").hidden && images.every(image => image.complete && image.naturalWidth > 0));
      if (ready && state.referencePaintedKey !== key) {
        const paint = options.paint ?? globalThis.requestAnimationFrame;
        await new Promise(resolve => paint(() => paint(resolve)));
        if (state.referenceKey === key) state.referencePaintedKey = key;
      }
      ready = ready && state.referencePaintedKey === state.referenceKey;
      if (state.destroyed) return;
      const visible=!doc.hidden && state.page==="recording";
      await requestJson(fetcher, automatic?"/api/recording/display":"/api/display", automatic ?
        {session_id:record.session_id,frame:record.guide.frame,phase:record.guide.phase,visible,media_ready:ready,
          ...(owner ? {viewer:options.viewer} : {})} :
        {viewer:options.viewer,frame:external.frame,index:external.index,visible,media_ready:ready});
    } finally { state.visualReporting = false; }
  }
  function reviewImageUrl(sample) { return `/api/recording/image?session=${encodeURIComponent(state.reviews.session_id)}&sample=${encodeURIComponent(sample.sample_id)}`; }
  function renderReview(current) {
    const active = state.status?.recording?.state === "recording", available = Boolean(state.status?.recording?.path);
    if (state.reviews && state.reviews.session_id !== state.status?.recording?.session_id) {
      state.reviews = null; state.reviewFocus = null; state.reviewSelected.clear(); get("review-grid").replaceChildren();
      get("review-image").hidden = true; get("review-image").removeAttribute("src"); get("review-confirmed").checked = false;
    }
    get("review-load").disabled = !current.current || !available || active || Boolean(state.busy);
    get("review-prev").disabled = get("review-load").disabled || !state.reviews || state.reviews.offset <= 0;
    get("review-next").disabled = get("review-load").disabled || !state.reviews || state.reviews.offset+state.reviews.samples.length >= state.reviews.total;
    get("review-select-page").disabled = !state.reviews?.samples.length || Boolean(state.busy);
    get("review-proposal").disabled = !state.reviewFocus?.prompt_candidate || Boolean(state.busy);
    get("review-apply").disabled = !current.current || active || !state.reviewSelected.size || !get("review-confirmed").checked || !get("reviewer").value.trim() || Boolean(state.busy);
    get("review-count").textContent = state.reviews ? `${state.reviews.offset+1}–${state.reviews.offset+state.reviews.samples.length} / ${state.reviews.total} · ${state.reviewSelected.size} ${t("selected")}` : "";
    get("review-focus").textContent = state.reviewFocus ? `${state.reviewFocus.sample_id} · ${JSON.stringify(state.reviewFocus.prompt_candidate?.labels ?? {})}` : "";
    get("review-image").setAttribute("alt", t("reviewImage"));
    for (const image of get("review-grid").children) image.children[1]?.setAttribute("alt", t("reviewImage"));
  }
  async function loadReviews(offset = 0) {
    if (get("review-load").disabled) return;
    await control("working", async () => {
      const result = await requestJson(fetcher, `/api/recording/review?offset=${offset}&limit=24&all=${get("review-all").checked}`);
      state.reviews = result; state.reviewFocus = null; state.reviewSelected.clear(); get("review-grid").replaceChildren(); get("review-image").hidden = true;
      get("review-confirmed").checked = false;
      for (const sample of result.samples) {
        const wrapper = doc.createElement("label"), checkbox = doc.createElement("input"), image = doc.createElement("img"), text = doc.createElement("span");
        checkbox.type = "checkbox"; image.src = reviewImageUrl(sample); image.setAttribute("alt", t("reviewImage")); text.textContent = sample.sample_id;
        checkbox.addEventListener("change", () => { if (checkbox.checked) state.reviewSelected.add(sample.sample_id); else state.reviewSelected.delete(sample.sample_id); render(); });
        image.addEventListener("click", () => { state.reviewFocus = sample; get("review-image").src = reviewImageUrl(sample); get("review-image").hidden = false; render(); });
        wrapper.append(checkbox, image, text); get("review-grid").append(wrapper);
      }
    });
  }
  async function applyReview() {
    if (get("review-apply").disabled) return;
    await control("working", async () => {
      const targets = JSON.parse(get("review-targets").value);
      await requestJson(fetcher, "/api/recording/review", {session_id: state.reviews.session_id, sample_ids: [...state.reviewSelected], targets,
        confirmed: get("review-confirmed").checked, reviewer: get("reviewer").value.trim(), observation: get("observation").value});
      get("review-confirmed").checked = false;
    });
  }
  function render() {
    const current = view(); doc.documentElement.lang = state.language; doc.title = t("title");
    for (const [id, key] of Object.entries(LABELS)) get(id).textContent = t(key);
    get("language").value = state.language; get("language").setAttribute("aria-label", t("language")); get("preview").setAttribute("alt", t("previewAlt"));
    get("status").textContent = t(state.busy || (!state.config && !state.pollError ? "loading" : current.key === "running" && state.status?.source_mode === "replay" ? "replaying" : current.key));
    get("status").setAttribute("data-state", current.key);
    get("camera-panel").setAttribute("data-live", String(current.video));
    get("service-state").textContent = t(current.current ? "serviceOnline" : "serviceOffline");
    get("service-state").setAttribute("data-online", String(current.current));
    get("start").disabled = !state.config || !current.current || Boolean(state.busy) || current.running;
    get("capture").disabled = get("start").disabled;
    get("stop").disabled = Boolean(state.busy) || !current.running; get("replay").disabled = get("start").disabled;
    get("start").hidden = current.running; get("stop").hidden = !current.running;
    get("capture").hidden = current.running; get("capture-stop").hidden = !current.running;
    get("save").disabled = !state.config || Boolean(state.busy) || !state.dirty;
    for (const id of [...TEXT_FIELDS, ...NUMBER_FIELDS, ...CHECK_FIELDS]) get(id).disabled = Boolean(state.busy) || !state.config;
    if (!get("filter_enabled").checked) for (const id of FILTER_NUMBER_FIELDS) get(id).disabled = true;
    get("config-state").textContent = state.config ? t(state.dirty ? "unsaved" : "saved") : "";
    renderConnection(current); renderPages();
    const error = state.error || state.pollError || state.status?.last_error;
    get("error").hidden = !error && !state.errorKey; get("error").textContent = state.errorKey ? t(state.errorKey) : error ? errorMessage(state.language, error) : "";
    const relayEyeErrors = ["eye_left", "eye_right"].map(k => state.status?.cymple_relay?.eyes?.[k]?.error).filter(Boolean);
    const technicalParts = [...new Set([error, state.discoveryError, state.relayError, state.status?.cymple_relay?.error, ...relayEyeErrors, state.status?.recording?.error, state.status?.training?.error].filter(Boolean))];
    get("technical-error").textContent = technicalParts.join("\n\n") || "—";
    get("migration").hidden = !state.status?.legacy_configuration_available;
    get("placeholder").textContent = t(!get("preview-enabled").checked ? "previewOff" : state.previewError ? "previewError" : "noVideo");
    const owner = `${state.status?.source?.session_id}/${state.status?.output_session_id}`;
    if (owner !== state.previewOwner) { clearPreview(); state.previewOwner = owner; }
    if (!current.video || state.busy || !get("preview-enabled").checked) clearPreview();
    renderDiagnostics(current, technicalParts);
    const replay = state.status?.replay; get("replay-info").textContent = replay?.path ? `${replay.path} · ${display(replay.position)} / ${display(replay.total)} ${t("frames")}${replay.finished ? ` · ${t("replayDone")}` : ""}` : t("replayReady");
    const focused=state.page==="recording" && state.status?.recording?.state==="recording" && state.status?.recording?.guide?.step;
    doc.documentElement.setAttribute?.("data-guiding",String(Boolean(focused)));
    renderCapturePlan(current); renderRecord(current); renderReference(current); renderTraining(current); renderReview(current); renderCympleRelay(current);
    if(state.status?.managed_capture && state.page==="recording") {
      get("page-intro").textContent=t(state.status.capture_control?.sealed_c ? "sealedReady" : "captureSelfService");
    }
    const sealed = state.status?.capture_control?.sealed_c;
    get("review").hidden = Boolean(sealed);
    if (sealed) {
      for (const id of ["start", "replay", "train-start", "train-cancel", "model-browse"]) get(id).disabled = true;
      if (state.status.capture_control.state!=="idle") for (const id of ["capture", "save", "discover", "headset-select"]) get(id).disabled = true;
    }
    const capturePending = ["starting", "recording", "stopping"].includes(state.status?.capture_control?.state);
    if (capturePending) {
      for (const id of [...TEXT_FIELDS, ...NUMBER_FIELDS, ...CHECK_FIELDS, "start", "capture", "replay", "save", "discover", "headset-select", "model-browse"])
        get(id).disabled = true;
    }
    if (state.status?.read_only) {
      get("status").textContent = t("visualReadonly");
      get("page-intro").textContent = t("visualReadonly");
      get("record-next").textContent = t("visualReadonly");
      for (const id of [...TEXT_FIELDS, ...NUMBER_FIELDS, ...CHECK_FIELDS, "start", "stop", "save",
        "capture", "capture-stop", "replay", "discover", "model-browse", "headset-select",
        "record-start", "record-stop", "record-cancel", "train-start", "train-cancel", "voice-enabled"]) get(id).disabled = true;
    }
    renderRouting(current);
  }
  function channelOrigin(snap, snapValid, name, activeSources) {
    let route = "model";
    if (Object.hasOwn(AGGREGATE_CHILDREN_MAP, name)) {
      const kids = AGGREGATE_CHILDREN_MAP[name];
      route = kids.some(k => activeSources[k] === "off") ? "off" :
        kids.every(k => activeSources[k] === "stock") ? "stock" : "model";
    } else if (Object.hasOwn(activeSources, name)) {
      route = activeSources[name];
    }
    if (route === "off") return {origin: "off", key: "originOff"};
    if (snapValid && snap?.prediction?.native_channels?.includes(name)) return {origin: "native", key: "originNativeLive"};
    if (route === "stock") {
      if (snapValid && channelValue(snap, name, true) != null) return {origin: "fallback", key: "originModelFallback"};
      return {origin: "stock-idle", key: "originStockIdle"};
    }
    if (snap?.model?.trained_channels && !snap.model.capture_only && !snap.model.trained_channels.includes(name)) {
      return {origin: "untrained", key: "originUntrained"};
    }
    return {origin: "model", key: "originModel"};
  }
  function renderExpressionPanel(current) {
    makeRows();
    if (!current.current || !current.running) {
      state.exprFrozen = false;
      state.frozenSnapshot = null;
    }
    const snap = state.exprFrozen && state.frozenSnapshot ? state.frozenSnapshot.status : state.status;
    const snapValid = state.exprFrozen && state.frozenSnapshot ? state.frozenSnapshot.valid : (current.valid && !state.busy);
    const activeSources = snap?.config ?
      resolveChannelSources(snap.config.mouth_mode, snap.config.hybrid_channels, snap.config.channel_sources) :
      (state.channelSources ?? MOUTH_PRESETS.curated);
    const channels = state.status?.contract?.channels ?? DEFAULT_DIAG_CHANNELS;
    const uniqueChannels = [...new Set(channels)];
    const pinnedSet = new Set(state.pinnedChannels);

    for (const entry of rows.values()) {
      const value = channelValue(snap, entry.name, snapValid);
      const info = channelOrigin(snap, snapValid, entry.name, activeSources);
      const active = value != null && value >= 0.02;
      const text = value == null ? "—" : value.toFixed(3);
      if (entry.output.textContent !== text) {
        entry.output.textContent = text;
        entry.output.setAttribute("aria-label", `${entry.name}: ${text}`);
      }
      entry.bar.value = value ?? 0;
      entry.bar.hidden = value == null;
      const titleText = entry.meta?.label?.[state.language] ?? entry.meta?.label?.en ?? entry.name;
      if (entry.titleEl.textContent !== titleText) entry.titleEl.textContent = titleText;
      const originText = t(info.key);
      if (entry.originEl.textContent !== originText) entry.originEl.textContent = originText;
      entry.originEl.setAttribute("data-origin", info.origin);
      const isPinned = pinnedSet.has(entry.name);
      if (entry.pinBtn) {
        entry.pinBtn.setAttribute("aria-pressed", String(isPinned));
        const pinLabel = t(isPinned ? "exprPinRemove" : "exprPinAdd");
        entry.pinBtn.setAttribute("title", pinLabel);
        entry.pinBtn.setAttribute("aria-label", `${pinLabel}: ${entry.name}`);
      }
      const host = entry.kind === "spot" ? entry.cardEl : entry.cellEl;
      host.setAttribute("data-active", String(active));
      host.setAttribute("data-origin", info.origin);
      host.setAttribute("data-pinned", String(isPinned));
      if (entry.compare) {
        entry.compare.wrap.hidden = !state.exprCompare;
        if (state.exprCompare) {
          const hasNativeSlot = !PURE_EXTENDED_SET.has(entry.name);
          const mergedParent = STOCK_AGGREGATE_PARENTS[entry.name] ?? null;
          const nativeVal = hasNativeSlot ? stockChannelValue(snap, entry.name, snapValid) : null;
          const modelVal = modelChannelValue(snap, entry.name, snapValid);
          const nativeSelected = info.origin === "native";
          const modelSelected = info.origin === "model" || info.origin === "fallback";
          const updateCmp = (slot, labelKey, val, selected, tooltip = "") => {
            const lbl = t(labelKey);
            if (slot.labelEl.textContent !== lbl) slot.labelEl.textContent = lbl;
            if (tooltip) slot.labelEl.setAttribute("title", tooltip);
            else slot.labelEl.removeAttribute("title");
            const valText = val == null ? "—" : val.toFixed(3);
            if (slot.valEl.textContent !== valText) slot.valEl.textContent = valText;
            slot.bar.value = val ?? 0;
            slot.bar.hidden = val == null;
            slot.item.setAttribute("data-selected", String(selected));
            slot.item.setAttribute("data-available", String(val != null));
            slot.item.setAttribute("data-active", String(val != null && val >= 0.02));
          };
          entry.compare.wrap.setAttribute("data-single", String(!hasNativeSlot));
          entry.compare.native.item.hidden = !hasNativeSlot;
          if (hasNativeSlot) {
            updateCmp(
              entry.compare.native,
              mergedParent ? "exprCompareNativeMerged" : "exprCompareNative",
              nativeVal,
              nativeSelected,
              mergedParent ? `${entry.name} ← ${mergedParent}` : "",
            );
          }
          updateCmp(entry.compare.model, "exprCompareModel", modelVal, modelSelected);
        }
      }
    }

    const isDefaultPins = state.pinnedChannels.length === PRIMARY_CHANNELS.length &&
      state.pinnedChannels.every((name, idx) => name === PRIMARY_CHANNELS[idx]);
    get("outputs").hidden = state.pinnedChannels.length === 0;
    get("expr-pins-empty").hidden = state.pinnedChannels.length > 0;
    get("expr-pins-clear").hidden = state.pinnedChannels.length === 0;
    get("expr-pins-reset").hidden = isDefaultPins;

    let validCount = 0, activeCount = 0, nativeLiveCount = 0, modelActiveCount = 0;
    for (const name of uniqueChannels) {
      const value = channelValue(snap, name, snapValid);
      const info = channelOrigin(snap, snapValid, name, activeSources);
      if (value != null) {
        validCount++;
        if (value >= 0.02) activeCount++;
        if (info.origin === "native") nativeLiveCount++;
        else if (info.origin === "model" || info.origin === "fallback") modelActiveCount++;
      }
    }

    const freezeBtn = get("expr-freeze");
    freezeBtn.disabled = !current.valid && !state.exprFrozen;
    freezeBtn.textContent = t(state.exprFrozen ? "exprResume" : "exprFreeze");
    freezeBtn.setAttribute("aria-pressed", String(state.exprFrozen));
    get("expr-live-pill").textContent = t(state.exprFrozen ? "exprFrozen" : current.valid ? "exprLive" : current.running ? "exprWaiting" : "exprIdle");
    get("expr-live-pill").setAttribute("data-state", state.exprFrozen ? "frozen" : current.valid ? "running" : current.running ? "connecting" : "stopped");
    get("expr-active-summary").textContent = `${t("exprStatActive")} ${activeCount} · ${t("exprStatValid")} ${validCount}/${uniqueChannels.length}`;
    get("expr-source-summary").textContent = `${t("exprStatNative")} ${nativeLiveCount} · ${t("exprStatModel")} ${modelActiveCount}`;
    get("support-info").textContent = `${t("supported")}: ${(snap ?? state.status)?.model?.trained_channels?.length ?? 0} / 50`;

    get("diag-expr-badge").textContent = state.exprFrozen ? t("exprFrozen") : current.valid ? `${activeCount} / ${validCount}` : t("connStatusIdle");
    get("diag-expr-badge").setAttribute("data-live", String(current.valid && !state.exprFrozen));

    get("expr-compare-toggle").setAttribute("aria-pressed", String(state.exprCompare));
    get("expr-compare-toggle").setAttribute("class", `filter-chip${state.exprCompare ? " active" : ""}`);
    get("expr-active-only").setAttribute("aria-pressed", String(state.exprActiveOnly));
    get("expr-active-only").setAttribute("class", `filter-chip${state.exprActiveOnly ? " active" : ""}`);
    for (const [id, srcKey] of [["expr-src-all", "all"], ["expr-src-native", "native"], ["expr-src-model", "model"]]) {
      const selected = state.exprSourceFilter === srcKey;
      get(id).setAttribute("aria-pressed", String(selected));
      get(id).setAttribute("class", `filter-chip${selected ? " active" : ""}`);
    }
    get("expr-search").setAttribute("placeholder", t("exprSearchPlaceholder"));
    get("expr-search").value = state.exprQuery;
    for (const [regionId, {btn, labelKey}] of exprRegionBtns) {
      const selected = state.exprRegion === regionId;
      btn.textContent = t(labelKey);
      btn.setAttribute("aria-pressed", String(selected));
      btn.setAttribute("class", `expr-region-btn${selected ? " active" : ""}`);
    }

    const query = state.exprQuery.trim().toLowerCase();
    let visibleGroups = 0;
    for (const g of exprGroups) {
      g.titleEl.textContent = g.group.title?.[state.language] ?? g.group.title?.en ?? t(g.group.shortKey);
      const regionMatch = state.exprRegion === "all" || state.exprRegion === g.group.id;
      let groupActive = 0, visibleInGroup = 0;
      for (const cell of g.cells) {
        const value = channelValue(snap, cell.name, snapValid);
        const info = channelOrigin(snap, snapValid, cell.name, activeSources);
        const isActive = value != null && value >= 0.02;
        if (isActive) groupActive++;
        const matchesActive = !state.exprActiveOnly || isActive;
        const matchesSource = state.exprSourceFilter === "all" ||
          (state.exprSourceFilter === "native" && (info.origin === "native" || info.origin === "stock-idle" || info.origin === "fallback")) ||
          (state.exprSourceFilter === "model" && info.origin === "model");
        const meta = cell.meta;
        const matchesQuery = !query || cell.name.toLowerCase().includes(query) ||
          (meta?.parent && meta.parent.toLowerCase().includes(query)) ||
          (meta?.label?.en && meta.label.en.toLowerCase().includes(query)) ||
          (meta?.label?.["zh-CN"] && meta.label["zh-CN"].toLowerCase().includes(query));
        const showCell = regionMatch && matchesActive && matchesSource && matchesQuery;
        cell.cellEl.hidden = !showCell;
        if (showCell) visibleInGroup++;
      }
      g.badgeEl.textContent = `${t("exprStatActive")} ${groupActive} / ${g.cells.length}`;
      g.groupEl.hidden = visibleInGroup === 0;
      if (visibleInGroup > 0) visibleGroups++;
    }
    get("expr-empty").hidden = visibleGroups > 0;
  }
  function renderDiagnostics(current, technicalParts) {
    const showExpr = state.diagView !== "connection";
    get("expression-diagnostics").hidden = !showExpr;
    get("diagnostics").hidden = showExpr;
    get("diag-tab-expressions").setAttribute("aria-selected", String(showExpr));
    get("diag-tab-expressions").setAttribute("class", `diag-tab${showExpr ? " active" : ""}`);
    get("diag-tab-connection").setAttribute("aria-selected", String(!showExpr));
    get("diag-tab-connection").setAttribute("class", `diag-tab${!showExpr ? " active" : ""}`);
    renderExpressionPanel(current);

    const identity = value => typeof value === "number" && !Number.isSafeInteger(value) ? t("unsafeInteger") : display(value);
    const source = state.status?.source ?? {}, result = state.status?.prediction_source ?? {}, m = current.current ? state.status?.metrics ?? {} : {};
    get("source-info").textContent = `${identity(source.session_id)} / ${identity(source.source_seq)}`;
    get("result-info").textContent = `${identity(result.session_id)} / ${identity(result.source_seq)}`;
    get("raw-identity").textContent = identity(source.raw_identity);
    get("time-mouth").textContent = identity(source.mouth_sensor_ns);
    get("engine-info").textContent = `${display(state.status?.engine)} / ${display(state.status?.device)}`;
    get("provider-info").textContent = state.status?.model?.providers?.join(" → ") || "—";
    get("fallback-info").textContent = state.status?.model?.fallbacks?.map(x => `${x.engine}/${x.device}: ${x.error}`).join("\n") || t("noFallback");
    get("source-age").textContent = `${fixed(m.source_age_ms)} ms`;
    get("receive-age").textContent = `${fixed(m.receive_age_ms)} ms`;
    get("rates").textContent = `${fixed(m.received_hz)} / ${fixed(m.inference_hz)} Hz`;
    get("drops").textContent = `${display(m.source_gaps)} / ${display(m.pending_dropped)}`;
    get("queue").textContent = `${display(m.queue_depth)} / ${fixed(m.queue_ms)} ms`;
    get("inference-time").textContent = percentile(m.latency_ms?.predict_ms);
    get("latency").textContent = percentile(m.latency_ms?.source_copy_to_result_ms);
    const liveStock = state.status?.live_stock;
    get("live-stock-info").textContent = !liveStock?.wanted ? t("liveStockOff") :
      liveStock.fresh ? `${t("liveStockFresh")} (${fixed(liveStock.age_ms)} ms)` :
      liveStock.error ? `${t("liveStockWaiting")} · ${liveStock.error}` : t("liveStockWaiting");
    const stock = state.status?.stock;
    const association = stock?.associated > 0 ? "associationExact" : stock?.records > 0 ? "associationUnverified" : "associationWaiting";
    get("stock-info").textContent = `${t(association)} · ${JSON.stringify(stock ?? null)}`;
    get("snapshot-state").textContent = t(current.current ? "snapshot" : "expiredSnapshot");
    get("conn-error-card").setAttribute("data-has-error", String(technicalParts.length > 0));
    get("diag-conn-badge").setAttribute("data-alert", String(technicalParts.length > 0));
    get("diag-conn-badge").setAttribute("data-live", String(technicalParts.length === 0 && current.current && current.running && current.video));
    get("diag-conn-badge").textContent = technicalParts.length > 0 ? `${t("connStatusAlert")} (${technicalParts.length})` :
      current.current && current.running && current.video ? `${fixed(m.inference_hz)} Hz` :
      t(current.current ? "connStatusIdle" : "offline");
  }
  function renderRouting(current) {
    makeMorphControls();
    const capturePending = ["starting", "recording", "stopping"].includes(state.status?.capture_control?.state);
    const routingDisabled = Boolean(state.busy) || !state.config || Boolean(state.status?.read_only) || capturePending;
    const sources = state.channelSources ?? MOUTH_PRESETS.curated;
    const activePreset = matchingPreset(sources);
    const presetStateKey = {curated: "presetStateCurated", hybrid: "presetStateHybrid", model_only: "presetStateModel", custom: "presetStateCustom"}[activePreset];
    get("routing-preset-badge").textContent = t(presetStateKey);
    get("routing-preset-badge").setAttribute("data-preset", activePreset);
    for (const [id, mode] of [["preset-curated", "curated"], ["preset-hybrid", "hybrid"], ["preset-model-only", "model_only"]]) {
      const selected = activePreset === mode;
      get(id).disabled = routingDisabled;
      get(id).setAttribute("aria-pressed", String(selected));
      get(id).setAttribute("class", `preset-card${selected ? " active" : ""}`);
    }
    for (const [id, filterKey] of [["filter-all", "all"], ["filter-stock", "stock"], ["filter-model", "model"], ["filter-good", "good"]]) {
      const selected = state.channelFilter === filterKey;
      get(id).setAttribute("aria-pressed", String(selected));
      get(id).setAttribute("class", `filter-chip${selected ? " active" : ""}`);
    }
    get("channel-search").setAttribute("placeholder", t("channelSearchPlaceholder"));
    get("channel-search").value = state.channelQuery;
    const query = state.channelQuery.trim().toLowerCase();
    const tagKey = {good: "tagGood", merged: "tagMerged", weak: "tagWeak", missing: "tagMissing"};
    let stockCount = 0, modelCount = 0, offCount = 0;
    for (const g of morphGroups) {
      g.titleEl.textContent = g.group.title[state.language] ?? g.group.title.en;
      if (g.allStockBtn) {
        g.allStockBtn.textContent = t("groupAllStock");
        g.allStockBtn.disabled = routingDisabled;
      }
      g.allModelBtn.textContent = t("groupAllModel");
      g.allModelBtn.disabled = routingDisabled;
      let groupStock = 0, visibleInGroup = 0;
      for (const entry of g.items) {
        const {item, rowEl, titleSpan, tagEl, seg, buttons} = entry;
        const chosen = sources[item.name] ?? "model";
        if (chosen === "stock") { stockCount++; groupStock++; }
        else if (chosen === "off") offCount++;
        else modelCount++;
        const label = item.label[state.language] ?? item.label.en;
        titleSpan.textContent = label;
        tagEl.textContent = t(tagKey[item.tag]);
        seg.setAttribute("aria-label", label);
        for (const [choice, btn] of Object.entries(buttons)) {
          const active = chosen === choice;
          btn.disabled = routingDisabled;
          btn.textContent = t(choice === "stock" ? "sourceStock" : choice === "off" ? "sourceOff" : "sourceModel");
          btn.setAttribute("aria-pressed", String(active));
          btn.setAttribute("class", `source-btn${active ? " active" : ""}`);
        }
        const matchesFilter = state.channelFilter === "all" ||
          (state.channelFilter === "stock" && chosen === "stock") ||
          (state.channelFilter === "model" && chosen === "model") ||
          (state.channelFilter === "good" && (item.tag === "good" || item.tag === "merged"));
        const matchesQuery = !query || item.name.toLowerCase().includes(query) ||
          (item.parent && item.parent.toLowerCase().includes(query)) ||
          item.label.en.toLowerCase().includes(query) ||
          item.label["zh-CN"].toLowerCase().includes(query);
        const show = matchesFilter && matchesQuery;
        rowEl.hidden = !show;
        if (show) visibleInGroup++;
      }
      g.countEl.textContent = `${t("hybridSummaryNative")} ${groupStock} / ${g.items.length}`;
      g.section.hidden = visibleInGroup === 0;
    }
    const summaryParts = [`${t("hybridSummaryNative")} ${stockCount}`, `${t("hybridSummaryModel")} ${modelCount}`];
    if (offCount > 0) summaryParts.push(`${t("hybridSummaryOff")} ${offCount}`);
    get("hybrid-channels-summary").textContent = summaryParts.join(" · ");
    const showLiveHint = stockCount > 0 && current.running && !state.status?.capture_only && state.status?.source_mode === "live";
    get("hybrid-live-hint").hidden = !showLiveHint;
    if (showLiveHint) get("hybrid-live-hint").textContent = t(state.status?.live_stock?.fresh ? "hybridStockFresh" : "hybridStockWaiting");
  }
  function applyConfig(config) {
    state.config = config;
    if (!state.dirty) {
      makeMorphControls();
      const legacySmoothing = Number(config.smoothing_ms ?? 20);
      const fallbackCutoff = legacySmoothing > 0 ? Number((1000 / (2 * Math.PI * legacySmoothing)).toFixed(2)) : 8;
      const defaults = {filter_min_cutoff: fallbackCutoff, filter_beta: 4, filter_d_cutoff: 1};
      for (const key of [...TEXT_FIELDS, ...NUMBER_FIELDS]) {
        const value = config[key] ?? defaults[key];
        get(key).value = value == null ? "" : String(value);
      }
      get("output_enabled").checked = config.output_enabled === true;
      get("filter_enabled").checked = config.filter_enabled !== undefined ? config.filter_enabled === true : legacySmoothing > 0;
      state.channelSources = resolveChannelSources(config.mouth_mode, config.hybrid_channels, config.channel_sources);
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
      headset_host: get("headset_host").value.trim(),
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
    const sealed = Boolean(state.status?.capture_control?.sealed_c);
    const readOnly = Boolean(state.status?.read_only);
    get("cymple-relay").hidden = sealed || readOnly;
    const relay = state.status?.cymple_relay;
    const relayRunning = relay?.running === true;
    const capturePending = ["starting", "recording", "stopping"].includes(state.status?.capture_control?.state);
    const blocked = !current.current || Boolean(state.busy) || sealed || readOnly || capturePending;
    const relayState = current.current ? (relay?.state ?? "idle") : "offline";
    const statusKey = {idle: "cympleStateIdle", connecting: "cympleStateConnecting", running: "cympleStateRunning",
      reconnecting: "cympleStateReconnecting", offline: "offline"}[relayState] ?? "cympleStateIdle";
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
      const eyeStateKey = {idle: "cympleStateIdle", connecting: "cympleStateConnecting",
        streaming: "cympleStateStreaming", reconnecting: "cympleStateReconnecting"}[eye?.state ?? "idle"] ?? "cympleStateIdle";
      get(`cymple-${side}-state`).textContent = t(eyeStateKey);
      get(`cymple-${side}-metrics`).textContent = !eye || (!relayRunning && !(eye.frames > 0)) ? "—" :
        `${fixed(eye.fps)} fps · ${fixed(eye.avg_kb)} KB/f · ${eye.frames} ${t("cympleRelayFrames")}`;
    }
    const eyeErrors = current.current ? ["eye_left", "eye_right"].map(k => relay?.eyes?.[k]?.error).filter(Boolean) : [];
    const rawErr = state.relayError || (current.current ? relay?.error : "") || eyeErrors.join(" · ");
    get("cymple-relay-error").hidden = !state.relayErrorKey && !rawErr;
    get("cymple-relay-error").textContent = state.relayErrorKey ? t(state.relayErrorKey) : rawErr ? errorMessage(state.language, rawErr) : "";
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
    get("connection-setup").hidden = !["tracking", "recording"].includes(state.page);
    get("connection-setup").setAttribute("class", `setup-grid${state.page === "recording" ? " capture-setup" : ""}`);
    get("model-setup").hidden = state.page !== "tracking";
    get("first-model-help").hidden = Boolean(get("model_path").value.trim());
    if (["tracking", "recording"].includes(state.page) && state.cameraPage !== state.page) {
      get(`camera-slot-${state.page}`).append(get("camera-panel")); state.cameraPage = state.page;
    }
  }
  function navigate(page, updateHash = true) {
    if (!PAGES.includes(page) || state.destroyed) return;
    state.page = page; render();
    if (updateHash && navigation && navigation.location.hash !== `#/${page}`) navigation.location.hash = `#/${page}`;
    if (updateHash) { navigation?.scrollTo?.({top: 0, behavior: "auto"}); get("title").focus?.({preventScroll: true}); }
  }
  const hashChanged = () => navigate(routePage(navigation.location.hash), false);
  function renderConnection(current) {
    const host = get("headset_host").value.trim(), port = Number(get("headset_port").value), selected = host ? `${host}:${port}` : "";
    const key = JSON.stringify([state.discovered, selected, state.language]);
    if (key !== state.discoveryKey) {
      state.discoveryKey = key; const selector = get("headset-select"); selector.replaceChildren();
      const add = (value, title) => { const option = doc.createElement("option"); option.value = value; option.textContent = title; selector.append(option); };
      add("", t(state.discovered.length ? "chooseHeadset" : "noHeadset"));
      for (const device of state.discovered) add(`${device.host}:${device.port}`, `${device.name} · ${device.host}`);
      if (host && !state.discovered.some(item => item.host === host && item.port === port)) add(selected, `${t("manualHeadset")} · ${host}`);
      selector.value = selected;
    }
    const verified = state.discovered.some(item => item.host === host && item.port === port);
    get("discover").textContent = t(state.discovering ? "discovering" : "discover");
    get("discover").disabled = state.discovering || !current.current || Boolean(state.busy) || current.running;
    get("headset-select").disabled = state.discovering || Boolean(state.busy) || current.running || !current.current;
    get("discovery-badge").textContent = t(verified ? "headsetFound" : host ? "manualHeadset" : "autoDiscovery");
    get("discovery-info").textContent = state.discoveryError ? errorMessage(state.language, state.discoveryError) :
      t(state.discovering ? "discoverySearching" : state.discovered.length ? "discoveryFound" : state.discoveryDone ? "discoveryEmpty" : "discoveryHelp");
    const path = get("model_path").value.trim();
    get("model-name").textContent = path ? path.split(/[\\/]/).pop() : t("noModel");
    get("model-location").textContent = path || t("modelOptional");
    get("model-location").setAttribute("title", path);
    get("model-browse").textContent = t(state.pickingModel ? "choosingModel" : "browseModel");
    get("model-browse").disabled = state.pickingModel || !current.current || Boolean(state.busy) || current.running;
  }
  async function discover() {
    if (state.discovering || state.destroyed || view().running || state.status?.read_only) return;
    state.discovering = true; state.discoveryError = ""; render();
    try {
      const result = await requestJson(fetcher, "/api/discovery", undefined, 10000);
      if (state.destroyed) return;
      state.discovered = result.devices; state.discoveryDone = true;
      if (result.devices.length === 1 && !state.dirty && !view().running) {
        const device = result.devices[0];
        applyDiscoveredEyePorts(device);
        if (get("headset_host").value !== device.host || Number(get("headset_port").value) !== device.port) {
          get("headset_host").value = device.host; get("headset_port").value = String(device.port); state.dirty = true;
        }
      }
    } catch (error) { if (!state.destroyed) { state.discovered = []; state.discoveryError = error.message; } }
    finally { state.discovering = false; if (!state.destroyed) render(); }
  }
  async function chooseModel() {
    if (get("model-browse").disabled || state.destroyed) return;
    state.pickingModel = true; state.error = ""; render();
    try {
      const result = await requestJson(fetcher, "/api/model/select", {initial: get("model_path").value.trim()}, 300000);
      if (!state.destroyed && !result.cancelled) { get("model_path").value = result.path; state.dirty = true; }
    } catch (error) { if (!state.destroyed) state.error = error.message; }
    finally { state.pickingModel = false; if (!state.destroyed) render(); }
  }
  async function pollStatus() {
    if (state.polling || state.busy || state.destroyed) return; state.polling = true; const generation = state.generation;
    try {
      const status = await requestJson(fetcher, "/api/status");
      if (generation !== state.generation || state.destroyed) return;
      state.status = status; applyConfig(status.config ?? await requestJson(fetcher, "/api/config")); applyCympleRelay(status.cymple_relay); state.online = true; state.at = now(); state.pollError = "";
      const record=status.recording;
      const saved=status.managed_capture && record?.state==="stopped" && record.session_id &&
        ["completed","error"].includes(status.capture_control?.state);
      if (saved && state.archiveKey!==record.session_id) {
        state.archiveKey=record.session_id;
        void loadPresets();
      }
    } catch (error) { if (generation === state.generation && !state.destroyed) { state.online = false; state.pollError = error.message; } }
    finally { state.polling = false; if (!state.destroyed) render(); }
  }
  function readForm() {
    makeMorphControls();
    const config = Object.fromEntries(TEXT_FIELDS.map(key => [key, get(key).value.trim()]));
    for (const key of NUMBER_FIELDS) config[key] = Number(get(key).value);
    const channelSources = resolveChannelSources("curated", null, state.channelSources);
    const mouth_mode = matchingPreset(channelSources);
    const channel_sources = mouth_mode === "custom" ? channelSources : null;
    const filter_enabled = get("filter_enabled").checked;
    const smoothing_ms = !filter_enabled ? 0 : (config.filter_min_cutoff > 0 ? Number((1000 / (2 * Math.PI * config.filter_min_cutoff)).toFixed(2)) : 0);
    return {...config, filter_enabled, smoothing_ms, mouth_mode, hybrid_channels: null, channel_sources, output_enabled: get("output_enabled").checked, output_host: "127.0.0.1", model_kind: "face50"};
  }
  async function control(key, action) {
    if (state.busy || state.destroyed || state.status?.read_only) return; state.busy = key; state.generation++; state.error = ""; state.errorKey = ""; render();
    try { await action(); state.status = await requestJson(fetcher, "/api/status"); applyConfig(state.status.config ?? state.config); applyCympleRelay(state.status.cymple_relay); state.online = true; state.at = now(); state.pollError = ""; }
    catch (error) { state.error = error.message; }
    finally { state.busy = ""; if (!state.destroyed) render(); }
  }
  async function persistConfig() { const saved = await requestJson(fetcher, "/api/config", readForm(), 20000); state.dirty = false; applyConfig(saved); }
  function validate(mode, modelRequired = true) {
    if (modelRequired && !get("model_path").value.trim() || mode === "live" && !get("headset_host").value.trim()) state.errorKey = "required";
    else if (!get("settings").checkValidity()) state.errorKey = "invalidSettings";
    else return true; render(); return false;
  }
  async function save() { if (!get("save").disabled && validate("save", false)) await control("saving", persistConfig); }
  async function start(mode = "live") {
    if (get(mode === "capture" ? "capture" : "start").disabled || !validate(mode === "capture" ? "live" : mode, mode !== "capture")) return;
    await control("starting", async () => { await persistConfig(); await requestJson(fetcher, "/api/start", {mode}, 60000); });
  }
  async function stop() { if (!get("stop").disabled) await control("stopping", () => requestJson(fetcher, "/api/stop", {}, 20000)); }
  async function cympleRelay(action) {
    if (state.status?.read_only || state.status?.capture_control?.sealed_c) return;
    const btn = get(action === "start" ? "cymple-relay-start" : "cymple-relay-stop");
    if (btn.disabled) return;
    state.relayError = ""; state.relayErrorKey = "";
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
    });
  }
  async function record(action, stepIndex) {
    const button = {begin: "record-start", stop: "record-stop", cancel: "record-cancel", ready: "guide-ready", finish: "guide-finish", confirm: "guide-confirm", skip: "guide-skip", pause:"guide-pause",resume:"guide-pause",select_step:"guide-select"}[action]; if (!button || get(button).disabled) return;
    if (action === "begin" && !validate(state.status?.managed_capture ? "capture" : "live", false)) return;
    await control("working", async () => {
      if (action === "begin" && state.status?.managed_capture && state.dirty) await persistConfig();
      await requestJson(fetcher, "/api/recording", {action, ...(action === "begin" ? {person_id: get("person_id").value.trim(), confirmed: get("record-consent").checked, guide: get("guide-enabled").checked, stage: get("label-stage").value,
        receive_results: get("receive-results").checked,
        ...(get("guide-enabled").checked ? {preset_id:state.selectedPreset,automatic:get("guide-mode").value!=="manual",
          prepare_seconds:get("guide-mode").value==="manual"?7:Number(get("guide-prepare").value || 7), step_index:state.startStep,
          ...(state.selectedPreset==="continuation" && state.continuation?.session_id ? {continuation_session_id:state.continuation.session_id} : {})} : {})} :
        {guide_frame:state.status?.recording?.guide?.frame,step_index:stepIndex})}, 20000);
      if(action==="begin")state.presetPreview=false;
      get("record-consent").checked = false;
    });
  }
  async function train(action = "begin") {
    if (get(action === "cancel" ? "train-cancel" : "train-start").disabled) return;
    const args = Object.fromEntries([...trainInputs].map(([name, {input}]) => [name, input.type === "number" ? Number(input.value) : input.value.trim()]));
    await control("working", () => requestJson(fetcher, "/api/training", {action, operation: get("train-action").value, args}, 20000));
  }
  async function refreshPreview() {
    if (state.destroyed || state.busy || state.previewBusy || doc.hidden || !["tracking", "recording"].includes(state.page) || !get("preview-enabled").checked || !view().video) return;
    state.previewBusy = true; const generation = state.previewGeneration, controller = new AbortController(); state.previewAbort = controller;
    const timeout = timers.setTimeout(() => controller.abort(), 2500);
    try {
      const response = await fetcher(`/api/preview.jpg?t=${now()}`, {cache: "no-store", signal: controller.signal});
      if (generation !== state.previewGeneration || state.destroyed || !view().video) return;
      if (response.status === 204) { clearPreview(); return; } if (!response.ok) throw Error(`HTTP ${response.status}`);
      const blob = await response.blob(); if (generation !== state.previewGeneration || state.destroyed || !view().video) return;
      if (state.previewUrl) urls.revokeObjectURL(state.previewUrl); state.previewUrl = urls.createObjectURL(blob); state.previewError = false;
      get("preview").src = state.previewUrl; get("preview").hidden = false; get("placeholder").hidden = true; get("empty-camera").hidden = true;
    } catch { if (generation === state.previewGeneration && !state.destroyed) { state.previewError = true; clearPreview(); render(); } }
    finally { timers.clearTimeout(timeout); state.previewBusy = false; state.previewAbort = null; }
  }
  function isExpressionMonitorLive() {
    return !state.destroyed && !state.busy && !doc.hidden &&
      state.page === "diagnostics" && state.diagView !== "connection" &&
      !state.exprFrozen && state.status?.running === true && !state.exprEndpointMissing;
  }
  async function pollExpressions() {
    if (state.exprPromise) return state.exprPromise;
    if (!isExpressionMonitorLive()) return;
    const generation = state.generation;
    state.exprPromise = (async () => {
      try {
        const response = await fetcher("/api/expressions", {method: "GET", cache: "no-store"});
        if (generation !== state.generation || state.destroyed) return;
        if (response.status === 404) { state.exprEndpointMissing = true; return; }
        if (!response.ok) return;
        const snap = await response.json();
        if (generation !== state.generation || state.destroyed || !state.status) return;
        const prevRunning = state.status.running;
        state.status = {
          ...state.status,
          running: snap.running,
          connected: snap.connected,
          source_mode: snap.source_mode,
          capture_only: snap.capture_only,
          prediction: snap.prediction,
          live_stock: snap.live_stock ?? state.status.live_stock,
          metrics: {...(state.status.metrics ?? {}), ...(snap.metrics ?? {})},
        };
        state.online = true;
        state.at = now();
        state.pollError = "";
        if (prevRunning !== snap.running) render();
        else renderExpressionPanel(view());
      } catch {}
      finally { state.exprPromise = null; }
    })();
    return state.exprPromise;
  }
  function scheduleExpressions(delayMs) {
    if (state.destroyed) return;
    timers.clearTimeout(state.exprTimer);
    state.exprTimer = timers.setTimeout(expressionLoop, delayMs);
  }
  async function expressionLoop() {
    if (state.destroyed) return;
    if (isExpressionMonitorLive()) {
      await pollExpressions();
      scheduleExpressions(EXPRESSION_POLL_MS);
    } else {
      scheduleExpressions(250);
    }
  }
  function previewLoop() { void refreshPreview(); if (!state.destroyed) state.previewTimer = timers.setTimeout(previewLoop, 1000/Math.min(10, Math.max(1, state.config?.preview_fps ?? 10))); }
  function setLanguage(language) { if (!Object.hasOwn(messages, language)) return; state.language = language; try { storage?.setItem(LANGUAGE_KEY, language); } catch {} render(); }
  get("settings").addEventListener("submit", event => { event.preventDefault(); return start(); });
  for (const event of ["input", "change"]) {
    const changed = () => { state.dirty = true; state.errorKey = ""; render(); };
    get("settings").addEventListener(event, e => { if (!e?.target || [...TEXT_FIELDS, ...NUMBER_FIELDS, ...CHECK_FIELDS].includes(e.target.id)) changed(); });
    get("page-preferences").addEventListener(event, e => { if (!e?.target || [...TEXT_FIELDS, ...NUMBER_FIELDS, ...CHECK_FIELDS].includes(e.target.id)) changed(); });
  }
  for (const [id, mode] of [["preset-curated", "curated"], ["preset-hybrid", "hybrid"], ["preset-model-only", "model_only"]]) {
    get(id).addEventListener("click", () => {
      state.channelSources = {...MOUTH_PRESETS[mode]};
      state.dirty = true; state.errorKey = ""; render();
    });
  }
  for (const [id, filterKey] of [["filter-all", "all"], ["filter-stock", "stock"], ["filter-model", "model"], ["filter-good", "good"]]) {
    get(id).addEventListener("click", () => { state.channelFilter = filterKey; render(); });
  }
  get("channel-search").addEventListener("input", () => { state.channelQuery = get("channel-search").value; render(); });
  get("discover").addEventListener("click", discover);
  get("model-browse").addEventListener("click", chooseModel);
  get("headset-select").addEventListener("change", () => {
    const device = state.discovered.find(item => `${item.host}:${item.port}` === get("headset-select").value);
    if (device) { get("headset_host").value = device.host; get("headset_port").value = String(device.port); applyDiscoveredEyePorts(device); state.dirty = true; render(); }
    else if (!get("headset-select").value) { get("headset_host").value = ""; state.dirty = true; render(); }
  });
  for (const page of PAGES) get(`nav-${PAGE_NAV[page]}`).addEventListener("click", event => { event.preventDefault(); navigate(page); });
  get("go-diagnostics").addEventListener("click", event => { event.preventDefault(); state.diagView = "expressions"; navigate("diagnostics"); void pollExpressions(); });
  get("settings-go-diagnostics").addEventListener("click", event => { event.preventDefault(); navigate("diagnostics"); void pollExpressions(); });
  get("diag-tab-expressions").addEventListener("click", () => { state.diagView = "expressions"; render(); void pollExpressions(); });
  get("diag-tab-connection").addEventListener("click", () => { state.diagView = "connection"; render(); });
  get("expr-pins-clear").addEventListener("click", clearPins);
  get("expr-pins-reset").addEventListener("click", resetPins);
  get("expr-freeze").addEventListener("click", () => {
    if (get("expr-freeze").disabled) return;
    if (state.exprFrozen) {
      state.exprFrozen = false;
      state.frozenSnapshot = null;
    } else {
      state.exprFrozen = true;
      state.frozenSnapshot = {status: structuredClone(state.status), valid: view().valid && !state.busy};
    }
    render();
    if (!state.exprFrozen) void pollExpressions();
  });
  get("expr-compare-toggle").addEventListener("click", () => { state.exprCompare = !state.exprCompare; render(); });
  get("expr-active-only").addEventListener("click", () => { state.exprActiveOnly = !state.exprActiveOnly; render(); });
  for (const [id, srcKey] of [["expr-src-all", "all"], ["expr-src-native", "native"], ["expr-src-model", "model"]]) {
    get(id).addEventListener("click", () => { state.exprSourceFilter = srcKey; render(); });
  }
  get("expr-search").addEventListener("input", () => { state.exprQuery = get("expr-search").value; render(); });
  for (const id of ["go-capture", "train-go-record", "train-go-review"]) get(id).addEventListener("click", event => {
    event.preventDefault(); navigate("recording"); if (id === "train-go-review") get("review").open = true;
  });
  navigation?.addEventListener("hashchange", hashChanged);
  get("language").addEventListener("change", () => setLanguage(get("language").value));
  get("guide-pause").addEventListener("click",()=>record(state.status?.recording?.guide?.paused?"resume":"pause"));
  get("record-continue").addEventListener("click",()=>selectPreset("continuation"));
  get("guide-select").addEventListener("change",()=>selectStep(Number(get("guide-select").value)));
  for (const [id, delta] of [["guide-prev",-1],["guide-next",1]]) get(id).addEventListener("click",()=>{
    if (!get(id).disabled) return selectStep(Number(get("guide-select").value)+delta);
  });
  for (const [id, fn] of [["save", save], ["stop", stop], ["capture-stop", stop], ["cymple-relay-start", () => cympleRelay("start")], ["cymple-relay-stop", () => cympleRelay("stop")], ["capture", () => start("capture")], ["replay", () => start("replay")], ["record-start", () => record("begin")], ["record-stop", () => record("stop")], ["record-cancel", () => record("cancel")], ["train-start", () => train()], ["train-cancel", () => train("cancel")], ...["ready", "finish", "confirm", "skip"].map(action => [`guide-${action}`, () => record(action)])]) get(id).addEventListener("click", fn);
  for (const id of RELAY_OPTION_IDS) {
    for (const event of ["input", "change"]) {
      get(id).addEventListener(event, () => {
        state.relayDirty = true;
        if (id === "cymple-left-port" || id === "cymple-right-port") state.relayPortsManual = true;
        state.relayError = ""; state.relayErrorKey = "";
        render();
      });
    }
  }
  for (const id of ["preview-enabled", "record-consent", "voice-enabled", "person_id", "label-stage",
    "guide-enabled", "guide-mode", "guide-prepare"]) get(id).addEventListener("change", render);
  for (const id of ["review-confirmed", "reviewer"]) get(id).addEventListener("change", render);
  get("review-load").addEventListener("click", () => loadReviews()); get("review-prev").addEventListener("click", () => loadReviews(Math.max(0, state.reviews.offset-24)));
  get("review-next").addEventListener("click", () => loadReviews(state.reviews.offset+24));
  get("review-select-page").addEventListener("click", () => { for (const [i, row] of get("review-grid").children.entries()) { row.children[0].checked = true; state.reviewSelected.add(state.reviews.samples[i].sample_id); } render(); });
  get("review-proposal").addEventListener("click", () => { if (state.reviewFocus?.prompt_candidate) get("review-targets").value = JSON.stringify(state.reviewFocus.prompt_candidate.labels, null, 2); });
  get("review-apply").addEventListener("click", applyReview);
  get("train-action").addEventListener("change", buildTrainInputs); get("preview").addEventListener("error", () => { state.previewError = true; clearPreview(); render(); });
  get("reference-select").addEventListener("change", () => selectReference(get("reference-select").value));
  get("capture-preset").addEventListener("change",()=>selectPreset(get("capture-preset").value));
  for (const [id, delta] of [["reference-prev", -1], ["reference-next", 1]]) get(id).addEventListener("click", () => {
    const poses = practicePoses(), index = poses.findIndex(pose => pose.id === state.referenceSelected);
    if (!get(id).disabled) selectReference(poses[index+delta].id);
  });
  get("reference-play").addEventListener("click", () => { if (!get("reference-play").disabled && !get("reference-play").hidden) { state.referenceAnimated = !state.referenceAnimated; render(); } });
  for (const id of ["reference-image", "reference-neutral-image", "reference-profile-image"]) get(id).addEventListener("error", () => { state.referenceImageError = true; render(); });
  async function init() { render(); await Promise.all([pollStatus(), loadReference(), loadPresets()]); if (!state.destroyed) { state.interval = timers.setInterval(() => { render(); void pollStatus(); }, 500); state.visualTimer=timers.setInterval(() => {void reportVisual().catch(error => {state.error=error.message;});},200); previewLoop(); void expressionLoop(); await discover(); } }
  function destroy() { state.destroyed = true; state.generation++; navigation?.removeEventListener("hashchange", hashChanged); speech?.cancel(); timers.clearInterval(state.interval); timers.clearInterval(state.visualTimer); timers.clearTimeout(state.previewTimer); timers.clearTimeout(state.exprTimer); clearPreview(); }
  return {init, destroy, start, stop, save, record, train, cympleRelay, loadReviews, applyReview, readForm, pollStatus, pollExpressions, togglePin, clearPins, resetPins, refreshPreview, reportVisual, setLanguage, selectReference, selectPreset, discover, chooseModel, navigate, state, rows, trainInputs, morphControls, morphGroups, exprGroups};
}
if (typeof document !== "undefined") {
  let storage; try { storage = window.localStorage; } catch {}
  const app = createApp(document, {storage, navigation: window, viewer: new URLSearchParams(location.search).get("viewer"),
    languages: navigator.languages ?? [navigator.language]}); void app.init(); window.addEventListener("pagehide", () => app.destroy(), {once: true});
}
