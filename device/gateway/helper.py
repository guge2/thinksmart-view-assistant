#!/usr/bin/python3
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
import time

from schema import SERVICES, VOICE_FIELDS, MODEL_FIELDS, voice_values, model_values, model_catalog, config_revision

ROOT = Path('/etc/thinksmart-voice')
RUNTIME = Path('/run/thinksmart-gateway')
REQUEST = RUNTIME / 'request.json'
OVERRIDES = ROOT / 'gateway.conf'


def atomic(path, data, mode=0o600, owner=None):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if owner is not None:
            os.fchown(fd, owner, owner)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def env_values(path):
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    return dict(line.split('=', 1) for line in lines if '=' in line and not line.lstrip().startswith('#'))


def safe_config():
    env = env_values(Path('/etc/thinksmart-voice.conf'))
    overrides = env_values(OVERRIDES)
    env.update(overrides)
    data = {}
    for key, field in VOICE_FIELDS.items():
        value = env.get(field[0], field[1])
        data[key] = float(value) if len(field) == 4 else value
    data.update(chat_provider='bailian', chat_services=['bailian'], expert_provider='', expert_configured=False)
    for name in ('chat', 'expert'):
        path = ROOT / (name + '.json')
        if path.exists():
            saved = json.loads(path.read_text())
            data[name + '_model'] = saved.get('model', '')
            data[name + '_provider'] = saved.get('provider', 'bailian' if name == 'chat' else 'openai')
            if name == 'chat':
                data['chat_services'] = [data['chat_provider']]
            if name == 'expert':
                data['expert_configured'] = bool(saved.get('api_key'))
    if data['expert_configured'] and data['expert_provider'] == 'openai' and 'openai' not in data['chat_services']:
        data['chat_services'].append('openai')
    for key in MODEL_FIELDS:
        if VOICE_FIELDS[key][0] in overrides:
            data[key] = overrides[VOICE_FIELDS[key][0]]
    data['_catalog'] = model_catalog(data)
    data['_revision'] = config_revision()
    return data


def publish_config():
    config = safe_config()
    atomic(RUNTIME / 'config-view.json', json.dumps(config).encode(), owner=1000)
    return config


def run(*args):
    subprocess.run(args, check=True, capture_output=True, timeout=35)


def ready_voice():
    path = str(RUNTIME / ('health-' + str(os.getpid()) + '.sock'))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(path)
        os.chown(path, 1000, 1000)
        os.chmod(path, 0o600)
        sock.settimeout(1)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                sock.sendto(b'{"c":"ping","seq":1}', '/run/thinksmart-voice/control.sock')
                response = json.loads(sock.recv(8192))
                if (response.get('e') == 'pong' and response.get('healthy')
                        and response.get('audio_ready') is True):
                    return response
            except (OSError, ValueError):
                pass
            time.sleep(.2)
        raise RuntimeError('语音服务没有就绪')
    finally:
        sock.close()
        if os.path.exists(path):
            os.unlink(path)


def configure(values):
    values = model_values(voice_values(values), safe_config())
    deferred = values.keys() <= MODEL_FIELDS
    wanted = dict(values)
    changes = {}
    env = env_values(OVERRIDES)
    for key, value in values.items():
        env[VOICE_FIELDS[key][0]] = str(value)
    changes[OVERRIDES] = (''.join(f'{key}={value}\n' for key, value in sorted(env.items())).encode(), 0o644)
    backups = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None for path in changes}
    try:
        for path, (content, mode) in changes.items():
            atomic(path, content, mode)
        if deferred:
            return publish_config()
        run('/usr/bin/systemctl', 'restart', SERVICES['voice'])
        health = ready_voice()
        effective = dict(health.get('configuration', {}), **health.get('models', {}))
        if any(effective.get(key) != value for key, value in wanted.items()):
            raise RuntimeError('运行进程没有加载全部新配置')
    except Exception:
        for path, old in backups.items():
            if old is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, old[0], old[1])
        if not deferred:
            run('/usr/bin/systemctl', 'restart', SERVICES['voice'])
        raise RuntimeError('配置保存失败，已恢复原配置' if deferred else '配置未能启动，已恢复原配置') from None
    return publish_config()


def main():
    ident = None
    try:
        fd = os.open(REQUEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('请求文件无效')
            content = os.read(fd, 8193)
        finally:
            os.close(fd)
        REQUEST.unlink(missing_ok=True)
        if len(content) > 8192:
            raise ValueError('请求过长')
        request = json.loads(content)
        ident = request.get('id')
        if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident):
            raise ValueError('请求编号无效')
        action = request.get('action')
        if action == 'read':
            result = publish_config()
        elif action == 'configure':
            result = configure(request.get('values'))
        elif action == 'restart' and request.get('target') in SERVICES:
            target = request['target']
            run('/usr/bin/systemctl', 'restart', SERVICES[target])
            if target == 'voice':
                ready_voice()
            run('/usr/bin/systemctl', 'is-active', '--quiet', SERVICES[target])
            result = {'target': target}
        else:
            raise ValueError('不支持的维护操作')
        response = {'id': ident, 'ok': True, 'result': result}
    except Exception as error:
        REQUEST.unlink(missing_ok=True)
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else '维护操作失败，请查看服务状态'
        response = {'id': ident, 'ok': False, 'error': message[:200]}
    atomic(RUNTIME / 'result.json', json.dumps(response, ensure_ascii=False).encode(), owner=1000)


if __name__ == '__main__':
    main()
