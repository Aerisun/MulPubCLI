"""Parse browser-exported Zhihu cookies into the project session format.

用户在浏览器登录知乎后把 Cookie 导出成文件，这里把它解析成项目内
`.storage/auth/zhihu.json` 相同的凭证记录，让现有的 account / load / save /
发布 / 核验链路直接复用。

支持四种格式（自动识别，越靠前越优先）：
  - 原始 Cookie 头文本：`name=value; name=value; ...`（可直接从 Chrome DevTools
    的 Cookie 头复制，或保存成 txt 文件），这是最简单、最推荐的方式；
  - Chrome / Edge 扩展（EditThisCookie 等）导出的 JSON 数组；
  - Playwright context.cookies() 的 JSON 数组；
  - Netscape cookies.txt（`# Netscape HTTP Cookie File`，tab 分隔）。

原始 Cookie 头不带域名/过期信息，统一按知乎 `.zhihu.com` 归属处理。只保留知乎
域名的 Cookie，绝不把其他站点的会话混入知乎凭证；解析时做完整校验：域名过滤、
过期剔除、name/value 换行注入拒绝、z_c0 必须存在且未过期。解析过程不把 Cookie
明文输出到终端或日志。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

# 知乎允许的 Cookie 目标域名，与 ZhihuWeb.HOSTS 一致（避免循环导入）。
_ZHIHU_HOSTS = frozenset({'www.zhihu.com', 'zhuanlan.zhihu.com', 'api.zhihu.com'})


def _allowed_domain(domain: str) -> bool:
    """域名是否属于知乎（含子域）。空串与非法域名一律拒绝。"""
    d = domain.lstrip('.')
    return '.' in d and any(host == d or host.endswith('.' + d) for host in _ZHIHU_HOSTS)


def _expires(raw) -> int | None:
    """把各格式的过期字段规约为秒级时间戳；会话 Cookie 返回 None。

    Chrome 扩展用 expirationDate，Playwright 用 expires（-1 表示会话），
    Netscape 用 0 表示会话。小于等于 0 或缺失一律视为不设过期时间。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = int(raw)
        return None if value <= 0 else value
    return None


def _normalize_json_item(item: dict) -> dict | None:
    """把单个 JSON Cookie 规约为受控记录；结构非法返回 None（跳过）。"""
    name, value = item.get('name'), item.get('value')
    domain = item.get('domain')
    if not isinstance(name, str) or not isinstance(value, str) or not isinstance(domain, str):
        return None
    path = item.get('path')
    if not isinstance(path, str):
        path = '/'
    same_site = item.get('sameSite', item.get('same_site'))
    http_only = item.get('httpOnly', item.get('http_only', False))
    host_only = item.get('hostOnly', item.get('host_only', False))
    return {
        'name': name,
        'value': value,
        'domain': domain,
        'path': path,
        'secure': bool(item.get('secure', False)),
        'expires': _expires(item.get('expirationDate', item.get('expires'))),
        'domain_specified': True,
        'domain_initial_dot': domain.startswith('.'),
        'path_specified': True,
        'http_only': http_only is True,
        'same_site': same_site if isinstance(same_site, str) else None,
        'host_only': host_only is True,
    }


def _parse_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'Cookie 文件不是有效 JSON：{exc}') from None
    if not isinstance(data, list):
        raise ValueError('Cookie JSON 顶层必须是数组')
    records = []
    for item in data:
        if isinstance(item, dict):
            normalized = _normalize_json_item(item)
            if normalized is not None:
                records.append(normalized)
    return records


def _parse_netscape(text: str) -> list[dict]:
    """解析 Netscape cookies.txt（tab 分隔：domain  includeSubdomains  path  secure  expires  name  value）。"""
    records = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) < 7:
            continue  # 容忍损坏行，不整体报错
        domain, include_sub, path, secure, expires, name, value = fields[:7]
        if len(fields) > 7:  # value 本身可能含 tab
            value = '\t'.join(fields[7:])
        records.append({
            'name': name,
            'value': value,
            'domain': domain,
            'path': path or '/',
            'secure': secure.upper() == 'TRUE',
            'expires': _expires(expires),
            'domain_specified': True,
            'domain_initial_dot': domain.startswith('.'),
            'path_specified': True,
            'http_only': False,  # cookies.txt 不携带 HttpOnly 标记
            'same_site': None,
            'host_only': include_sub.upper() != 'TRUE',
        })
    return records


def _parse_cookie_header(text: str) -> list[dict]:
    """解析原始 Cookie 头文本：`name=value; name=value; ...`。

    支持直接粘贴浏览器 DevTools 的 Cookie 头（含或不含 `Cookie:` 前缀）。这种
    格式没有域名/路径/过期信息，统一按知乎 `.zhihu.com` 归属。value 本身可能含
    等号，因此只按第一个 `=` 切分 name/value。
    """
    body = text.strip()
    if body.lower().startswith('cookie:'):
        body = body[len('cookie:'):].lstrip()
    records = []
    for segment in body.split(';'):
        segment = segment.strip()
        if not segment or '=' not in segment:
            continue
        name, value = segment.split('=', 1)
        name = name.strip()
        if not name:
            continue
        records.append({
            'name': name,
            'value': value,
            'domain': '.zhihu.com',
            'path': '/',
            'secure': False,
            'expires': None,
            'domain_specified': True,
            'domain_initial_dot': True,
            'path_specified': True,
            'http_only': False,
            'same_site': None,
            'host_only': False,
        })
    return records


def _looks_like_netscape(text: str) -> bool:
    """cookies.txt 的字段用 tab 分隔；原始 Cookie 头用 `; ` 分隔，不含 tab。"""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        return '\t' in line
    return False


def parse_cookie_text(text: str) -> list[dict]:
    """解析 Cookie 文本并返回原始记录列表（未做知乎过滤）。"""
    if text.lstrip().startswith(('{', '[')):
        return _parse_json(text)
    if _looks_like_netscape(text):
        return _parse_netscape(text)
    return _parse_cookie_header(text)


def parse_cookie_file(path: Path) -> list[dict]:
    """读取 Cookie 文件并解析为原始记录列表（未做知乎过滤）。"""
    return parse_cookie_text(path.read_text(encoding='utf-8', errors='replace'))


def zhihu_records(records: list[dict], *, now=None) -> list[dict]:
    """按知乎域名过滤并剔除已过期 Cookie，做 name/value 安全校验。"""
    now = time.time() if now is None else now
    out = []
    for item in records:
        if not _allowed_domain(item['domain']):
            continue
        name, value = item['name'], item['value']
        if not name or any(c in name for c in '\r\n') or any(c in value for c in '\r\n'):
            raise ValueError('Cookie 名称或值无效')
        expiry = item['expires']
        if isinstance(expiry, int) and expiry > 0 and expiry <= now:
            continue  # 已过期，剔除
        out.append(item)
    return out


def require_login_cookie(records: list[dict], *, now=None) -> None:
    """要求至少存在一个未过期、非空的 z_c0，否则无法建立知乎登录态。"""
    now = time.time() if now is None else now
    if not any(
        r['name'] == 'z_c0'
        and r.get('value')
        and (r.get('expires') is None or r['expires'] > now)
        for r in records
    ):
        raise ValueError('导入文件中没有有效且未过期的知乎登录 Cookie（z_c0）')


def build_session(records: list[dict]) -> requests.Session:
    """用已过滤的知乎 Cookie 记录构造一个可用的 Requests 会话。

    复用 http.add_cookies，与 load_session 从凭证文件恢复会话使用同一套逻辑，
    保证导入后与发布/核验路径的行为完全一致。
    """
    from mutipubcli.http import add_cookies

    session = requests.Session()
    add_cookies(session, records)
    return session
