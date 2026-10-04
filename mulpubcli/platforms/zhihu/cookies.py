"""Internal Zhihu cookie pipeline — normalize, validate, and build a session.

知乎登录已彻底切换到真实浏览器扫码（mulpubcli.browser 拉起 Chromium），浏览器登录
拿到的是结构化 Cookie（Playwright context.cookies()），不再需要解析用户手动导出的
Cookie 文件。本模块只保留浏览器导出 Cookie 落盘为正式凭证所复用的内部环节：
  - _allowed_domain / zhihu_records  知乎域名过滤、过期剔除、name/value 换行校验；
  - require_login_cookie             至少一个未过期 z_c0 才能建立知乎登录态；
  - build_session                    Cookie 记录注入 Requests 会话（与 load_session 同源）。

不含任何人工导入逻辑，也不把 Cookie 明文输出到终端或日志。
"""
from __future__ import annotations

import time

import requests

# 知乎允许的 Cookie 目标域名，与 ZhihuWeb.HOSTS 一致（避免循环导入）。
_ZHIHU_HOSTS = frozenset({'www.zhihu.com', 'zhuanlan.zhihu.com', 'api.zhihu.com'})


def _allowed_domain(domain: str) -> bool:
    """域名是否属于知乎（含子域）。空串与非法域名一律拒绝。"""
    d = domain.lstrip('.')
    return '.' in d and any(host == d or host.endswith('.' + d) for host in _ZHIHU_HOSTS)


def _expires(raw) -> int | None:
    """把 Cookie 过期字段规约为秒级时间戳；会话 Cookie 返回 None。

    与 Playwright context.cookies() 约定一致：expires 为 -1 表示会话 Cookie；
    小于等于 0 或缺失一律视为不设过期时间。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = int(raw)
        return None if value <= 0 else value
    return None


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
        raise ValueError('没有有效且未过期的知乎登录 Cookie（z_c0）')


def build_session(records: list[dict]) -> requests.Session:
    """用已过滤的知乎 Cookie 记录构造一个可用的 Requests 会话。

    复用 http.add_cookies，与 load_session 从凭证文件恢复会话使用同一套逻辑，
    保证登录后与发布/核验路径的行为完全一致。
    """
    from mulpubcli.http import add_cookies

    session = requests.Session()
    add_cookies(session, records)
    return session