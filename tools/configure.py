"""Prepare a private configuration without overwriting existing settings."""
import argparse
import getpass
import json
from io import StringIO
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from urllib.parse import urlsplit
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_core.secrets import validate_secret


def write_private(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(path: Path):
    path = path.resolve()
    text = path.read_text(encoding='utf-8-sig') if path.exists() else (ROOT / '.env.example').read_text(encoding='utf-8')
    values = dict(dotenv_values(stream=StringIO(text)))
    for key in ('SECRET_KEY', 'JWT_SECRET_KEY', 'RUNTIME_BOOTSTRAP_SECRET'):
        value = values.get(key, '')
        if key == 'RUNTIME_BOOTSTRAP_SECRET' and key not in values:
            value = values.get('S1_RUNTIME_BOOTSTRAP_SECRET', '')
        try:
            validate_secret(key, value)
            if key == 'JWT_SECRET_KEY' and value == values.get('SECRET_KEY'):
                raise RuntimeError('Shared key')
            if key in values:
                continue  # Preserve quoting, comments and valid administrator settings.
        except RuntimeError:
            value = secrets.token_urlsafe(48)
        values[key] = value
        pattern = rf'^[ \t]*(?:export[ \t]+)?{key}[ \t]*=.*$'
        if re.search(pattern, text, re.M):
            text = re.sub(pattern, lambda _: f'{key}={value}', text, flags=re.M)
        else:
            text = text.rstrip() + f'\n{key}={value}\n'
    write_private(path, text)
    return path


def normalize_api_base(value: str) -> str:
    value = value.strip().rstrip('/')
    if value.endswith('/chat/completions'):
        value = value[:-len('/chat/completions')]
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise ValueError('请填写 http(s) 接口基础地址，不要附带账号密码、查询参数或网页路径。')
    return value


def configure_model(path: Path, *, ask=input, hidden=getpass.getpass):
    """Configure a profile locally; never send credentials to a provider here."""
    path = path.resolve()
    text = path.read_text(encoding='utf-8-sig')
    values = dict(dotenv_values(stream=StringIO(text)))
    raw = values.get('RUNTIME_MODEL_PROFILES_JSON') or values.get('ADVENTURER_S1_AI_PROFILES_JSON') or '[]'
    try:
        profiles = json.loads(raw)
    except ValueError:
        raise ValueError('已有模型配置不是有效 JSON，请先修复配置文件。') from None
    if not isinstance(profiles, list) or any(not isinstance(p, dict) for p in profiles):
        raise ValueError('已有模型配置必须是对象列表，请先修复配置文件。')
    current = profiles[0] if profiles else {
        'id': 'default', 'provider': 'openai_compatible',
        'api_base': values.get('DEEPSEEK_API_BASE') or '',
        'api_key': values.get('DEEPSEEK_API_KEY') or '',
        'model': values.get('DEEPSEEK_MODEL') or '',
    }
    existing = all(current.get(k) for k in ('api_base', 'api_key', 'model'))
    if existing:
        print('检测到已有模型配置。默认保留；修改时仅编辑第一个模型，其他模型与用途分配保留。')
        if ask('是否修改模型配置？[y/N] ').strip().lower() != 'y':
            return False
    print('填写服务商提供的 OpenAI Chat Completions 兼容接口信息。不会在这里发起模型请求。')
    print('接口地址示意：https://your-provider.example/v1（不要照抄示意地址）')
    base_input = ask('接口基础地址（已有配置可直接回车保留）：').strip() or str(current.get('api_base') or '')
    api_base = normalize_api_base(base_input)
    model = ask('模型 ID（从服务商模型列表复制；已有配置可回车保留）：').strip() or str(current.get('model') or '')
    api_key = hidden('API Key（隐藏输入；已有配置可回车保留）：').strip() or str(current.get('api_key') or '')
    if not model or not api_key or any(ord(c) < 32 for c in model + api_key):
        raise ValueError('模型 ID 和 API Key 必须非空，且不能含换行或控制字符。')
    updated = {**current, 'api_base': api_base, 'model': model, 'api_key': api_key}
    profiles = [updated, *profiles[1:]] if profiles else [updated]
    # Encode dollars inside JSON so dotenv interpolation cannot alter credentials.
    encoded = json.dumps(profiles, ensure_ascii=True, separators=(',', ':')).replace('$', '\\u0024')
    quoted = "'" + encoded.replace('\\', '\\\\').replace("'", "\\'") + "'"
    setting = 'RUNTIME_MODEL_PROFILES_JSON=' + quoted
    pattern = r'^[ \t]*(?:export[ \t]+)?RUNTIME_MODEL_PROFILES_JSON[ \t]*=.*$'
    if re.search(pattern, text, re.M):
        text = re.sub(pattern, lambda _: setting, text, flags=re.M)
    else:
        text = text.rstrip() + '\n' + setting + '\n'
    write_private(path, text)
    print('模型配置已保存，API Key 不会显示。若服务正在运行，请正常停止后重新启动。')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path(os.getenv('PLATFORM_ENV_FILE', str(ROOT / '.env'))))
    parser.add_argument('--model', action='store_true', help='交互填写接口地址、模型 ID 和 API Key')
    args = parser.parse_args()
    print(f'Configuration ready: {configure(args.env_file)} (secret values not printed)')
    if args.model:
        try:
            configure_model(args.env_file)
        except (ValueError, EOFError):
            raise SystemExit('模型配置未完成。请检查输入后重新运行 --model；原模型配置未被覆盖。') from None
