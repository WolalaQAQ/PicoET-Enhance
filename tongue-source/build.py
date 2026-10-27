"""Build source-only Android artifacts or Windows offline harnesses; never calls adb."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
JPEG_URL = 'https://codeload.github.com/libjpeg-turbo/libjpeg-turbo/tar.gz/refs/tags/3.1.2'
JPEG_SHA256 = '560f6338b547544c4f9721b18d8b87685d433ec78b3c644c70d77adad22c55e6'


def jpeg_source():
    deps = ROOT/'.deps'
    source = deps/'libjpeg-turbo-3.1.2'
    if source.exists():
        return source
    deps.mkdir(exist_ok=True)
    archive = deps/'libjpeg-turbo-3.1.2.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(JPEG_URL, timeout=60) as src, archive.open('xb') as dst:
            shutil.copyfileobj(src, dst)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != JPEG_SHA256:
        raise ValueError('libjpeg-turbo source archive differs from the pinned 3.1.2 input')
    with tarfile.open(archive, 'r:gz') as package:
        package.extractall(deps, filter='data')
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('target', choices=['android', 'host'])
    parser.add_argument('--ndk', type=Path, default=os.environ.get('PICOET_NDK'))
    parser.add_argument('--cmake', default='cmake')
    parser.add_argument('--ninja', default='ninja')
    parser.add_argument('--vcvars', type=Path, help='Optional vcvars64.bat when not in a VS developer shell')
    parser.add_argument('--jpeg-source', type=Path, help='Existing libjpeg-turbo 3.1.2 source; no download')
    args = parser.parse_args()
    build = ROOT/('build-'+args.target)
    build.mkdir(exist_ok=True)
    env = os.environ.copy()
    if args.vcvars:
        script = build/'vcenv.cmd'
        script.write_text(f'@call "{args.vcvars}" >nul\n@if errorlevel 1 exit /b 1\n@set\n', encoding='utf-8')
        result = subprocess.run(['cmd.exe', '/d', '/c', str(script)], capture_output=True, check=True)
        for line in result.stdout.decode('mbcs').splitlines():
            if '=' in line and not line.startswith('='):
                key, value = line.split('=', 1)
                env[key] = value
    common = ['-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_MAKE_PROGRAM={args.ninja}']
    if args.target == 'android':
        if not args.ndk:
            parser.error('Android builds need --ndk or PICOET_NDK (tested: NDK 26.1.10909125)')
        common += [f'-DCMAKE_TOOLCHAIN_FILE={args.ndk.resolve()}/build/cmake/android.toolchain.cmake',
                   '-DANDROID_ABI=arm64-v8a', '-DANDROID_PLATFORM=android-29', '-DANDROID_STL=c++_static']
    def run(*values):
        command = list(map(str, values))
        print(subprocess.list2cmdline(command), flush=True)
        subprocess.run(command, env=env, check=True)
    jpeg = (args.jpeg_source or jpeg_source()).resolve()
    jpeg_build = ROOT/('build-jpeg-'+args.target)
    run(args.cmake, '-S', jpeg, '-B', jpeg_build, *common, '-DENABLE_SHARED=OFF', '-DENABLE_STATIC=ON',
        '-DWITH_TOOLS=OFF', '-DWITH_TESTS=OFF', '-DWITH_SIMD='+('ON' if args.target == 'android' else 'OFF'))
    run(args.cmake, '--build', jpeg_build, '--target', 'jpeg-static', '--parallel', '8')
    library = jpeg_build/('jpeg-static.lib' if args.target == 'host' else 'libjpeg.a')
    run(args.cmake, '-S', ROOT, '-B', build, *common, f'-DPTJ_JPEG_SOURCE={jpeg}',
        f'-DPTJ_JPEG_BUILD={jpeg_build}', f'-DPTJ_JPEG_LIBRARY={library}')
    targets = ['ipc_layout', 'ptj_source', 'ptj_ipc_daemon', 'picoet_camera_jpeg', 'picoet-inject'] if args.target == 'android' else ['all']
    run(args.cmake, '--build', build, '--target', *targets, '--parallel', '8')
    if args.target == 'android':
        # EXCLUDE_FROM_ALL keeps the normal enhancement hook out of this build.
        shutil.copy2(build/'hook/picoet-inject', build/'picoet-inject')
    for name in ('LICENSE.md', 'README.ijg'):
        shutil.copy2(jpeg/name, build/name)
    print('Build complete:', build)


if __name__ == '__main__':
    main()
