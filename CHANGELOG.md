# Changelog

English | [简体中文](CHANGELOG_zh.md)

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
