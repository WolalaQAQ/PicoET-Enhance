"""Version comparison and GitHub Releases update checker for PicoET-Enhance PC Studio."""
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

APP_VERSION = '2.6.2-dev.1'
GITHUB_REPO = 'WolalaQAQ/PicoET-Enhance'
RELEASES_LATEST_API = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
RELEASES_PAGE_URL = f'https://github.com/{GITHUB_REPO}/releases'
MAX_RELEASE_RESPONSE_BYTES = 2 * 1024 * 1024

_VERSION_RE = re.compile(r'^[vV]?(\d+)\.(\d+)\.(\d+)(?:-([^+]+))?(?:\+.*)?$')


def _safe_http_url(url):
    text = str(url or '').strip()
    if not text:
        return ''
    parsed = urlsplit(text)
    if parsed.scheme.lower() in ('http', 'https') and parsed.netloc:
        return text
    return ''


def _parse_prerelease(suffix):
    """Parse SemVer 2.0.0 prerelease identifiers for deterministic comparison."""
    parts = []
    for ident in suffix.split('.'):
        ident = ident.strip()
        if not ident:
            continue
        if ident.isdigit():
            # Numeric identifiers have lower precedence than non-numeric identifiers.
            parts.append((0, int(ident), ''))
        else:
            parts.append((1, 0, ident))
    return tuple(parts)


def parse_version(text):
    """Parse 'vX.Y.Z' or 'X.Y.Z-dev.N' into a comparable tuple ((X, Y, Z), is_stable, prerelease_parts)."""
    m = _VERSION_RE.match(str(text or '').strip())
    if not m:
        return None
    nums = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    suffix = (m.group(4) or '').strip()
    return (nums, suffix == '', _parse_prerelease(suffix))


def is_newer_version(candidate, current):
    """Return True when candidate version is strictly newer than current version."""
    cand = parse_version(candidate)
    curr = parse_version(current)
    if cand is None or curr is None:
        return False
    if cand[0] != curr[0]:
        return cand[0] > curr[0]
    if cand[1] != curr[1]:
        return cand[1] > curr[1]
    return cand[2] > curr[2]


def summarize_notes(body, max_lines=6):
    """Extract up to max_lines concise bullet or text lines from release notes markdown."""
    lines = []
    for raw in str(body or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line == '---':
            continue
        if line.startswith(('- ', '* ')):
            line = line[2:].strip()
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def classify_assets(assets):
    """Pick installer (.exe), portable (.zip), and headset module (.zip) download URLs from release assets."""
    result = {
        'installer_name': '',
        'installer_url': '',
        'portable_name': '',
        'portable_url': '',
        'module_name': '',
        'module_url': '',
    }
    for item in assets or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        url = _safe_http_url(item.get('browser_download_url'))
        lower = name.lower()
        if not name or not url or lower.endswith('.sha256'):
            continue
        if not result['installer_url'] and 'pc-studio' in lower and lower.endswith('.exe'):
            result['installer_name'] = name
            result['installer_url'] = url
        elif not result['portable_url'] and 'pc-studio' in lower and lower.endswith('.zip'):
            result['portable_name'] = name
            result['portable_url'] = url
        elif not result['module_url'] and 'magisk-picoet-enhance' in lower and lower.endswith('.zip'):
            result['module_name'] = name
            result['module_url'] = url
    return result


class UpdateChecker:
    """Thread-safe GitHub Releases update checker with in-memory TTL caching."""

    def __init__(self, current_version=APP_VERSION, *, api_url=RELEASES_LATEST_API, opener=urlopen, cache_ttl=300.0):
        self.current_version = str(current_version)
        self.api_url = api_url
        self.opener = opener
        self.cache_ttl = float(cache_ttl)
        self.lock = threading.Lock()
        self._cached = None
        self._cached_mono = 0.0

    def _base_response(self):
        return {
            'current_version': self.current_version,
            'latest_version': None,
            'tag_name': '',
            'update_available': False,
            'release_url': RELEASES_PAGE_URL,
            'installer_name': '',
            'installer_url': '',
            'portable_name': '',
            'portable_url': '',
            'module_name': '',
            'module_url': '',
            'published_at': '',
            'notes': [],
            'checked_at': int(time.time()),
            'error': None,
        }

    def check(self, force=False):
        with self.lock:
            now_mono = time.monotonic()
            if (
                not force
                and self._cached is not None
                and self._cached.get('error') is None
                and (now_mono - self._cached_mono) < self.cache_ttl
            ):
                return dict(self._cached)

        res = self._base_response()
        req = Request(
            self.api_url,
            headers={
                'Accept': 'application/vnd.github+json',
                'User-Agent': f'PicoET-PC-Studio/{self.current_version}',
            },
        )
        try:
            with self.opener(req, timeout=4.0) as resp:
                raw_bytes = resp.read(MAX_RELEASE_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_RELEASE_RESPONSE_BYTES:
                    raise ValueError('GitHub release payload exceeded size limit')
                raw = raw_bytes.decode('utf-8')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('Unexpected GitHub release payload')
            tag_name = str(payload.get('tag_name') or '').strip()
            latest_ver = tag_name.lstrip('vV') or str(payload.get('name') or '').lstrip('vV').strip()
            res['tag_name'] = tag_name or (f'v{latest_ver}' if latest_ver else '')
            res['latest_version'] = latest_ver or self.current_version
            res['update_available'] = is_newer_version(res['latest_version'], self.current_version)
            res['release_url'] = _safe_http_url(payload.get('html_url')) or RELEASES_PAGE_URL
            res['published_at'] = str(payload.get('published_at') or '')
            res['notes'] = summarize_notes(payload.get('body') or '')
            res.update(classify_assets(payload.get('assets')))
        except HTTPError as exc:
            if exc.code == 404:
                res['latest_version'] = self.current_version
                res['tag_name'] = f'v{self.current_version}'
            else:
                res['error'] = f'HTTP {exc.code}'
        except (URLError, OSError, ValueError) as exc:
            reason = getattr(exc, 'reason', None)
            res['error'] = str(reason or exc)

        with self.lock:
            self._cached = dict(res)
            self._cached_mono = time.monotonic()
        return res
