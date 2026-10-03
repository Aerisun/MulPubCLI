"""Creator bridge for the pinned source's dsProgram/dsfProgram mismatch.

Reuse its state, headers and JS algorithms. Keep session inputs on stdin and
leave the integrity-checked reference checkout unchanged.
"""
import json
import subprocess
from pathlib import Path

from mulpubcli.http import HTTPFailure


def creator_params(auth, api, data='', method='POST', *, include_client_hints=False,
                   tier=None, b1_profile=None, mns_profile=None):
    from xhs_utils.xhs_creator.params import get_request_headers_template
    from xhs_utils.xhs_creator import runtime

    auth.validate(require_authenticated=True)
    _, material = auth.profile.resolve_mns_material(tier=tier, mns_profile=mns_profile)
    if material.device_tag != 'nop':
        auth.ensure_ds_material()
    profile = auth.profile
    context = profile.next_sign_context(tier=tier, mns_profile=mns_profile)
    cookies = profile.cookie_map
    payload = {key: context[key] for key in (
        'now', 'version', 'loadts', 'seq', 'envConst', 'envFpTail', 'deviceTag',
        'b1b1', 'signCount', 'webBuild', 'signVersion', 'appId', 'platform',
        'dsProgram', 'xt') if key in context}
    payload.update(api=api, data=data, tier=context['tier'],
                   cookie='; '.join(f'{key}={value}' for key, value in cookies.items()),
                   b1=profile.current_b1(context['now'], profile_name=b1_profile),
                   dslPair=profile.dsl_pair(context['now']))
    if context['deviceTag'] != 'nop' and not payload.get('dsProgram'):
        raise HTTPFailure('小红书动态签名材料缺失，已停止')
    # The pinned Python adapter emits dsProgram; signFull reads dsfProgram.
    script = """const fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
input.dsfProgram=input.dsProgram;
const {signFull}=require(process.argv[1]);
process.stdout.write(JSON.stringify(signFull(input)));"""
    module = Path(runtime.__file__).parent.parent / 'xhs_core/js/sign.js'
    try:
        proc = subprocess.run(['node', '-e', script, str(module)],
                              input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, timeout=30)
        if proc.returncode:
            raise ValueError('signer failed')
        result = json.loads(proc.stdout)
        prefix, lo, hi = {'0101': ('mns0101_', 196, 206),
                         '0201': ('mns0201_', 200, 212)}[context['tier']]
        if not (result['x3'].startswith(prefix) and lo <= len(result['x3']) <= hi
                and result['xs'] and result['xs_common']):
            raise ValueError('signer output changed')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise HTTPFailure('小红书签名生成失败，已停止') from None
    headers = get_request_headers_template(context, method=method, include_client_hints=include_client_hints)
    headers.update({'x-s': result['xs'], 'x-t': str(result['xt']), 'x-s-common': result['xs_common']})
    body = '' if data is None or data == '' else data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return headers, cookies, body
