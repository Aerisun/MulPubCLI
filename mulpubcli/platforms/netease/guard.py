"""NetEase public-publish anti-bot token (ursToken) handling, kept honest.

网易号公众发布（operation="publish"）要求一个 ursToken。该 token 由网易自带的
易盾/Watchman 反自动化 SDK（createNEGuardian({appId:'YD00250021379271'}).getToken()）
在真实浏览器里打点换取：它先向 ac.dun.163.com 拉配置、动态加载 watchman.min.js，
并依赖 localStorage/document/设备指纹。这套东西本质上就是"证明当前是真人浏览器"，
**无法在纯 HTTP 内核里重建，也不应该绕过它**——伪造/绕过正是触发
"该内容为未知第三方工具发布，不能申诉"封禁的原因。

因此公关发布由本模块显式接收一个**已由网易官方守卫 SDK 在浏览器里打出的合法
ursToken**（文件或环境变量），客户端只负责携带它发出纯 HTTP 的 publishV2 请求，
并且只在校验通过（32 位字母数字）后使用。草稿（saveDraft）不需要 ursToken，
仍可全纯 HTTP 完成。

来源建议：用户在自己浏览器里打开网易号编辑器触发一次发布，从 DevTools 网络面板
copy 出请求里的 ursToken，或用扩展脚本 getToken() 打出后填入。令牌为短期凭证，
过期重打即可。这个局限是平台反自动化的固有设计，不是工具能绕开的。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from mulpubcli.http import HTTPFailure, private_json

TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{32}$')


def load_urs_token(*, value: str | None = None, token_file: Path | None = None,
                   cached: Path | None = None) -> str:
    """取一个可用于网易公众发布的 ursToken，来源优先级由高到低。

    value      : 调用方显式传入的 token 字符串（如 --urs-token）；
    token_file : 指向一份新鲜的 token 文本文件（首行非空即 token）；
    cached     : 项目内私有缓存文件（.storage/private/netease-urstoken），
                 存在且格式合法则复用，避免每次发布都要求填。

    返回校验通过的 32 位 token；取不到或格式非法抛 HTTPFailure（草稿不调用）。
    """
    raw = None
    source = 'none'
    if value:
        raw, source = value.strip(), 'inline'
    elif token_file is not None:
        try:
            raw = Path(token_file).read_text(encoding='utf-8').strip().splitlines()[0]
            source = 'file'
        except (OSError, IndexError):
            raw = None
    else:
        raw = os.environ.get('NETEASE_URS_TOKEN', '').strip()
        if raw:
            source = 'env'
    if raw and TOKEN_RE.fullmatch(raw):
        if cached is not None and source != 'cached':
            try:
                private_json(cached, {'urs_token': raw, 'source': source})
            except OSError:
                pass
        return raw
    if cached is not None:
        try:
            data = jsonload(cached)
            token = data.get('urs_token', '')
            if isinstance(token, str) and TOKEN_RE.fullmatch(token):
                return token
        except (OSError, ValueError):
            pass
    raise HTTPFailure(
        '网易公众发布需要合法的 ursToken（易盾反自动化令牌）。请用网易官方守卫 SDK 在'
        '浏览器里打出最新 token，用 --urs-token 传入、写入 NETEASE_URS_TOKEN，或放进'
        '私有令牌缓存；草稿发布不需要。见 mulpubcli/platforms/netease/guard.py。',
        kind='verification_required')


def jsonload(path: Path) -> dict:
    import json
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}