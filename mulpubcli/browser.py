"""按需启动 Playwright Chromium 完成登录，并在结束时关闭浏览器。"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .http import HTTPFailure


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise HTTPFailure(
            "缺少浏览器登录依赖。请安装 playwright 后执行 "
            "playwright install chromium", kind='local_environment') from exc
    return sync_playwright


def _env_proxy() -> dict | None:
    """从标准环境变量构造 Playwright 代理配置。

    这台机器（及很多出口受限的开发机）靠本地 HTTP 代理转发外网，curl 通过
    HTTPS_PROXY 走代理，而 Chromium 默认忽略环境代理直接连，会被防火墙空响应
    拒绝（net::ERR_EMPTY_RESPONSE）。这里按 https_proxy/http_proxy 生成代理配置，
    no_proxy 同步为 bypass，让浏览器登录走和 curl 一样的出口。
    """
    proxy_url = (os.environ.get('https_proxy') or os.environ.get('http_proxy')
                 or os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY'))
    if not proxy_url:
        return None
    cfg = {'server': proxy_url}
    no_proxy = os.environ.get('no_proxy') or os.environ.get('NO_PROXY')
    if no_proxy:
        cfg['bypass'] = ','.join(x.strip() for x in no_proxy.split(',') if x.strip())
    return cfg


def _proxy_config(proxy) -> dict | None:
    """规约显式/环境代理为 Playwright 的 proxy 配置：显式优先，缺省走环境代理。"""
    if proxy is None:
        return _env_proxy()
    if isinstance(proxy, str):
        return {'server': proxy}
    if isinstance(proxy, dict):
        return proxy
    raise TypeError('proxy 必须是 URL 字符串或 {server, bypass} 字典')


@dataclass
class LoginResult:
    status: str                     # ok / need_human / failed
    message: str = ''
    exported: dict = field(default_factory=dict)
    timed_out: bool = False


class PlaywrightLoginer:
    """按需拉起 Chromium，跑完登录流程后立即关闭。

    每次 login() 都是全新实例，用完即关，不在两次调用间持有浏览器进程。
    """

    DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

    def __init__(self, *, user_agent: str = DEFAULT_UA,
                 storage_state_dir: Path | None = None,
                 profile_name: str = 'sohu-profile',
                 proxy: dict | str | None = None):
        if not profile_name or profile_name in ('.', '..') or '/' in profile_name or '\\' in profile_name:
            raise ValueError('浏览器配置目录名无效')
        self.user_agent = user_agent
        self.storage_state_dir = storage_state_dir
        self.profile_name = profile_name
        # 显式 proxy 优先；未给则按环境代理自动配置（无代理则为 None）。
        self.proxy = _proxy_config(proxy)
        self._profile: str | None = None
        self._browser = None
        self._context = None
        self._pw = None

    def _setup_profile(self) -> str | None:
        if not self.storage_state_dir:
            return None
        self.storage_state_dir.mkdir(parents=True, exist_ok=True)
        # 持久化 user-data 目录：复用同一设备指纹（preview-dv-id），已授权设备不重复验证。
        profile_dir = self.storage_state_dir / self.profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile = str(profile_dir)
        return self._profile

    def _launch(self, *, headless: bool, viewport: tuple[int, int] = (1280, 720)):
        sync_playwright = _playwright()
        self._pw = sync_playwright().start()
        browser_kwargs = {
            "headless": headless,
            "args": [
                "--no-sandbox", "--disable-gpu", "--no-first-run",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        context_kwargs = {
            "user_agent": self.user_agent,
            "viewport": {"width": viewport[0], "height": viewport[1]},
            "locale": "zh-CN",
        }
        if self.proxy:
            browser_kwargs["proxy"] = self.proxy
        # 持久化 user-data 目录用 launch_persistent_context（返回的就是 BrowserContext）；
        # 不用持久化时直接 launch 再 new_context。两者都规范化到 self._context。
        if self._profile:
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=self._profile, **browser_kwargs, **context_kwargs)
            self._browser = None
        else:
            self._browser = self._pw.chromium.launch(**browser_kwargs)
            self._context = self._browser.new_context(**context_kwargs)

    # ─────────────────────────────────────────────
    # 公共接口
    # ─────────────────────────────────────────────

    def login(self, url: str, perform: Callable, *,
              headless: bool = True,
              on_human_needed: Callable | None = None,
              human_done: Callable | None = None,
              wait_after_auto_ms: int = 2000,
              wait_between_checks_ms: int = 1000,
              wait_for_human_update: Callable | None = None,
              max_human_wait_ms: int = 300_000,
              viewport: tuple[int, int] = (1280, 720)) -> LoginResult:
        """执行一次完整登录流程，用完即关。

        url      — 初始登录页地址。
        perform  — 登录自动化回调，签名 (page, browser, ctx) -> bool。
                   返回 True 表示需要人工操作，False 表示已完成。
        on_human_needed — 可选：需要人工操作时通知调用方，无参数。
        human_done — 可选：签名 (page) -> bool，真人环节完成判定。仅在 perform
                   返回 True 后用于轮询等待；不会重跑 perform，避免重复提交。
        wait_for_human_update — 可选：签名 (page, remaining_ms) -> None。等待页面自身事件后再
                   检查真人操作结果；不设置时按 wait_between_checks_ms 定时检查。
        """
        if not self.storage_state_dir:
            raise ValueError("未指定 storage_state_dir，无法保存浏览器登录态")
        self._setup_profile()
        result = LoginResult(status="ok")
        try:
            self._launch(headless=headless, viewport=viewport)
            ctx = self._context
            page = ctx.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_after_auto_ms)

            human = perform(page, self._browser, ctx)
            if human:
                result.status = "need_human"
                result.message = "登录需要人工验证"
                if on_human_needed is not None:
                    on_human_needed()
                if human_done is not None:
                    result = self._wait_for_human(human_done, page, wait_between_checks_ms,
                                                  max_human_wait_ms, result,
                                                  wait_for_update=wait_for_human_update)
            return result
        finally:
            self.close()

    def _wait_for_human(self, human_done, page, between_ms, max_ms, result,
                        *, wait_for_update=None):
        deadline = None if max_ms is None else time.monotonic() + max_ms / 1000.0
        while True:
            remaining_ms = (None if deadline is None else
                            max(0, int((deadline - time.monotonic()) * 1000)))
            if remaining_ms == 0:
                break
            if wait_for_update is not None:
                wait_for_update(page, remaining_ms)
            else:
                page.wait_for_timeout(between_ms if remaining_ms is None else
                                      min(between_ms, remaining_ms))
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                state = human_done(page)
                if state == 'expired':
                    result.status = 'expired'
                    result.message = '二维码已过期，请重新执行 login 获取新码。'
                    return result
                if state is True:
                    result.status = "ok"
                    result.message = "真人验证已完成，登录继续。"
                    return result
            except Exception:
                if deadline is None:
                    raise
        result.status = "failed"
        result.message = "等待人工验证超时。"
        result.timed_out = True
        return result

    def close(self):
        # 持久化模式用 context，普通模式用 browser，逐个尝试关闭并清理。
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
