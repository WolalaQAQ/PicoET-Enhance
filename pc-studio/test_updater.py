"""Offline tests for SemVer update comparison, URL sanitization, and payload bounds."""
import io
import json
import unittest

from updater import (
    MAX_RELEASE_RESPONSE_BYTES,
    RELEASES_PAGE_URL,
    UpdateChecker,
    classify_assets,
    is_newer_version,
    parse_version,
)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class UpdaterTests(unittest.TestCase):
    def test_semver_prerelease_numeric_and_build_metadata(self):
        self.assertTrue(is_newer_version('2.6.2-dev.10', '2.6.2-dev.2'))
        self.assertFalse(is_newer_version('2.6.2-dev.2', '2.6.2-dev.10'))
        self.assertTrue(is_newer_version('v2.7.0', '2.7.0-rc.1'))
        self.assertFalse(is_newer_version('2.7.0-rc.1', '2.7.0'))
        self.assertTrue(is_newer_version('2.7.0-beta.1', '2.7.0-alpha.9'))
        self.assertTrue(is_newer_version('2.7.0-alpha.beta', '2.7.0-alpha.1'))
        self.assertFalse(is_newer_version('2.7.0+build.2', '2.7.0+build.1'))
        self.assertIsNone(parse_version('not-a-version'))

    def test_asset_and_release_url_scheme_validation(self):
        assets = classify_assets([
            {'name': 'PicoET-PC-Studio-Setup-v2.7.0.exe', 'browser_download_url': 'javascript:alert(1)'},
            {'name': 'picoet-pc-studio-v2.7.0.zip', 'browser_download_url': 'file:///C:/evil.zip'},
            {'name': 'magisk-picoet-enhance-v2.7.0.zip', 'browser_download_url': 'https://example.invalid/magisk.zip'},
            {'name': 'magisk-picoet-enhance-v2.7.0.zip.sha256', 'browser_download_url': 'https://example.invalid/magisk.zip.sha256'},
        ])
        self.assertEqual(assets['installer_url'], '')
        self.assertEqual(assets['portable_url'], '')
        self.assertEqual(assets['module_url'], 'https://example.invalid/magisk.zip')

        payload = {
            'tag_name': 'v2.7.0',
            'name': None,
            'html_url': 'javascript:void(0)',
            'published_at': None,
            'body': None,
            'assets': None,
        }
        checker = UpdateChecker(
            '2.6.2-dev.1',
            opener=lambda req, timeout=4.0: _FakeResponse(json.dumps(payload).encode('utf-8')),
        )
        res = checker.check(force=True)
        self.assertIsNone(res['error'])
        self.assertTrue(res['update_available'])
        self.assertEqual(res['release_url'], RELEASES_PAGE_URL)
        self.assertEqual(res['notes'], [])

    def test_oversized_release_payload_is_rejected(self):
        checker = UpdateChecker(
            '2.6.2-dev.1',
            opener=lambda req, timeout=4.0: _FakeResponse(b'{' + b' ' * (MAX_RELEASE_RESPONSE_BYTES + 16) + b'}'),
        )
        res = checker.check(force=True)
        self.assertIn('size limit', res['error'])
        self.assertFalse(res['update_available'])


if __name__ == '__main__':
    unittest.main()
