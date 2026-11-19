"""Build the rootless module companion with the Android SDK/JDK, without Gradle."""
import argparse
import os
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, environment):
    result = subprocess.run(list(map(str, arguments)), env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding='utf-8', errors='replace')
    print(result.stdout, end='')
    result.check_returncode()
    return result.stdout


def build(output, version, code, keystore=None, alias='panel'):
    environment = os.environ.copy()
    jdk = Path(environment['JAVA_HOME'])/'bin'
    sdk = Path(environment['ANDROID_HOME'])
    suffix = '.exe' if os.name == 'nt' else ''
    java = lambda name: jdk/(name+suffix)
    bt = sdk/'build-tools/35.0.0'
    binary = lambda name: bt/(name+suffix)
    android = sdk/'platforms/android-35/android.jar'
    output.mkdir(parents=True, exist_ok=False)
    classes = output/'classes'
    classes.mkdir()
    tests = output/'host-tests'
    tests.mkdir()
    sources = ROOT/'headset/src/dev/pico/etenhance'
    run([java('javac'), '-J-Dfile.encoding=UTF-8', '-J-Duser.language=en', '-encoding', 'UTF-8', '--release', '8', '-d', tests,
         *[sources/name for name in ('PanelChannel.java', 'ProcessingSettings.java', 'PanelState.java', 'Scope.java', 'Catalog.java', 'CameraState.java')],
         ROOT/'headset/tests/PanelChannelTest.java', ROOT/'headset/tests/ProcessingSettingsTest.java',
         ROOT/'headset/tests/ScopeTest.java', ROOT/'headset/tests/CameraStateTest.java'], environment)
    log = run([java('java'), '-cp', tests, 'dev.pico.etenhance.PanelChannelTest', output/'fixtures'], environment)
    log += run([java('java'), '-cp', tests, 'dev.pico.etenhance.ProcessingSettingsTest', ROOT/'magisk-picoet-enhance'], environment)
    log += run([java('java'), '-cp', tests, 'dev.pico.etenhance.ScopeTest', ROOT/'magisk-picoet-enhance'], environment)
    log += run([java('java'), '-cp', tests, 'dev.pico.etenhance.CameraStateTest'], environment)
    (output/'host-tests.txt').write_text(log, encoding='utf-8')
    resources = output/'resources.zip'
    generated = output/'generated'
    unsigned, aligned, apk = [output/name for name in ('unsigned.apk', 'aligned.apk', 'PicoETEnhance.apk')]
    run([binary('aapt2'), 'compile', '--dir', ROOT/'headset/res', '-o', resources], environment)
    run([binary('aapt2'), 'link', '-o', unsigned, '--manifest', ROOT/'headset/AndroidManifest.xml',
         '-I', android, '--java', generated, '--version-name', version, '--version-code', str(code), resources], environment)
    sources = list((ROOT/'headset/src').rglob('*.java'))+list(generated.rglob('*.java'))
    run([java('javac'), '-J-Dfile.encoding=UTF-8', '-J-Duser.language=en', '-encoding', 'UTF-8', '--release', '8', '-cp', android, '-d', classes, *sources], environment)
    run([java('java'), '-cp', bt/'lib/d8.jar', 'com.android.tools.r8.D8', '--lib', android,
         '--min-api', '29', '--output', output, *classes.rglob('*.class')], environment)
    with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(output/'classes.dex', 'classes.dex')
    if keystore is None:
        keystore = ROOT/'.local/panel-signing/developer.jks'
        keystore.parent.mkdir(parents=True, exist_ok=True)
        environment['PICOET_PANEL_PASSWORD'] = 'android'
        if not keystore.exists():
            run([java('keytool'), '-J-Dfile.encoding=UTF-8', '-J-Duser.language=en', '-genkeypair', '-keystore', keystore, '-storepass:env', 'PICOET_PANEL_PASSWORD',
                 '-keypass:env', 'PICOET_PANEL_PASSWORD', '-alias', alias, '-dname', 'CN=PicoET Development Only',
                 '-keyalg', 'RSA', '-keysize', '2048', '-validity', '10000'], environment)
    run([binary('zipalign'), '-f', '4', unsigned, aligned], environment)
    signer = [java('java'), '-jar', bt/'lib/apksigner.jar']
    run([*signer, 'sign', '--ks', keystore, '--ks-key-alias', alias,
         '--ks-pass', 'env:PICOET_PANEL_PASSWORD', '--key-pass', 'env:PICOET_PANEL_PASSWORD', '--out', apk, aligned], environment)
    signature = run([*signer, 'verify', '--verbose', '--print-certs', apk], environment)
    (output/'signature.txt').write_text(signature, encoding='utf-8')
    run([binary('zipalign'), '-c', '4', apk], environment)
    print('PANEL_BUILT='+str(apk))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--version-code', type=int, required=True)
    parser.add_argument('--keystore', type=Path)
    parser.add_argument('--alias', default='panel')
    args = parser.parse_args()
    build(args.output.resolve(), args.version, args.version_code, args.keystore, args.alias)
