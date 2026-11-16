import {errorMessage, initialLanguage, LANGUAGE_KEY, messages} from "./i18n.mjs";

const TEXT_FIELDS = ["headset_host", "model_path", "device", "replay_path"];
const NUMBER_FIELDS = ["headset_port", "stale_ms", "preview_fps", "output_port"];
export const PRIMARY_CHANNELS = ["JawOpen", "MouthPucker", "MouthSmileL", "MouthSmileR", "TongueOut", "TongueLeft", "TongueRight", "TongueUp", "TongueDown", "TongueRoll"];
export const PAGES = ["tracking", "recording", "training", "preferences"];
const PAGE_NAV = {tracking: "workspace", recording: "recording", training: "training", preferences: "advanced"};
export function routePage(hash = "") { const page = hash.replace(/^#\/?/, ""); return PAGES.includes(page) ? page : "tracking"; }
const LABELS = {title: "title", "host-label": "host", "model-label": "model", "model-help": "modelHelp", start: "start", capture: "capture", stop: "stop",
  "advanced-title": "advanced", "diagnostics-title": "diagnostics", "port-label": "port", "device-label": "device", "expiry-label": "expiry", "fps-label": "fps",
  "udp-label": "udp", "output-port-label": "outputPort", "settings-help": "settingsHelp", save: "save", replay: "replay", "replay-path-label": "replayPath",
  "preview-label": "preview", "all-channels-title": "allChannels", "unknown-help": "unknownHelp", migration: "migration",
  "recording-title": "recordingTitle", "record-help": "recordHelp", "person-label": "person", "guided-label": "guided", "voice-label": "voice", "consent-label": "consent",
  "record-start": "recordStart", "record-stop": "recordStop", "record-cancel": "cancel", "training-title": "trainingTitle", "training-help": "trainingHelp",
  "guide-ready": "guideReady", "guide-finish": "guideFinish", "guide-confirm": "guideConfirm", "guide-skip": "guideSkip",
  "label-stage-label": "labelStage", "raw-only-option": "rawOnly", "instant-option": "instantLabels",
  "review-title": "reviewTitle", "review-help": "reviewHelp", "review-all-label": "reviewAll", "review-load": "reviewLoad", "review-prev": "previous", "review-next": "next", "review-select-page": "selectPage",
  "review-proposal": "reviewProposal", "review-targets-label": "reviewTargets", "reviewer-label": "reviewer", "observation-label": "observation", "visible-option": "visible", "uncertain-option": "uncertain", "occluded-option": "occluded", "no-mouth-option": "noMouth", "review-consent-label": "reviewConsent", "review-apply": "reviewApply",
  "train-action-label": "trainAction", "train-start": "trainStart", "train-cancel": "cancel", "engine-label": "engine", "providers-label": "providers",
  "fallback-label": "fallback", "source-label": "source", "result-label": "result", "source-age-label": "sourceAge", "receive-age-label": "receiveAge",
  "rate-label": "rates", "drop-label": "drops", "queue-label": "queue", "inference-label": "inference", "latency-label": "latency", "stock-label": "stock",
  "reference-title": "referenceTitle", "reference-select-label": "referenceSelect", "reference-prev": "previousPose", "reference-next": "nextPose",
  "reference-neutral-caption": "referenceNeutral", "reference-target-caption": "referenceTarget", "reference-profile-caption": "referenceProfile",
  "reference-directions": "referenceDirections", "reference-details-title": "referenceDetails", "reference-license": "referenceLicense",
  "nav-caption": "navCaption", "nav-live-label": "navLive", "nav-record-label": "navRecord", "nav-train-label": "navTrain", "nav-settings-label": "navSettings",
  "local-label": "localLabel", "privacy-note": "privacyNote", "workspace-caption": "workspaceCaption", "page-eyebrow": "pageEyebrow", "page-intro": "pageIntro",
  "headset-title": "headsetTitle", "headset-select-label": "headsetSelect", "model-path-label": "modelPath", "run-help": "runHelp",
  "camera-eyebrow": "cameraEyebrow", "camera-title": "cameraTitle", "camera-empty-help": "cameraEmptyHelp", "camera-privacy": "cameraPrivacy",
  "output-eyebrow": "outputEyebrow", "output-title": "outputTitle", "output-note": "outputNote", "footer-note": "footerNote",
  "capture-stop": "closeCamera", "first-model-text": "firstModelText", "go-capture": "goCapture",
  "reference-picker-title": "referencePicker", "record-options-title": "recordOptions", "replay-tools-title": "replayTools",
  "train-go-record": "goCapture", "train-go-review": "goReview",
  "train-step-one": "trainStepOne", "train-step-one-help": "trainStepOneHelp", "train-step-two": "trainStepTwo", "train-step-two-help": "trainStepTwoHelp",
  "train-step-three": "trainStepThree", "train-step-three-help": "trainStepThreeHelp", "training-tools-title": "trainingTools", "training-tools-help": "trainingToolsHelp", "technical-error-title": "technicalError"};
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
  if (!valid || !status?.model?.trained_channels?.includes(name)) return null;
  const i = status.contract?.channels?.indexOf(name) ?? -1;
  if (i < 0 || !(BigInt(status.prediction.valid_mask ?? 0) & (1n << BigInt(i)))) return null;
  const value = status.prediction.channels?.[name];
  return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
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
  const phaseKey = !guide ? "captureIdle" : recording?.state === "cancelled" ? "capturePaused" :
    !active && !external && !done ? "capturePaused" :
    ({prepare:"capturePrepare",hold:"visualhold",review:"captureReview",finished:"visualdone"})[phase] ?? "visual"+phase;
  return {total,current,active,phaseKey};
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
    presets: [], selectedPreset: null, presetPreview: false, presetOptionsKey: "", presetListKey: "", externalCase: null};
  const t = key => messages[state.language][key] ?? key, view = () => deriveView(state.status, state.online, now()-state.at);
  const choiceLabel = value => messages[state.language][`choice-${value}`] ?? value;
  const fieldLabel = name => t(name === "device" ? "trainDevice" : name === "split" ? "splitPath" : name);
  const rows = new Map(), trainInputs = new Map(); let rowKey = "", specKey = "";
  function clearPreview() {
    state.previewGeneration++; state.previewAbort?.abort(); get("preview").hidden = true; get("preview").removeAttribute("src");
    if (state.previewUrl) urls.revokeObjectURL(state.previewUrl); state.previewUrl = null; get("placeholder").hidden = false; get("empty-camera").hidden = false;
  }
  function makeRows() {
    const channels = state.status?.contract?.channels ?? PRIMARY_CHANNELS, key = channels.join("/");
    if (rowKey === key) return; rowKey = key; rows.clear(); get("outputs").replaceChildren(); get("channel-table").replaceChildren();
    for (const [container, names] of [["outputs", PRIMARY_CHANNELS], ["channel-table", channels]]) {
      for (const name of names) {
        const label = doc.createElement("label"), title = doc.createElement("span"), output = doc.createElement("output"), bar = doc.createElement("progress");
        title.textContent = name; bar.max = 1; bar.value = 0; bar.setAttribute("aria-label", name);
        label.append(title, output, bar); get(container).append(label); rows.set(`${container}/${name}`, {output, bar, name});
      }
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
    selector.value=state.selectedPreset ?? ""; selector.disabled=active || Boolean(state.busy) || !presets.length;
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
      for (const step of preset?.steps ?? []) {const item=doc.createElement("li");
        item.textContent=step.title?.[language] ?? step.prepare?.[language] ?? step.id;get("capture-action-list").append(item);}
    }
    for(const [index,item] of [...get("capture-action-list").children].entries()) {
      if(progress.current===index+1)item.setAttribute("aria-current","step");else item.removeAttribute("aria-current");
    }
  }
  function selectPreset(id) {
    if (get("capture-preset").disabled || !availablePresets().some(p=>p.id===id)) return;
    state.selectedPreset=id; state.presetPreview=true;
    const first=selectedPreset().steps[0]; state.referenceSelected=first.demo_id ?? first.id;
    state.referenceAnimated=false; render();
  }
  async function loadPresets() {
    try {const result=await requestJson(fetcher,"/api/presets");if(!state.destroyed)state.presets=result.presets;}
    catch(error) {if(!state.destroyed)state.error=error.message;}
    finally {if(!state.destroyed)render();}
  }
  function renderRecord(current) {
    const record = state.status?.recording, active = record?.state === "recording" || record?.state === "collecting";
    get("record-start").disabled = !current.current || !current.running || state.status?.connected !== true || state.status?.source_mode !== "live" || active || Boolean(state.busy) || state.dirty || !get("record-consent").checked || !get("person_id").value.trim();
    get("record-stop").disabled = !current.current || !active || Boolean(state.busy); get("record-cancel").disabled = get("record-stop").disabled;
    get("record-start").hidden = active; get("record-stop").hidden = !active; get("record-cancel").hidden = !active;
    get("record-consent").disabled = active || Boolean(state.busy); get("person_id").disabled = active || Boolean(state.busy);
    get("record-state").textContent = current.current ? t(record?.state ?? "idle") : t("offline");
    get("capture-stop").disabled = get("stop").disabled;
    const next = !get("headset_host").value.trim() ? "recordNeedHeadset" : !current.running ? "recordOpenCamera" :
      !current.video ? "recordWaitCamera" : !get("record-consent").checked ? "recordNeedConsent" : "recordReady";
    get("record-next").textContent = active ? "" : t(next);
    get("record-path").textContent = display(record?.directory ?? record?.path); get("record-error").hidden = !record?.error;
    get("record-error").textContent = record?.error ? errorMessage(state.language, record.error) : "";
    const guide = record?.guide, step = guide?.current ?? guide?.step;
    const phase = guide?.phase === "review" ? "confirm" : guide?.phase;
    const prompt = step && typeof step === "object" ? (step[phase]?.[state.language === "en" ? "en" : "zh"] ?? step.id) : step;
    get("guide").hidden = !active || !current.current || !prompt;
    get("guide-step").textContent = prompt ?? ""; get("guide-hint").textContent = t(guide?.phase === "settle" ? "settle" : "hold");
    get("guide-progress").value = guide?.progress ?? 0;
    for (const [action, enabled] of [["ready", guide?.phase === "prepare" && step?.kind !== "remount"], ["finish", guide?.ready_to_finish], ["confirm", guide?.phase === "review"], ["skip", Boolean(step)]]) {
      get(`guide-${action}`).disabled = !active || !current.current || !enabled || Boolean(state.busy);
      get(`guide-${action}`).hidden = !enabled;
    }
    const available = Boolean(speech && Utterance); get("voice-enabled").disabled = !available;
    get("voice-info").textContent = t(!available ? "voiceUnavailable" : get("voice-enabled").checked ? "voiceReady" : "voiceOff");
    const voiceKey = active && current.current && available && get("voice-enabled").checked && prompt ? `${record?.session_id}/${guide?.index}/${guide?.phase}/${prompt}/${state.language}` : null;
    if (voiceKey !== state.spoken) {
      speech?.cancel(); state.spoken = voiceKey;
      if (voiceKey) { const utterance = new Utterance(`${prompt}. ${get("guide-hint").textContent}`); utterance.lang = state.language === "en" ? "en-US" : "zh-CN"; speech.speak(utterance); }
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
    return {following: Boolean(active), step: active || null, id: active?.id ?? state.referenceSelected};
  }
  function renderReference(current) {
    const poses = state.reference?.poses ?? [], guide = referenceStep(current), pose = poses.find(item => item.id === guide.id), hasImage = Boolean(pose?.front);
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
    get("reference-instruction").textContent = external && guide.id === guide.step.visual.demo_prompt_ids[0] ? `${guide.step.text} ${guide.step.visual.note}` : guide.following ? "" : pose?.instruction?.[language] ?? "";
    if (state.referencePose !== guide.id) { state.referencePose = guide.id; state.referenceAnimated = false; }
    const animated = external?.running ? !["hold", "done", "paused", "interrupted"].includes(external.phase) : state.referenceAnimated;
    const anchorId = external && guide.id === guide.step.visual.demo_prompt_ids[0] ? guide.step.visual.demo_prompt_ids[1] : null;
    const anchor = poses.find(item => item.id === anchorId)?.front ?? state.reference?.neutral;
    const key = `${guide.id}/${animated}/${pose?.front}/${pose?.profile}/${anchor}/${external?.frame ?? ""}`;
    if (key !== state.referenceKey) {
      state.referenceKey = key; state.referenceImageError = false;
      for (const id of ["reference-image", "reference-neutral-image", "reference-profile-image"]) get(id).removeAttribute("src");
      if (hasImage) {
        const asset = file => `/static/guide-reference/${encodeURIComponent(file)}`;
        get("reference-image").src = asset(animated && pose.animation ? pose.animation : pose.front);
        get("reference-neutral-image").src = asset(anchor);
        if (pose.profile) get("reference-profile-image").src = asset(pose.profile);
      }
      if (external && state.page === "recording") get("reference-image").scrollIntoView?.({block:"center"});
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
    get("reference-message").textContent = !hasImage && pose?.unavailable_reason?.[language] ? pose.unavailable_reason[language] :
      t(state.referenceLoading ? "referenceLoading" : !state.reference ? "referenceMissing" : !hasImage ? "referencePoseMissing" : state.referenceImageError ? "referenceImageError" : "referenceHelp");
    const authored = pose?.provenance?.some(kind => kind === "original_tongue_mesh_with_authored_skin_binding_and_post_skinning_controls" || kind === "original_mouth_shift_deltas_with_authored_independent_lip_masks");
    get("reference-origin").textContent = hasImage ? t(authored ? "referenceAuthored" : "referenceOriginal") : "";
    get("reference-source").textContent = state.reference?.model ? `${state.reference.model.name} · ${state.reference.model.copyright}` : "";
  }
  function selectReference(id) {
    if (referenceStep(view()).following || !state.reference?.poses?.some(pose => pose.id === id)) return;
    state.referenceSelected = id; state.referenceAnimated = false; render();
  }
  async function loadReference() {
    try { const manifest = await requestJson(fetcher, "/static/guide-reference/manifest.json"); if (!state.destroyed) state.reference = manifest; }
    catch { if (!state.destroyed) state.reference = null; }
    finally { state.referenceLoading = false; if (!state.destroyed) render(); }
  }
  async function reportVisual() {
    if (!options.viewer || state.visualReporting || state.destroyed || !view().current) return;
    const external = state.status?.external_guide;
    if (!external) return;
    state.visualReporting = true;
    try {
      const current = referenceStep(view()), key = state.referenceKey;
      const images = ["reference-image", "reference-neutral-image", ...(get("reference-profile").hidden ? [] : ["reference-profile-image"])].map(get);
      const rect = get("reference-image").getBoundingClientRect?.();
      const height = doc.documentElement.clientHeight ?? Infinity;
      const inView = !rect || rect.height > 0 && (Math.min(rect.bottom,height)-Math.max(rect.top,0))/rect.height >= .8;
      let ready = state.selectedPreset===external.case && state.page === "recording" && current.id === current.step?.visual?.demo_prompt_ids?.[0] &&
        inView && !state.referenceImageError && !get("reference-views").hidden && images.every(image => image.complete && image.naturalWidth > 0);
      if (ready && state.referencePaintedKey !== key) {
        const paint = options.paint ?? globalThis.requestAnimationFrame;
        await new Promise(resolve => paint(() => paint(resolve)));
        if (state.referenceKey === key) state.referencePaintedKey = key;
      }
      ready = ready && state.referencePaintedKey === state.referenceKey;
      if (state.destroyed) return;
      await requestJson(fetcher, "/api/display", {viewer: options.viewer, frame: external.frame,
        index: external.index, visible: !doc.hidden && state.page === "recording", media_ready: ready});
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
    for (const id of [...TEXT_FIELDS, ...NUMBER_FIELDS, "output_enabled"]) get(id).disabled = Boolean(state.busy) || !state.config;
    get("config-state").textContent = state.config ? t(state.dirty ? "unsaved" : "saved") : "";
    renderConnection(current); renderPages();
    const error = state.error || state.pollError || state.status?.last_error;
    get("error").hidden = !error && !state.errorKey; get("error").textContent = state.errorKey ? t(state.errorKey) : error ? errorMessage(state.language, error) : "";
    get("technical-error").textContent = [error, state.discoveryError, state.status?.recording?.error, state.status?.training?.error].filter(Boolean).join("\n\n") || "—";
    get("migration").hidden = !state.status?.legacy_configuration_available;
    get("placeholder").textContent = t(!get("preview-enabled").checked ? "previewOff" : state.previewError ? "previewError" : "noVideo");
    makeRows();
    for (const {output, bar, name} of rows.values()) {
      const value = channelValue(state.status, name, current.valid && !state.busy); output.textContent = value == null ? "—" : value.toFixed(3); bar.value = value ?? 0;
      bar.hidden = value == null; output.setAttribute("aria-label", `${name}: ${value == null ? "—" : value.toFixed(3)}`);
    }
    get("support-info").textContent = `${t("supported")}: ${state.status?.model?.trained_channels?.length ?? 0} / 50`;
    const owner = `${state.status?.source?.session_id}/${state.status?.output_session_id}`;
    if (owner !== state.previewOwner) { clearPreview(); state.previewOwner = owner; }
    if (!current.video || state.busy || !get("preview-enabled").checked) clearPreview();
    const identity = value => typeof value === "number" && !Number.isSafeInteger(value) ? t("unsafeInteger") : display(value);
    const source = state.status?.source ?? {}, result = state.status?.prediction_source ?? {}, m = current.current ? state.status?.metrics ?? {} : {};
    get("source-info").textContent = `${identity(source.session_id)} / ${identity(source.source_seq)}`;
    get("result-info").textContent = `${identity(result.session_id)} / ${identity(result.source_seq)}`;
    get("raw-identity").textContent = identity(source.raw_identity); get("time-mouth").textContent = identity(source.mouth_sensor_ns);
    get("engine-info").textContent = `${display(state.status?.engine)} / ${display(state.status?.device)}`;
    get("provider-info").textContent = state.status?.model?.providers?.join(" → ") || "—";
    get("fallback-info").textContent = state.status?.model?.fallbacks?.map(x => `${x.engine}/${x.device}: ${x.error}`).join("\n") || t("noFallback");
    get("source-age").textContent = `${fixed(m.source_age_ms)} ms`; get("receive-age").textContent = `${fixed(m.receive_age_ms)} ms`;
    get("rates").textContent = `${fixed(m.received_hz)} / ${fixed(m.inference_hz)} Hz`; get("drops").textContent = `${display(m.source_gaps)} / ${display(m.pending_dropped)}`;
    get("queue").textContent = `${display(m.queue_depth)} / ${fixed(m.queue_ms)} ms`; get("inference-time").textContent = percentile(m.latency_ms?.predict_ms);
    get("latency").textContent = percentile(m.latency_ms?.source_copy_to_result_ms);
    const stock = state.status?.stock;
    const association = stock?.associated > 0 ? "associationExact" : stock?.records > 0 ? "associationUnverified" : "associationWaiting";
    get("stock-info").textContent = `${t(association)} · ${JSON.stringify(stock ?? null)}`;
    get("snapshot-state").textContent = t(current.current ? "snapshot" : "expiredSnapshot");
    const replay = state.status?.replay; get("replay-info").textContent = replay?.path ? `${replay.path} · ${display(replay.position)} / ${display(replay.total)} ${t("frames")}${replay.finished ? ` · ${t("replayDone")}` : ""}` : t("replayReady");
    renderCapturePlan(current); renderRecord(current); renderReference(current); renderTraining(current); renderReview(current);
    if (state.status?.read_only) {
      get("status").textContent = t("visualReadonly");
      get("page-intro").textContent = t("visualReadonly");
      get("record-next").textContent = t("visualReadonly");
      for (const id of [...TEXT_FIELDS, ...NUMBER_FIELDS, "output_enabled", "start", "stop", "save",
        "capture", "capture-stop", "replay", "discover", "model-browse", "headset-select",
        "record-start", "record-stop", "record-cancel", "train-start", "train-cancel", "voice-enabled"]) get(id).disabled = true;
    }
  }
  function applyConfig(config) {
    state.config = config;
    if (!state.dirty) { for (const key of [...TEXT_FIELDS, ...NUMBER_FIELDS]) get(key).value = config[key] == null ? "" : String(config[key]); get("output_enabled").checked = config.output_enabled === true; }
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
      state.status = status; applyConfig(status.config ?? await requestJson(fetcher, "/api/config")); state.online = true; state.at = now(); state.pollError = "";
    } catch (error) { if (generation === state.generation && !state.destroyed) { state.online = false; state.pollError = error.message; } }
    finally { state.polling = false; if (!state.destroyed) render(); }
  }
  function readForm() {
    const config = Object.fromEntries(TEXT_FIELDS.map(key => [key, get(key).value.trim()]));
    for (const key of NUMBER_FIELDS) config[key] = Number(get(key).value);
    return {...config, output_enabled: get("output_enabled").checked, output_host: "127.0.0.1", model_kind: "face50"};
  }
  async function control(key, action) {
    if (state.busy || state.destroyed || state.status?.read_only) return; state.busy = key; state.generation++; state.error = ""; state.errorKey = ""; render();
    try { await action(); state.status = await requestJson(fetcher, "/api/status"); applyConfig(state.status.config ?? state.config); state.online = true; state.at = now(); state.pollError = ""; }
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
    if (get("start").disabled || !validate(mode === "capture" ? "live" : mode, mode !== "capture")) return;
    await control("starting", async () => { await persistConfig(); await requestJson(fetcher, "/api/start", {mode}, 60000); });
  }
  async function stop() { if (!get("stop").disabled) await control("stopping", () => requestJson(fetcher, "/api/stop", {}, 20000)); }
  async function record(action) {
    const button = {begin: "record-start", stop: "record-stop", cancel: "record-cancel", ready: "guide-ready", finish: "guide-finish", confirm: "guide-confirm", skip: "guide-skip"}[action]; if (!button || get(button).disabled) return;
    if (action === "begin" && !validate("live", false)) return;
    await control("working", async () => {
      await requestJson(fetcher, "/api/recording", {action, ...(action === "begin" ? {person_id: get("person_id").value.trim(), confirmed: get("record-consent").checked, guide: get("guide-enabled").checked, stage: get("label-stage").value,
        ...(get("guide-enabled").checked ? {preset_id:state.selectedPreset} : {})} : {})}, 20000);
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
  function previewLoop() { void refreshPreview(); if (!state.destroyed) state.previewTimer = timers.setTimeout(previewLoop, 1000/Math.min(10, Math.max(1, state.config?.preview_fps ?? 10))); }
  function setLanguage(language) { if (!Object.hasOwn(messages, language)) return; state.language = language; try { storage?.setItem(LANGUAGE_KEY, language); } catch {} render(); }
  get("settings").addEventListener("submit", event => { event.preventDefault(); return start(); });
  for (const event of ["input", "change"]) {
    const changed = () => { state.dirty = true; state.errorKey = ""; render(); };
    get("settings").addEventListener(event, e => { if (!e.target || [...TEXT_FIELDS, ...NUMBER_FIELDS, "output_enabled"].includes(e.target.id)) changed(); });
    get("page-preferences").addEventListener(event, e => { if (!e.target || [...TEXT_FIELDS, ...NUMBER_FIELDS, "output_enabled"].includes(e.target.id)) changed(); });
  }
  get("discover").addEventListener("click", discover);
  get("model-browse").addEventListener("click", chooseModel);
  get("headset-select").addEventListener("change", () => {
    const device = state.discovered.find(item => `${item.host}:${item.port}` === get("headset-select").value);
    if (device) { get("headset_host").value = device.host; get("headset_port").value = String(device.port); state.dirty = true; render(); }
    else if (!get("headset-select").value) { get("headset_host").value = ""; state.dirty = true; render(); }
  });
  for (const page of PAGES) get(`nav-${PAGE_NAV[page]}`).addEventListener("click", event => { event.preventDefault(); navigate(page); });
  for (const id of ["go-capture", "train-go-record", "train-go-review"]) get(id).addEventListener("click", event => {
    event.preventDefault(); navigate("recording"); if (id === "train-go-review") get("review").open = true;
  });
  navigation?.addEventListener("hashchange", hashChanged);
  get("language").addEventListener("change", () => setLanguage(get("language").value));
  for (const [id, fn] of [["save", save], ["stop", stop], ["capture-stop", stop], ["capture", () => start("capture")], ["replay", () => start("replay")], ["record-start", () => record("begin")], ["record-stop", () => record("stop")], ["record-cancel", () => record("cancel")], ["train-start", () => train()], ["train-cancel", () => train("cancel")], ...["ready", "finish", "confirm", "skip"].map(action => [`guide-${action}`, () => record(action)])]) get(id).addEventListener("click", fn);
  for (const id of ["preview-enabled", "record-consent", "voice-enabled", "person_id"]) get(id).addEventListener("change", render);
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
    const poses = state.reference?.poses ?? [], index = poses.findIndex(pose => pose.id === state.referenceSelected);
    if (!get(id).disabled) selectReference(poses[index+delta].id);
  });
  get("reference-play").addEventListener("click", () => { if (!get("reference-play").disabled && !get("reference-play").hidden) { state.referenceAnimated = !state.referenceAnimated; render(); } });
  for (const id of ["reference-image", "reference-neutral-image", "reference-profile-image"]) get(id).addEventListener("error", () => { state.referenceImageError = true; render(); });
  async function init() { render(); await Promise.all([pollStatus(), loadReference(), loadPresets()]); if (!state.destroyed) { state.interval = timers.setInterval(() => { render(); void pollStatus(); }, 500); if(options.viewer) state.visualTimer=timers.setInterval(() => {void reportVisual().catch(error => {state.error=error.message;});},200); previewLoop(); await discover(); } }
  function destroy() { state.destroyed = true; state.generation++; navigation?.removeEventListener("hashchange", hashChanged); speech?.cancel(); timers.clearInterval(state.interval); timers.clearInterval(state.visualTimer); timers.clearTimeout(state.previewTimer); clearPreview(); }
  return {init, destroy, start, stop, save, record, train, loadReviews, applyReview, readForm, pollStatus, refreshPreview, reportVisual, setLanguage, selectReference, selectPreset, discover, chooseModel, navigate, state, rows, trainInputs};
}
if (typeof document !== "undefined") {
  let storage; try { storage = window.localStorage; } catch {}
  const app = createApp(document, {storage, navigation: window, viewer: new URLSearchParams(location.search).get("viewer"),
    languages: navigator.languages ?? [navigator.language]}); void app.init(); window.addEventListener("pagehide", () => app.destroy(), {once: true});
}
