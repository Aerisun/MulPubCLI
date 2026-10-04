"""Shared QR login lifecycle and truthful Cookie expiry metadata."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from typing import Callable

from .http import iter_cookies


def wait_for_qr_login(
    *, platform: str, qr_image: str,
    begin: Callable[[bool], dict], poll: Callable[[], dict],
    save: Callable[[], None], emit: Callable[[dict], None],
    interval_seconds: float = 2, sleep: Callable[[float], None] | None = None,
) -> dict:
    """Keep one command alive until the platform authenticates or expires its QR.

    The platform owns QR validity. No guessed fixed deadline and no silent QR
    rotation: an expired QR is reported so the user knows which image to scan.
    """
    sleeper = sleep or time.sleep
    result = begin(False)
    save()
    if result.get('status') != 'waiting':
        return result
    emit({'status': 'waiting', 'platform': platform,
          'qr_image': result.get('qr_image') or qr_image,
          'message': f'请用 {platform} App 扫描二维码；命令正在等待确认'})
    while True:
        sleeper(interval_seconds)
        result = poll()
        save()
        if result.get('status') in ('waiting', 'scanned', 'waiting_confirmation'):
            continue
        return result


def cookie_expiry_metadata(cookies: list[dict], *, auth_names: tuple[str, ...]) -> dict:
    """Summarize actual Cookie expiry attributes, never infer a session lifetime."""
    expirations: dict[str, str | None] = {}
    for item in cookies:
        name = item.get('name')
        if not isinstance(name, str) or not name:
            continue
        raw = item.get('expires')
        expiry = None
        if type(raw) in (int, float) and math.isfinite(raw) and raw > 0:
            try:
                expiry = datetime.fromtimestamp(raw, timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                pass
        expirations[name] = expiry
    known = [expirations[name] for name in auth_names if expirations.get(name)]
    return {'cookie_expirations': expirations, 'expires_at': min(known) if known else None}


def response_cookie_expirations(response) -> list[dict]:
    """Read only Cookie names and expiry attributes from one HTTP response."""
    known: dict[str, dict] = {}
    jar = getattr(response, 'cookies', None)
    if jar is not None:
        for cookie in iter_cookies(jar):
            known[cookie.name] = {'name': cookie.name, 'expires': cookie.expires}
    headers = getattr(response, 'headers', None)
    if headers is not None:
        lines = (headers.get_list('set-cookie') if hasattr(headers, 'get_list')
                 else [headers.get('set-cookie')] if headers.get('set-cookie') else [])
        for line in lines:
            parsed = SimpleCookie()
            try:
                parsed.load(line)
            except Exception:
                continue
            for name, morsel in parsed.items():
                expiry = None
                if morsel['max-age']:
                    try:
                        expiry = int(time.time()) + int(morsel['max-age'])
                    except ValueError:
                        pass
                elif morsel['expires']:
                    try:
                        expiry = int(parsedate_to_datetime(morsel['expires']).astimezone(timezone.utc).timestamp())
                    except (TypeError, ValueError, OverflowError):
                        pass
                known[name] = {'name': name, 'expires': expiry}
    return list(known.values())
