# Third-party notices

## ShadowHook 2.0.1

`hook/third_party/shadowhook-2.0.1/` is vendored source from
[bytedance/android-inline-hook](https://github.com/bytedance/android-inline-hook) release
v2.0.1, licensed under the MIT License. The vendored copy carries integration patches
documented in `hook/third_party/shadowhook-2.0.1/VENDOR.txt`: the linker monitor is
disabled for the static build via `PICOET_SHADOWHOOK_NO_LINKER_MONITOR`, and ELF-gap
allocation is disabled via `PICOET_SHADOWHOOK_NO_ELF_GAP` so ShadowHook cannot
overwrite PicoET's manually reserved plugin tail.

```
MIT License

Copyright (c) 2021-2026 ByteDance Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## PICO system software

This project does not contain or redistribute any PICO binary. It references the
device's own `libpxreyetrackingservice.so` and `libpxreyetracking.phoenix.so` by numeric
offset at runtime, only on a build whose SHA-256 matches a supported firmware version.
PICO, PICO 4 Pro and related names are trademarks of their respective owners and are used
here only to describe interoperability.
