"""Load the pinned local protocol source and serialize its device session as JSON."""
from dataclasses import asdict, fields
from pathlib import Path
import subprocess
import sys

SIGNER_REV = 'e86f82bbf07e36b30c74a00f2b6745d63cb6cadc'


def load_source(source: Path) -> Path:
    source = source.resolve()
    if not source.is_dir():
        print(f"首次初始化小红书环境，正在下载签名库到 {source}...")
        source.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(['git', 'init', str(source)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(source), 'remote', 'add', 'origin', 'https://github.com/cv-cat/XHS_ALL_IN_ONE.git'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(source), 'fetch', '--depth', '1', 'origin', SIGNER_REV], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(source), 'checkout', '--detach', SIGNER_REV], check=True, capture_output=True)
        print("正在安装 Node.js 依赖 (crypto-js)...")
        subprocess.run(['npm', 'install', 'crypto-js'], cwd=str(source), check=True, capture_output=True)
        print("小红书环境初始化完成。")
        
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    clean = subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--', 'xhs_utils', 'apis/xhs_creator_login_apis.py', 'apis/xhs_pc_login_apis.py'], check=False).returncode == 0
    if revision != SIGNER_REV or not clean:
        raise ValueError(f'小红书签名源码版本不匹配或已被修改，请删除 {source} 后重试')
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
        
    return source



def dump_profile(profile) -> dict:
    snapshot = asdict(profile)
    result = {f.name: snapshot[f.name] for f in fields(profile) if f.init}
    result['cookies'] = profile.cookie_map
    result['runtime'] = {
        'named_states': {key: asdict(value) for key, value in profile._named_b1_states.items()},
        'named_values': profile._named_b1_values,
        'explicit': getattr(profile, '_b1_state_explicit', False),
        'overrides': sorted(getattr(profile, '_mns_stage_overrides', [])),
    }
    return result


def _filter_dc(cls, data: dict) -> dict:
    """Keep only keys that *cls* actually accepts as __init__ arguments."""
    allowed = {f.name for f in fields(cls) if f.init}
    return {k: v for k, v in data.items() if k in allowed}


def restore_profile(data: dict):
    from xhs_utils.xhs_creator.state import CreatorDeviceProfile, CreatorSessionState, CreatorB1RuntimeState, CreatorMnsMaterial
    values = dict(data)
    runtime = values.pop('runtime', {})
    values['session'] = CreatorSessionState(**_filter_dc(CreatorSessionState, values['session']))
    values['b1_state'] = CreatorB1RuntimeState(**_filter_dc(CreatorB1RuntimeState, values['b1_state']))
    for name in ('mns_stages', 'mns_profiles'):
        values[name] = {key: CreatorMnsMaterial(**_filter_dc(CreatorMnsMaterial, {**material, 'env_fp_tail': tuple(material['env_fp_tail'])}))
                        for key, material in values[name].items()}
    profile = CreatorDeviceProfile(**_filter_dc(CreatorDeviceProfile, values))
    profile._named_b1_states = {key: CreatorB1RuntimeState(**_filter_dc(CreatorB1RuntimeState, value)) for key, value in runtime.get('named_states', {}).items()}
    profile._named_b1_values = runtime.get('named_values', {})
    profile._b1_state_explicit = runtime.get('explicit', False)
    profile._mns_stage_overrides = set(runtime.get('overrides', []))
    return profile


def restore_pc_profile(data: dict):
    from xhs_utils.xhs_pc.state import PcDeviceProfile, PcSessionState, B1RuntimeState, MnsStageMaterial
    # Also accept the project-local research snapshot made before CLI integration.
    values = {f.name: data[f.name] for f in fields(PcDeviceProfile) if f.init and f.name in data}
    values['cookies'] = data.get('_cookie_map', data['cookies'])
    values['session'] = PcSessionState(**_filter_dc(PcSessionState, values['session']))
    values['b1_state'] = B1RuntimeState(**_filter_dc(B1RuntimeState, values['b1_state']))
    values['mns_stages'] = {key: MnsStageMaterial(**_filter_dc(MnsStageMaterial, {**value, 'env_fp_tail': tuple(value['env_fp_tail'])}))
                            for key, value in values['mns_stages'].items()}
    profile = PcDeviceProfile(**_filter_dc(PcDeviceProfile, values))
    runtime = data.get('runtime', {})
    profile._named_b1_states = {key: B1RuntimeState(**_filter_dc(B1RuntimeState, value)) for key, value in
        runtime.get('named_states', data.get('_named_b1_states', {})).items()}
    profile._named_b1_values = runtime.get('named_values', data.get('_named_b1_values', {}))
    return profile
