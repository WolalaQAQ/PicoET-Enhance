# Changelog

English | [简体中文](CHANGELOG_zh.md)

## [Unreleased]

### Features
- Separate **Expression monitor** and **Connection diagnostics** into a dedicated `#/diagnostics` workspace page with segmented tab switching (`Expression monitor` vs `Connection diagnostics`), moving telemetry out of Settings. Redesign the Expression monitor around a 10-channel core spotlight grid, anatomical region tabs across all 50 `pico-mouth50-v1` channels in symmetric paired cards, live source badges (`Native`, `Fallback`, `Model`, `Off`, `Untrained`), active/source filters, bilingual search, and a UI-side Freeze toggle; structure Connection diagnostics into a 2×2 card dashboard with an issue badge alert.
- Upgrade PC Studio whole-mouth channel routing to three presets (`curated` recommended default, `hybrid` full native, and `model_only`) backed by a unified 46-morph-target per-channel source editor (`channel_sources`, grouped by anatomy with native-quality tags, quick group/filter controls, and instant search). Changes hot-switch live without stopping camera tracking or restarting VRCFaceTracking, while preserving backward compatibility for saved `hybrid_channels` configs.
- Share one single headset `LABELS\n` (`PSL1`) TCP subscription whenever any routed morph target selects native stock or `receive_results` recording is active, merging fresh `final33` values (including valid zero, and mapping aggregate channels cleanly onto subdivided targets without overwrite leaks) with One-Euro-filtered model outputs before emitting `MFR1` packets.
- Filter valid face50 channels with source-time One-Euro smoothing; expose `smoothing_ms` in the bilingual Settings page and preserve raw predictions alongside filtered logs.
- Pair with the simplified `UnifiedPicoModule` pure-sink receiver (which writes `MFR1` directly and yields `BridgeSplit` face demand via `PXR_SUB` while keeping eye tracking uninterrupted).
- Put LAN broadcast and post-processing in two tabs. Keep the tuning sidebar and parameter panel, and group the two independent result switches.
- Use `--continue-recording` to offer the remaining actions from a stopped guided recording. The continuation saves separately and stays in the same dataset split group.
- Keep live camera preview visible during guidance. Choose automatic or manual step advance, adjust preparation time, move to any step, and resume the last unfinished guide from the recording page.
- Distinguish an image stream closing between frames from an incomplete header or payload.

### Design Rationale
- Keep channel-routing decisions and live tuning in PC Studio rather than VRCFaceTracking module JSON so users can toggle native/model per channel in real time while direct `Bridge → UnifiedPicoModule` users keep their existing path untouched, and reuse the single `PSL1` TCP slot allowed per headset daemon.
- Use actual image-sensor intervals and a speed-adaptive cutoff to reduce jitter without a fixed frame queue. Keep model inference stateless and filtering in the runtime.

### Notes & Caveats
- Routing presets or custom configurations that keep at least one channel on native stock require turning on Mouth under Result sharing in the headset companion app; missing or stale `PSL1` records automatically fall back to valid model channels. When zero targets use native stock (such as `model_only`), `LABELS\n` is skipped unless recording requests it.
- Zero smoothing is exact passthrough. Session/model changes, invalid results, mask changes and long source gaps reset state. Output permission still comes from independently validated manifest bits; smoothing does not repair sustained model errors.

## [2.6.0] - 2026-11-20

### Features
- Add LAN broadcast: the headset serves the mouth, left-eye and right-eye cameras as independent streams, announces them by broadcast/multicast, and answers standard HTTP MJPEG clients. The protocol, discovery and result formats are in `CAMERA-PROTOCOL.md`.
- Add a master switch, service status and per-camera addresses and client counts to the companion app. The home screen has two tabs, **LAN broadcast** and **Post-processing**, keeping the complete tuning panel, drafts, presets, language and save/apply behavior unchanged.
- Add two independent, default-off result switches: **Mouth** (PSL1) and **Both eyes** (PSE1, carrying each eye's stages, validity and inference timestamps). Turning one off disconnects only that result stream.
- Add PC Studio (`pc-studio/`), a Windows app for LAN camera forwarding and preview: headset discovery, camera preview switching across mouth, left eye and right eye with MJPEG URLs, a dual-eye relay to Cymple's native UDP input, an in-app GitHub Releases update checker, and windowless app-mode launch with single-instance reuse. Releases attach both a user-scope Windows installer (`PicoET-PC-Studio-Setup-v<version>.exe`) and a standalone portable archive (`picoet-pc-studio-v<version>.zip`).
- Share one native tracking lease for the union of camera and result subscribers, and release it when the last consumer leaves. Serve JPEG and standard MJPEG subscribers with one encode per source frame and independent, bounded latest-frame delivery.

### Design Rationale
- The companion app stays rootless and without network permission. Camera control and tuning use separate mailboxes handled by the same module worker, so navigation cannot overwrite an in-flight request.
- Sharing defaults off and remembers an explicit choice. Closing the app leaves the service available, but cameras run only while clients are subscribed. Turning sharing off drains every stream and reuses the existing ownership, nonce and join acknowledgements, without restarting tracking.
- PC Studio only forwards and previews. It has no model or training dependencies, so it installs in seconds and runs on any Windows PC on the same network.
- The camera sources live in `camera-source/` and the PC app in `pc-studio/`.

### Notes & Caveats
- `versionCode` is 28, above the 2.6.0 development builds used for testing. Update the APK and the module backend together.
- LAN images are unencrypted and unauthenticated; use them on trusted private networks only. Close other eye/face clients once before the first attachment so the adapter can initialize while tracking is idle. Retire any separately started development adapter before using the module switch.
- Processing settings and stock defaults are unchanged.

## [2.6.0-dev.2] - 2026-11-18

### Features
- Add a separate, default-off eye expression result switch and PSE1 subscription carrying both eyes' stages, validity and inference timestamps.
- Add a default-off headset switch for sharing mouth results and a per-recording PC option to receive them. Turning result sharing off disconnects PSL1 without interrupting images.
- Let managed Capture use an existing LAN camera service; closing the workbench's subscription leaves other consumers and the service running.
- Expose independent mouth, left-eye and right-eye LAN streams, a broadcast/multicast camera catalog and HTTP MJPEG endpoints, with a reusable discovery CLI and protocol reference.
- Acquire one native tracking lease on Android for the union of camera/label subscribers and release it after the last consumer leaves.
- Support concurrent JPEG and standard HTTP MJPEG subscribers with one encoding per source frame and independent, bounded latest-frame delivery.
- Make Capture self-operated: timed preparation and holds, automatic demonstration changes and recording completion, pause/resume and skip, plus an optional managed source lifecycle behind one recording action.
- Add a persistent current-step counter/progress bar, four ordered capture presets (12/11/13/71 steps) and an expandable action list to Capture. Guided recording uses the selected preset and locks it for the session. Every timed step is something the wearer can do; occlusion labelling stays in Review, and the refit suggestion appears after a recording is saved.
- Rewrite the wording of all four pages in both languages: buttons, status and error messages, help, preset names and descriptions, the ordered action lists, step prompts and the reference image titles and captions. Each message now says what is happening, what to do and what the result is.
- Add reusable exact-pair auditing and derived raw-supervision helpers, preserving signed archives, reviewed-label priority and whole-session grouping.
- Add optional source-weighted MSE/Huber and mild, non-mirrored IR augmentation for matched training comparisons.
- Support optional 224×224 student inputs, retaining 160×160 by default and restoring input size when reloading checkpoints.
- Lower the default teacher-head learning rate to `1e-4` after observing sigmoid saturation and zero gradients on sparse real-image supervision; student defaults remain `1e-3`.
- Support explicitly selected local DINOv3 ViT-B/16, ViT-L/16 and ViT-H+/16 teachers with full 400×400 inputs, head training, last-two-block fine-tuning and the existing pseudo-label/student-distillation flow.
- Split the PC app into Tracking, Capture, Train and Settings pages with direct links and browser-history navigation. Keep everyday actions visible and move channel tables, technical parameters and individual training operations into advanced entries.
- Discover mouth sources on the LAN through UDP45681 and verify their PTC1 identity over TCP; keep a manual address under Settings → Connection and performance.
- Select the local ONNX path through the PC's native file dialog, without uploading or copying the model.
- Add bilingual Meta-Man face close-ups to pose guidance: 71 illustrated stages covering all 50 named controls, 70 opt-in animations and 25 side views, with practice browsing and recording-step matching. Occlusion and refitting remain text-only operations.
- Add an opt-in 400×400 grayscale JPEG95 camera source with shared-slot IPC and a separate root transport daemon, targeting fresh 90 Hz inputs.
- Make end-to-end whole-mouth regression the development route and define `pico-mouth50-v1`: MouthLink's original 44 fields plus four upper/lower lip horizontal controls and two upper/lower Funnel controls.
- Add a stock raw/instant/final expression and quality side channel, exact source pairing, separately consented local recording, pose guidance and unknown-label masks.
- Add local teacher fine-tuning, soft labels, a directly supervised student baseline, student distillation/personal adaptation and ONNX export tools; user data is not uploaded.
- Send whole-mouth controls through 256-byte MFR1 with per-field validity on local UDP45682 for the matching UnifiedPicoModule to merge, retaining ORT backend selection and the bilingual app.

### Fixes
- Enable self-operated Capture in the normal PC entrypoint, save a discovered or selected headset when recording begins, and reuse an already-open capture preview without reconnecting. Keep headset selection and optional preview available on Capture.
- Send discovery from each local IPv4 interface, including explicit multicast interface selection, so a VPN default route does not hide LAN cameras.
- Allow ten seconds for the first camera frame during native cold start, then retain the one-second established-stream timeout.
- Scope vendor frame-counter resets to verified native capture epochs so a new subscription works after the last consumer disconnects. Keep raw identities and timestamps unchanged, with strict sensor-time and same-epoch regression checks.

### Design Rationale
- Give each camera its own bounded raw slots and encoder; perform Binder startup/shutdown on a separate worker so camera demand does not block the IPC heartbeat.
- Keep image subscription generations stable while any consumer remains; a slow or disconnected client does not hold the raw shared-slot lease or stop other consumers.
- Advance only with fresh continuous input and a current, visible, loaded-and-painted demonstration. Keep recording controls beside the existing reference images and preserve unknown training targets.
- Derive the guide length and order from the preset's action list; freeze the selected plan in the recording manifest before frames arrive, while reusing the existing demonstration assets.
- Keep optimization coverage separate from validation and output permission; export without an approval retains provenance but enables no channels.
- Record distinct DINOv2/DINOv3 checkpoint kinds and input sizes so continuation restores the matching teacher. Both use CLS and mean-patch features with the same 50-channel head and MobileNetV3-small student.
- Separate tasks rather than using a sidebar to scroll through a long form. Changing pages retains drafts and active sessions; camera preview uses the same image element across Tracking and Capture.
- Keep device/model selection as a draft; discovery does not subscribe to images, and choosing a model does not start inference or recording.
- Preserve the HighFidelity character, source mesh and existing morph coordinates. Distinguish 48 original-morph stages from 23 stages with added tongue binding/bending or independent lip-shift controls; demonstrations are visual cues, not measured targets.
- Keep capture on the headset, configuration/training/inference on the PC, and mouth ownership/SDK merging in VRCFT, without adding ORT or a teacher to the host.
- Preserve the synchronous camera protection copy, borrow the shared slot for encoding, and keep personal profiles separate from standard weights.
- Distribute the teacher for local supervision of a user's student, then run only the student during normal use; collecting user images on the developer's side is not a prerequisite.
- Preserve raw, gated instant and final filtered values separately; a gated TongueOut zero is not an automatic negative label. Split training/validation by person and headset-fit session.

### Notes & Caveats
- Rebuild the main hook, adapter, shim, controller and daemon together for PTJIPC5. PTJ1/PTC1/PSL1 wire layouts are unchanged; camera5 timestamps describe a shared parent frame, not PSE1 image associations.
- Preset selection and practice do not begin recording or approve labels. Guided recording stops automatically; free recording is stopped manually. Pausing the guide retains the raw stream.
- Model loading and the final runtime merge enforce `validated_channels` and `valid_mask`; legacy training-only manifests no longer enable output.
- DINOv3 requires its matching local source and authorized weights under the DINOv3 License. DINOv2 remains the CLI default; select v3 B, L or H+ with `--teacher-backbone dinov3`, `dinov3-large` or `dinov3-huge`.
- Automatic discovery requires this branch's updated headset daemon and a network that allows broadcasts. The native file chooser runs on the PC hosting the local app and needs Python's Tk support.
- Reference images retain their source and asset-license notices; the pack contains no source FBX or SDK implementation. Added guide controls are labelled separately from original Meta morphs.
- Pair with UnifiedPicoModule `feat/tongue-pc`. This branch's Magisk package includes optional LAN cameras controlled from the companion home screen; prepare the PC app and receiver separately from source.
- Retain segmentation/geometry and personal mask-training code only for history and regression tests, not as the default or a silent fallback for regression-model failures.
- No validated general-purpose mouth/teacher weights are bundled, and training materials are not downloaded automatically. Synthetic training and ONNX checks do not establish worn accuracy.
- On-device inference-input/camera identity association, sustained worn sources, personal accuracy and native/VD concurrency still need live acceptance; this work does not install or operate the device.

## [2.6.0-dev.1] - 2026-11-18

### Features
- Give the companion app a home for LAN cameras and expression tuning. Move the complete parameter panel into its own screen, preserving drafts, presets, language and save/apply behavior.
- Add a real LAN-camera master switch, actual service status, per-camera client counts and MJPEG addresses. Bundle the adapter, IPC shim, transport daemon and module controller.

### Fixed
- Match the companion home to the existing tuning panel: outlined ripple buttons, card borders, typography, spacing and switch sizing.

### Design Rationale
- Keep the app rootless and without network permission. Camera and tuning use separate mailboxes handled by the same module worker, so navigation cannot overwrite an in-flight request.
- Default sharing off and remember explicit choices. Closing the app leaves the service available, but cameras run only while clients subscribe. Turning sharing off drains every stream and checks the existing ownership, nonce and join acknowledgements.
- Reuse the enhancement control lock and existing transport, without restarting tracking to toggle sharing. Wait for idle tracking before the first attachment; retain a failed instance's diagnostics instead of repeatedly injecting.

### Notes & Caveats
- Unpublished development version, `versionCode` 24. Update the APK and module backend together. Processing settings remain unchanged.
- LAN images are unencrypted and unauthenticated. Retire any separately started development adapter before using the module switch. Module-managed startup, shutdown and the native home screen still require device acceptance.

## [2.5.2] - 2026-11-15

### Features
- Add ◀ / ▶ fine-adjustment buttons beside every numeric slider in the settings app. Each tap changes the draft by the field's fixed step, keeping the value, slider and chart in sync. Precise input remains available.

### Fixed
- Retune Balanced so small widening movements and eye, brow, mouth and tongue actions follow more readily. Keep more smoothing than Responsive, along with blink snapping, rest removal and the lip closure boost. Update both languages in the panel.

### Design Rationale
- Fine arrows use the stored value rather than slider positions, making controller clicks precise even on logarithmic ranges. Balanced trades a little steadiness for quicker small movements; release builds check all eye and mouth channels and verify preset files against native definitions.

### Notes & Caveats
- Stock defaults, Responsive and No processing are unchanged. Saved settings are not replaced automatically; select, save and apply Balanced again to adopt its new values.
- `versionCode` is 23. The settings app keeps the same signing identity and preserves its preferences on update.

## [2.5.1] - 2026-11-15

### Fixed
- After a module upgrade, the system kept showing the settings app's previous version (for example 2.4.2 after installing 2.5.0) and would have ignored any new app components. Android caches the parsed app and judges that cache by the app directory's modification time, which stays fixed under the Magisk mount. Installing the module now deletes the app's entry from `/data/system/package_cache`, so the next boot reads the new APK.

### Notes & Caveats
- `versionCode` is 22. Only the installer changed; the native files, settings app, presets and saved settings behave as in 2.5.0.
- Verified on PICO 4 Pro / PICO OS 5.13.7 with SELinux enforcing: after installing over 2.5.0 and one reboot, the system reported the settings app's current version.

## [2.5.0] - 2026-11-15

### Features
- PICO's stock firmware already postprocesses eye and mouth expressions (stage strengths, brow response and links, Blink endpoints, adaptive/composite/Kalman filters, mouth baseline adaptation, weak-shape compression, the tongue threshold and smoothing), with fixed parameters that could not be changed. 2.5.0 exposes 44 of these parameters as settings whose defaults are the stock values. Settings are stored in `/data/local/tmp/picoet-postprocessing.conf`; without that file, processing is identical to stock.
- Bundle a separate, rootless **PicoET-Enhance** settings app with the module. It appears in the headset application library after installation and reboot, and is removed together with the module after uninstalling and rebooting. The app sends fixed requests from its Android sandbox; the module-owned `picoet-panel` worker carries them out through `picoet-config` and `picoet.sh reload-processing`. Bridge and the PC module need no update and do not manage headset settings.
- Organize the app around 15 movements to check in a mirror, such as blinking, raising the brows, talking and a slight tongue. Each movement page draws the model value against what the avatar receives, lists common symptoms with the slider to move, and shows the settings that shape that movement; the chart recalculates as you drag. All settings lists the 44 values in processing order. Controls are named after their visible effect, accept precise input and can be reset one at a time.
- Offer Simple and Technical wording. Simple describes visible movement, direction and trade-off; Technical adds variables, formulas and the source-interval conversion. Switching changes only the text.
- Provide four presets: **Balanced** (recommended), **Responsive**, **Stock** and **No processing**. Stock uses PICO's original parameters. Balanced lets the brows settle in about 0.2 s instead of about 1 s, keeps fuller jaw movement while talking, shows a slight tongue and weakens the blink/wide-eye brow links, while held expressions stay about as steady as stock. Responsive follows the model within about 0.1 s with half of the smoothing and no brow links. No processing skips this stock postprocessing and outputs bounded model values.
- Separate saving from applying. Saving replaces the file atomically without restarting tracking. Applying restarts the eye service only when no tracking client is connected and enhancement is on. The bottom bar shows whether there are unsaved changes, saved settings waiting to be applied, or settings applied in this session.
- Add the root-shell commands `bin/picoet-config write` and `picoet.sh reload-processing`.

### Design Rationale
- The postprocessing algorithms remain PICO's; this release only makes their parameters configurable. Settings and processing stay on the headset. Source identities, per-eye state, quality/reference logic and final output bounds are unchanged, and default values follow the existing numerical path.
- Privilege stays inside the existing Magisk module rather than the GUI or the forwarding app. The mailbox accepts only fixed action names, checks the caller's UID and uses directory-relative access; closing the app does not interrupt a module transaction.
- People tune by what they see on the avatar, so the app starts from a movement and its symptoms instead of 44 implementation stages. Charts use the same formulas as the native code at a fixed example interval of 1/90 s; at runtime the module converts coefficients with the measured interval between source timestamps.
- Preset descriptions are checked against their stated results: host tests run every movement through the app's simulation of the native chain and assert the quoted brow times, jaw peak, rest leftovers, slight tongue and brow links. Balanced keeps blink snapping, rest removal and the MouthClose addition; without them the eyes do not fully open or close and the mouth stays slightly open.

### Notes & Caveats
- `versionCode` is 21. Existing mode, gate and rate files are kept. After uninstalling the module and rebooting, the settings app and the postprocessing configuration file are removed.
- The settings app is signed with the project's release key; future updates use the same key.
- Configurations load once when the payload starts, so changes take effect after Apply or the next service start.
- Verified on PICO 4 Pro / PICO OS 5.13.7 with SELinux enforcing: native numerical tests, boot integration of the app and worker, and rootless save/apply/restore covering all four presets and 44 fields. Preset values come from synthetic movements; worn response and noise have not been compared yet.

## [2.4.2] - 2026-10-24

### Fixed
- Pass the installed NDK 26.1.10909125 and CMake 3.22.1 paths explicitly as `-Ndk` and `-CmakeBin` to `build-hook.ps1`, fixing CI's selection of the runner's preinstalled NDK 27.3 / CMake 4.1.2.

### Design Rationale
- Installing a toolchain does not ensure the build selects it, so CI must explicitly select the same versions used for validation.

### Notes & Caveats
- `versionCode` is 20; runtime sources, default 90/90 targets, configuration, schema 2 / `EPI1` v2 / `X` v2 and companion requirements are unchanged.

## [2.4.1] - 2026-10-24

### Features
- Defer automatic injection until `sys.boot_completed=1` and Binder readiness from the same service instance. Early boot no longer performs injection or runtime-state cleanup.
- Bound automatic injection across consecutive unstable instances: at most three instances, replenished only after the same instance stays continuously `ready` for 60 seconds. Repeated control/watchdog initialization does not reset the count.

### Fixed
- Use `PTRACE_SEIZE` / `PTRACE_INTERRUPT` to stop and rescan all threads, verifying process/thread identity and mappings before remote execution. Read back restored GPR, FPSIMD and temporary instruction bytes before resuming; unsafe execution or restoration failures terminate the affected instance.
- Treat temporary signal stops and lack of a safe executor as injector exit `3`, not a permanent injection block. Release the control lock before the watchdog's 30-second cooldown, and return the unused automatic-injection reservation.
- Apply the existing single recovery budget to synchronous and asynchronous unsafe failures. Persist the reservation before acknowledging the failure, and avoid restarting a replacement instance that is already running.
- Serialize installation and runtime control with the same lock. Preserve saved `off` from legacy root-owned `0666` files during migration; use checked file descriptors and atomic replacement for configuration and runtime reports.
- Fix ByteSig's default-signal handling: recognize `SIG_DFL` and `SIG_IGN` before `SA_SIGINFO`, restore the default disposition and re-deliver to the same thread without changing the interrupted signal mask. Protected operations and custom handlers retain their existing behavior.
- Replace recursive uninstall cleanup with deletion of known leaf files and removal of empty directories, without traversing nested mounts. Unexpected directories may remain.

### Design Rationale
- Keep unsafe recovery separate from the unstable-instance limit: temporary preparation failures (injector exit `3`) consume neither budget, while early consecutive replacements without asynchronous failure reports remain subject to the unstable-instance limit. A new boot or explicit mode command resets the budgets.
- Keep engines initialized before hook installation running. Profile 0 and startup ET configuration are not applied retroactively; enhancement state warms up again without forcing an engine rebuild. `ready` describes installed hooks and configured targets, not reconstruction of an existing engine.
- Leave expression algorithms, inference models, default 90/90 targets and stock queue behavior unchanged. PICO system libraries remain unmodified.

### Notes & Caveats
- VersionCode is 19. Configuration paths, runtime schema 2, `EPI1` v2 / `X` v2 and companion properties are unchanged; PicoFacialBridge 0.5.0+ and UnifiedPicoModule 0.4.0+ remain the matching components. Install the scripts, injector and payload together, then reboot.
- The one-time frequency migration still applies only to installed versionCode below 18 (before 2.4.0). Reinstalling or upgrading from 2.4.0 or later preserves user-selected rates and saved `off`.
- Passed 188 offline checks (59 packaging/source, 123 shell and six existing logic-model checks), 35 injector control cases including targeted diagnostic fault checks, 20 payload file-I/O checks, four isolated uninstall checks, 19 ByteSig signal modes and 37 local callback/registry tests. The callback/registry tests do not run SNPE.
- On PICO 4 Pro / PICO OS 5.13.7 with SELinux permissive, normal boot, upgrade, off→dual and repeated ensure passed. A 45-second worn session confirmed live eye/mouth updates and action response, profile 0 initialization without retries, and normal STOP/END without a service restart. This was not a new 90 Hz throughput measurement; earlier throughput figures retain their documented scope. SELinux enforcing remains untested.

## [2.4.0] - 2026-10-18

### Features
- Eye and mouth expressions now run at 90/90 Hz by default, up from 90/24. Both paths process every eligible fresh camera input, with the two inferences for a frame running in parallel.
- Add rate files for lowering either rate: `/data/local/tmp/picoet-eye-hz` and `/data/local/tmp/picoet-mouth-hz` take a positive integer, are capped at 90, and fall back to 90 when missing or invalid. The mouth rate cannot exceed the eye rate, and frames outside the chosen rate are skipped. Setting only the mouth file to 24 gives 90/24; see the README.
- Rebuild mouth postprocessing from the raw 35-value mouth output into the 33 mouth slots. Smoothing, gated baseline adaptation and quality timing now advance by the mouth's own source time, as the eye path already did.
- `picoet.sh status` reports `effective_eye_hz` and `effective_mouth_hz`, the effective configured target rates, not measured output rates.

### Fixed
- Fix `picoet.sh dual` reporting success when the restarted eye-tracking service exited before enhancement was loaded, leaving `status` showing it unloaded and off. The command now reports the failure, and the background watchdog retries on its next pass. This also affected 2.3.0.

### Design Rationale
- Mouth smoothing involves a double k6 step whose second pass depends on the first pass's error, so the whole step is normalized to source time once rather than scaling each pass separately. When the time ratio is one, the stock floating-point order is kept: over a 2,700-frame control, all 52 float values, validity bits and source identities match the reference output exactly.
- The inference models, same-frame parallelism and stock queue-full behavior are unchanged, and rates are never lowered automatically under load. The stock 24 Hz reference period also stays as it was.

### Notes & Caveats
- The rate files are read once when the service starts. Apply a change by running `picoet.sh dual`, which restarts the service and disconnects tracking clients; the command refuses while clients are connected unless you add `--force`. Normal mode (`off`) stays fully stock.
- Fresh installs enable `dual/on` at 90/90, and an upgrade keeps a saved `off`. Installing 2.4.0 over an earlier version starts at 90/90 once; rates you set afterwards are kept when you reinstall or upgrade.
- Deploy the payload and the matching `picoet.sh` together, since the runtime state now carries both rates (schema 2).
- Mouth baseline adaptation and quality timing are time-normalized approximations, so output at different rates is close but not bit-identical.
- No PicoFacialBridge or UnifiedPicoModule changes: `EPI1` v2 and `X` v2 are unchanged, and PicoFacialBridge 0.5.0+ with UnifiedPicoModule 0.4.0+ remain the matching versions.
- Measured on an awake, tracking-ready PICO 4 Pro (not worn) by replaying 2,700 recorded camera inputs at 90/90 through a test harness that drives the module's expression-processing algorithm: eye and mouth computation completed at 89.80 Hz, with six inputs dropped by the stock full-queue rule and no inference failures. With VRCFT-Enhance and desktop VRChat, 2,633 sources (87.80 Hz) carried eye and mouth together through `X`, SDK IPC and OSC with values matching at each stage; the loopback packet capture dropped packets, so the downstream counts are verifiable lower bounds. The replay was not run through the installed Magisk package and does not cover live camera copying or Virtual Desktop decoding and rendering. See the README for details.

## [2.3.0] - 2026-10-17

### Features
- Run the complete independent eye-expression path on fresh camera inputs while keeping the stock mouth chain at about 24 Hz, with same-frame eye/mouth inference in parallel.
- Export `EPI1` v2 with separate left-eye, right-eye and mouth source identities and update bits for `X` v2 companion builds.

### Changed
- Keep the toolchain's default GNU Build ID on the hook payload and helpers, and drop the build-time check that rejected it. The Build ID is a normal part of the link output and not related to the module's own integrity checks, which are unchanged.

### Design Rationale
- Normalize eye smoothing to each eye's source-time interval. Quality and reference decisions use each eye's effective observation time, so missing frames pause the window and repeated or backward sources do not advance it.
- Apply SNPE profile 0 only to the first eligible builder during guarded, uninitialized FaceTrackor eye/mouth initialization. Native eye tracking, runtime selection, priority and retry settings remain stock.

### Notes & Caveats
- Requires PicoFacialBridge 0.5.0 or later and UnifiedPicoModule 0.4.0 or later for the `X` v2 path; the earlier 0.4.0 / 0.3.0 pair supports the `X` v1 path only.
- Keep P1, boot loading, mode switching, dark-image admission, reference protection, within-eye rules, filtering and quantization. A partially failed owner remains sealed until normal lifecycle reconstruction.
- Validated on a PICO 4 Pro with PicoFacialBridge 0.5.0, UnifiedPicoModule 0.4.0 and official VRCFT 5.4.5 in an 84-second worn Virtual Desktop session with Cymple mouth tracking and PICO eyes: about 89.7 `X` packets per second reached the PC, each with a new eye source. PICO eyes with face tracking also ran normally. This was a short session, not a long-duration stability test.
- The official VRCFT host passes this stream to OSC at roughly 61–78 Hz on the test PC. The optional [VRCFT-Enhance](https://github.com/WolalaQAQ/VRCFT-Enhance) raises it to about 87–88 Hz; see the README.

## [2.2.0] - 2026-10-15

### Features
- Decouple the facial expression model: EyeLook, Blink, Wide and the brow channels are computed and filtered per eye, each with its own quality window, reference image, numerical history and Kalman filter.
- Open the service-local gate (`PXR::EyeUtil::isBusinessDev` -> true) so the eye-tracking service writes real pupil diameter data to shared memory.
- Export the filtered right inner brow and the per-eye quality states to the facial prefix as the `EPI1` extension, which PicoFacialBridge forwards as `X` packets.

### Changed
- Remove the native gaze modifications: the per-eye single-eye inference dispatch, the plugin-tail leaf and the left/right eye-mode patch. Combined gaze now remains fully stock.
- Accept `on` as a synonym for `dual` in `picoet.sh`.

### Fixed
- Fix SELinux policy loading on enforcing builds (#3): use Magisk statement syntax, allow traversal of `/data/adb` and the module tree, replace the nonexistent `mem` class with `/proc` metadata permissions, and name the service type explicitly for `execmem` instead of using `self`.
- Include the project license and third-party notices in the Magisk ZIP, with the xDL, linux-syscall-support and BSD header notices used by ShadowHook.

### Design Rationale
- The stock model mixes the two eyes before output, so one eye's closure, quality or history can change the other side. The independent path keeps each eye's own raw inference result, quality window, reference and history while the stock mouth processing and owner lifecycle keep running.
- Reuse the stock per-eye rules instead of approximating them: blink endpoints, the within-eye inner/outer brow linkage and the `correct`->`predict` Kalman order, instantiated once per eye inside the service.

### Notes & Caveats
- Validated on PICO 4 Pro / PICO OS 5.13.7 with SELinux permissive: recorded inputs reproduce the independent output and match the stock baseline on all non-eye coefficients and the mouth validity. Recorded eye images replayed on the headset through native eye inference reached official VRCFT 5.4.5 via PicoFacialBridge 0.4.0 and UnifiedPicoModule 0.3.0 with `E` and `X` values matching their source outputs. The recording is about 23.7 Hz and was replayed in an accelerated loop, so this verifies inference throughput and transport, not an original 90 Hz camera sequence.
- Requires PicoFacialBridge 0.4.0 or later and UnifiedPicoModule 0.3.0 or later for the per-eye `X` path. Single-eye quality failure (classification 0), staggered reference rebuild, a live mouth-output comparison and long live-camera use are not yet covered.
- PICO 4 Enterprise with SELinux enforcing remains unverified; the policy update requires a module reinstall and reboot.
- The extension carries both inner-brow sides, but the host-side brow parameters used by current avatars average them. The original within-eye inner-to-outer brow write-back is intentionally kept.
- The build now fails if a payload or helper contains a GNU Build ID.

## [2.1.0] - 2026-10-11

### Fixed
- Make dual output independent per eye and remove the shared fused-filter residual. Failure clears only that eye's gaze-valid bit while the other keeps updating.
- Store per-frame results in thread-local state and preserve caller registers and floating-point state in Hook B. Combined gaze retains the original processing.
- Fix enhancement failing to re-enable when Magisk launches scripts with relative paths.

### Changed
- Use two control modes: enhanced (`dual/on`) and normal (`off/off`). Remove the `left`, `right` and separate `gate` commands.
- Make the Magisk Action button switch according to actual readiness and retry enabling after failed loading.
- At upgrade and boot, normalize a saved `off` to `off/off` and other previous settings to `dual/on`.
- Strip ELF debug information before packaging.

## [2.0.4] - 2026-10-08

### Changed
- Default fresh installs to `dual` + `gate on`, automatically enabling genuine per-eye gaze and pupil data after reboot without manual mode selection. Installation messages show the saved state.
- Preserve valid mode/gate settings on upgrade, including `off` and `gate off`. Runtime failures still advertise only successfully installed hooks, never a forced enhanced state.

### Validation
- Android NDK builds, hook shape checks, shell/PowerShell syntax checks, module packaging and public-content checks pass.
- The new installation defaults have not yet been validated on a headset.

## [2.0.3] - 2026-10-03

### Fixed
- Save and restore the executor's complete general-purpose and FPSIMD context during injection. Refuse remote calls if the context cannot be saved.
- Verify the remote-call return trap. Timeouts, unexpected signals and failed restoration terminate the affected service instead of resuming a half-finished loader call. Automatic recovery allows one fresh-process retry per boot or explicit mode/gate command; subsequent failures stop automatic injection.
- Disable ShadowHook's ELF-gap allocation fallback in this integration so it cannot overwrite the hand-written Hook B leaf in the plugin tail.
- Publish the hooks actually installed, using atomic runtime reports bound to the service PID and start time. Pending or failed plugin installation no longer advertises independent dual gaze.
- Reconcile watchdog state every second, including asynchronous plugin completion, service replacement and read-only mapping fragments. Serialize watchdog and manual control commands.
- Stop all plugin installation after service compatibility or hook initialization fails, and require plugin guards before applying left/right patches. Explicit `gate off` remains supported.

### Changed
- Extend `status` with installation phase, applied mode/gate and automatic-recovery stop reasons.
- Preserve the running instance's advertised state when staging an upgrade; clear runtime reports at boot and uninstall.

### Validation
- Android NDK builds, hook shape checks, shell/PowerShell syntax checks and module packaging pass.
- This release's register restoration, failure recovery and runtime-state permissions have not yet been revalidated on a headset. The supported firmware remains PICO OS 5.13.7 on PICO 4 Pro.

## [2.0.2] - 2026-10-02

### Added
- Initial public runtime-injection release with a service-local per-eye gate and off, left, right and dual gaze modes.
- Magisk lifecycle scripts, transient companion-app mode properties, firmware guards and source-only hook builds without bundled PICO libraries.
