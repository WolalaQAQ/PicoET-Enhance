# Third-party notices

## ShadowHook 2.0.1

`hook/third_party/shadowhook-2.0.1/` is vendored source from
[bytedance/android-inline-hook](https://github.com/bytedance/android-inline-hook) release
v2.0.1, licensed under the MIT License. The vendored copy carries integration patches
documented in `hook/third_party/shadowhook-2.0.1/VENDOR.txt`: the linker monitor is
disabled for the static build via `PICOET_SHADOWHOOK_NO_LINKER_MONITOR`, and ELF-gap
allocation is disabled via `PICOET_SHADOWHOOK_NO_ELF_GAP`, so hook islands use owned
anonymous mappings. The Android syscall extensions in `cpp/third_party/lss/` are
also covered by ByteDance's MIT notice.

The integration also patches `cpp/common/bytesig.c`: signal chaining checks
`SIG_DFL` and `SIG_IGN` before `SA_SIGINFO`. For the default disposition, it restores
the previous action and re-delivers the signal to the same thread, leaving the
interrupted signal mask unchanged. Protected operations and custom handlers retain
their existing behavior. ByteSig's existing MIT notice is retained.

```
MIT License

Copyright (c) 2021-2026 ByteDance Inc.
Copyright (c) 2021-2025 ByteDance Inc.

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

## xDL

ShadowHook includes the xDL sources in `cpp/third_party/xdl/` under the MIT License.
The first copyright line below is from its LICENSE file; the second is carried by
the vendored source headers.

```
MIT License

Copyright (c) 2020-2024 HexHacking Team
Copyright (c) 2020-2025 HexHacking Team

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

## linux-syscall-support

`cpp/third_party/lss/linux_syscall_support.h` carries the following BSD license:

```
Copyright (c) 2005-2011, Google Inc.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   Neither the name of Google Inc. nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## BSD queue.h

`cpp/third_party/bsd/queue.h` carries the following license:

```
Copyright (c) 1991, 1993
     The Regents of the University of California.  All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:
1. Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.
3. Neither the name of the University nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE REGENTS AND CONTRIBUTORS ``AS IS'' AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED.  IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
SUCH DAMAGE.
```

## BSD tree.h

`cpp/third_party/bsd/tree.h` carries the following license:

```
Copyright 2002 Niels Provos <provos@citi.umich.edu>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:
1. Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Android NDK C++ runtime

The payload statically links the Android NDK C++ runtime. LLVM's runtime is
licensed under Apache-2.0 with LLVM exceptions; the object-code exception covers
portions embedded by compilation. See the [LLVM license](https://llvm.org/LICENSE.txt)
and the fixed NDK's `NOTICE.toolchain` for its license and exception text. No NDK
tool executables are distributed in the module.

## LAN camera JPEG daemon

The optional JPEG daemon links the libjpeg API from
[libjpeg-turbo 3.1.2](https://github.com/libjpeg-turbo/libjpeg-turbo/tree/3.1.2).
This software is based in part on the work of the Independent JPEG Group.
The IJG license covers the libjpeg API and its SIMD implementation (whose source
also carries zlib notices); the build system and TurboJPEG API carry a
three-clause BSD license. This integration uses the libjpeg API, not TurboJPEG.
`camera-source/build.py` downloads a pinned source archive, builds it separately,
and retains `LICENSE.md` and `README.ijg` with the build outputs. The module
includes both in `licenses/libjpeg-turbo/`. Keep those notices and this
attribution when distributing a linked daemon. ShadowHook and the Android C++
runtime use the existing notices above.

## PC Studio (Rust, Tauri v2, ort, ONNX Runtime, WebView2, Inno Setup 6)

The Windows PC Studio desktop app (`pc-studio/src-tauri/`) is compiled from Rust
with Tauri v2 into a standalone executable (`PicoET-PC-Studio.exe`). No
third-party binaries or prebuilt runtime packages are committed to this
repository; exact crate versions and checksums are pinned in
`pc-studio/src-tauri/Cargo.lock` and can be inspected with
`cargo metadata --locked --filter-platform x86_64-pc-windows-msvc --manifest-path pc-studio/src-tauri/Cargo.toml`.

### MPL-2.0 Rust crates (source availability notice)

`PicoET-PC-Studio.exe` statically links the following unmodified third-party
Rust crates licensed under the **Mozilla Public License, v. 2.0 (MPL-2.0)**
(transitive dependencies of `tauri`):

- `option-ext` (`0.2.0`) — <https://github.com/soc/option-ext>
- `cssparser` (`0.37.0`) — <https://github.com/servo/rust-cssparser>
- `cssparser-macros` (`0.7.1`) — <https://github.com/servo/rust-cssparser>
- `dtoa-short` (`0.3.5`) — <https://github.com/upsuper/dtoa-short>
- `selectors` (`0.38.0`) — <https://github.com/servo/stylo>

These components are subject to the terms of the Mozilla Public License, v. 2.0
(<https://www.mozilla.org/en-US/MPL/2.0/>) and are combined with this
MIT-licensed Larger Work under MPL-2.0 Section 3.3. Their unmodified Source Code
Form is publicly available from [crates.io](https://crates.io/) and the upstream
repositories listed above at the exact versions pinned in
`pc-studio/src-tauri/Cargo.lock`.

### Permissive Rust crates

The remaining statically linked Rust dependencies in
`pc-studio/src-tauri/Cargo.lock` are licensed under permissive open-source
licenses:

- **Apache-2.0 / MIT** (single- or dual-licensed, including `tauri`,
  `tauri-plugin-single-instance`, `tao` [Apache-2.0], `wry`, `webview2-com`
  [MIT], `ort`, `ort-sys`, `rfd` [MIT], `zip` [MIT], `crc32fast`, `uuid`,
  `chrono`, `reqwest`, `schannel` [MIT], `image`, `serde`, `serde_json`,
  `socket2`, `windows-sys`, `dpi`, `sync_wrapper` [Apache-2.0], `zopfli`
  [Apache-2.0], and their transitive dependencies). The Apache License 2.0 text
  is available at <https://www.apache.org/licenses/LICENSE-2.0>.
- **ISC**: `libloading` (used by `ort` for dynamic loading of `onnxruntime.dll`).
- **BSD-3-Clause** (single- or dual-licensed with MIT / Apache-2.0): `brotli`,
  `brotli-decompressor`, `alloc-stdlib`, `alloc-no-stdlib`, `moxcms`, `pxfm`,
  `if-addrs`.
- **Zlib** (single- or dual-licensed with MIT / Apache-2.0): `zune-jpeg`,
  `zune-core`, `zlib-rs`, `foldhash`, `miniz_oxide`, `bytemuck`, `tinyvec`,
  `raw-window-handle`, `static_vcruntime`.
- **Unicode-3.0**: ICU4X / Unicode data crates (`icu_collections`,
  `icu_locale_core`, `icu_normalizer`, `icu_normalizer_data`, `icu_properties`,
  `icu_properties_data`, `icu_provider`, `zerovec`, `zerotrie`, `yoke`,
  `zerofrom`, `litemap`, `tinystr`, `writeable`, `potential_utf`) and
  `unicode-ident` (`(MIT OR Apache-2.0) AND Unicode-3.0`).
- **Unlicense / 0BSD / CC0-1.0 / BSL-1.0** (dual-licensed with MIT or
  Apache-2.0): `memchr`, `aho-corasick`, `byteorder`, `byteorder-lite`, `jiff`,
  `jiff-core`, `jiff-tzdb`, `jiff-tzdb-platform`, `winapi-util`, `same-file`,
  `walkdir`, `adler2`, `dunce`, `ryu`.

### ONNX Runtime (`onnxruntime.dll`)

`PicoET-PC-Studio.exe` links the MIT/Apache-2.0 `ort` crate with dynamic loading
(`load-dynamic`) and loads [ONNX Runtime](https://github.com/microsoft/onnxruntime)
(MIT License, Copyright (c) Microsoft Corporation) dynamically at runtime when
performing whole-mouth `.onnx` inference. No ONNX Runtime binaries are committed
to this Git repository; release builds (`pc-studio/package_release.py`) fetch,
SHA-256 verify, and bundle the official `Microsoft.ML.OnnxRuntime.DirectML`
(`1.24.4`, `onnxruntime.dll` and `onnxruntime_providers_shared.dll`) along with
its upstream `ONNXRUNTIME-LICENSE.txt` and `ONNXRUNTIME-THIRD-PARTY-NOTICES.txt`
beside `PicoET-PC-Studio.exe` (relying on Windows' system `DirectML.dll`).
Alternative side-by-side DLLs supplied via `ORT_DYLIB_PATH`, placed next to
`PicoET-PC-Studio.exe`, or passed to `package_release.py` remain subject to
their respective upstream licenses (MIT for ONNX Runtime; Apache-2.0 for
OpenVINO; Microsoft DirectML and NVIDIA CUDA terms for their respective runtime
components).

### Microsoft Edge WebView2 Runtime

`PicoET-PC-Studio.exe` statically links the MIT-licensed `webview2-com` bindings
and hosts its UI via the system-installed **Microsoft Edge WebView2 Runtime**
(Evergreen). Microsoft Edge WebView2 Runtime is proprietary software owned by
Microsoft Corporation and is not redistributed by this project.

### Inno Setup 6

The Windows installer (`PicoET-PC-Studio-Setup-v<version>.exe`) is packaged
with **Inno Setup 6** (Copyright (C) Jordan Russell & Martijn Laan), licensed
under the [Inno Setup License](https://jrsoftware.org/files/is/license.txt)
(modified BSD-style license).

## Unity-Movement demonstration assets

The expression guidance images in `pc-studio/web/guide-reference/` are rendered
close-ups of the Meta-Man character from
[oculus-samples/Unity-Movement](https://github.com/oculus-samples/Unity-Movement/tree/v71.0.0/Samples~/BodyTrackingSamples/HighFidelity)
(`v71.0.0`, `Samples~/BodyTrackingSamples/HighFidelity`), Copyright (c) Meta
Platforms, Inc. and affiliates, licensed under the
[Oculus SDK License Agreement](https://developers.meta.com/horizon/licenses/oculussdk/).
See `pc-studio/web/guide-reference/NOTICE.txt` and `LICENSE.txt` for details.
Only rendered images and notices are included—no FBX models, Blender scenes, SDK
code, or training labels are distributed.

## PC tongue upstream work and optional Python training dependencies

The legacy `pc-studio/` segmentation architecture, preprocessing and geometry adapt
Rulez777's MIT-licensed PicoET-Enhance PR #4, pinned to
`04fe68ac88e8c1fcaf504eba8223ebf33c629d93` for `shadow_model.py`, with calibration,
personal mask-head training/acceptance and size hysteresis based on tongue.2
(`f5f7894c6b30a86fd4699d721f966b6a01861443`). The upstream copyright is
`Copyright (c) 2026 PicoET-Enhance contributors`; the full MIT notice is retained
in this repository's [LICENSE](LICENSE). The PC adaptation adds ONNX Runtime
providers, fresh-source transport and transactional personal-profile activation.
The upstream photographs and demo media are not distributed here. Standard
weights are not included in this source tree.

Optional offline Python training tools (`pc-studio/face_train.py`,
`pc-studio/mouth50_trainer/`, `pc-studio/requirements-training.txt`) use
separately installed packages: PyTorch/torchvision (BSD-style licenses), ONNX
(Apache-2.0), ONNX Runtime (MIT), NumPy (BSD), Pillow (HPND), and optional
OpenVINO (Apache-2.0). CUDA runtime components retain NVIDIA's terms. Their
installed wheels carry the applicable notices; none of those packages or runtime
DLLs is committed to this repository.

## MouthLink interoperability

The whole-mouth regression tools and Rust preprocessing adapter follow the
ONNX/JSON contract and Pillow preprocessing of
[UndefinedSignal/MouthLink](https://github.com/UndefinedSignal/MouthLink),
reviewed at `39e091f4191d2ea1ad8536120924cdf61949dae2` (`training/export_onnx.py`,
`tools/mouthlink_live.py`). Negative-pose sampling, session-wise validation and
incremental fine-tuning, continuous regression and masked-label supervision also
draw on that project's training workflow and
[issue #5](https://github.com/WolalaQAQ/PicoET-Enhance/issues/5). This integration
uses the existing JPEG transport and PC inference path; upstream photos, personal
weights, raw UDP source and native ORT bindings are not distributed here.

```
MIT License

Copyright (c) 2026 UndefinedSignal

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

## Optional DINOv2 training backbone

The teacher adapter can load an explicitly supplied local checkout and checkpoint
of [Meta's DINOv2](https://github.com/facebookresearch/dinov2), using the ordinary
ViT-B/14-with-registers backbone as a candidate. DINOv2's ordinary code and weights
are available under [Apache-2.0](https://github.com/facebookresearch/dinov2/blob/main/LICENSE).
Backbone source and weights are not vendored or downloaded by this pipeline.
Keep the notices accompanying the actual backbone/checkpoint if distributing a
trained model. This attribution does not cover unrelated specialist weights in
the upstream repository or private training images.

## Optional DINOv3 training backbone

The teacher adapter can load an explicitly supplied local checkout and ViT-B/16,
ViT-L/16 or ViT-H+/16 checkpoint of
[Meta's DINOv3](https://github.com/facebookresearch/dinov3), covered by the
[DINOv3 License](https://github.com/facebookresearch/dinov3/blob/main/LICENSE.md).
Obtain access to the weights and accept their accompanying terms through Meta's
official distribution. Source and weights are not bundled. Retain the applicable
license and attribution when distributing trained derivatives.

## PICO system software

This project does not contain or redistribute any PICO binary. It references the
device's own `libpxreyetrackingservice.so` and `libpxreyetracking.phoenix.so` by numeric
offset at runtime, only on a build whose SHA-256 matches a supported firmware version.
PICO, PICO 4 Pro and related names are trademarks of their respective owners and are used
here only to describe interoperability.
