#!/usr/bin/python3
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import time
from urllib.parse import urlsplit

from aiohttp import web
from schema import SERVICES, MODEL_FIELDS, preferences, voice_values, tts_pair, number, config_revision

sys.path.insert(0, '/usr/local/lib/thinksmart-ui')
from system import Backlight, Settings, read_volume, write_volume, read_wake_phrase, WAKE_PATTERN

RUNTIME = Path('/run/thinksmart-gateway')
WEB = Path(__file__).parent / 'web'
CONTROL = '/run/thinksmart-voice/control.sock'
CSRF = secrets.token_urlsafe(32)
VERSION = '0.1.0'
ALL_SERVICES = dict(SERVICES, gateway='thinksmart-gateway.service')


async def command(*args, timeout=5):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError('系统命令未完成')
    return output.decode('utf-8', 'replace')


async def voice(command_name, reply_timeout=3, **fields):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.setblocking(False)
    ident = secrets.token_hex(12)
    path = str(RUNTIME / ('ipc-' + ident + '.sock'))
    try:
        sock.bind(path)
        loop = asyncio.get_running_loop()
        request = dict(fields, c=command_name, id=ident)
        await asyncio.wait_for(loop.sock_sendto(sock, json.dumps(request).encode(), CONTROL), 2)
        response = json.loads(await asyncio.wait_for(loop.sock_recv(sock, 65536), reply_timeout))
        if command_name not in ('ping', 'snapshot'):
            if response.get('id') != ident or response.get('e') != 'ack':
                raise RuntimeError('设备没有确认操作')
            if not response.get('ok'):
                raise ValueError(response.get('status', '设备没有接受操作'))
        return response
    finally:
        sock.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


class Gateway:
    def __init__(self):
        self.maintenance = asyncio.Lock()
        self.busy = None
        self.cache = {}
        self.cache_at = 0.0
        self.status_lock = asyncio.Lock()
        self.services = {}
        self.services_at = 0.0
        self.volume = None
        self.volume_at = 0.0
        self.backlight = Backlight()

    def current_preferences(self):
        prefs = Settings().data
        if prefs.get('brightness') is None:
            prefs['brightness'] = self.backlight.read_percent() or 80
        return prefs

    async def root_action(self, action, **fields):
        async with self.maintenance:
            if (RUNTIME / 'request.json').exists():
                raise ValueError('上一项维护请求仍在处理中，请稍后再试')
            ident = secrets.token_hex(16)
            fd, temporary = tempfile.mkstemp(prefix='.request-', dir=RUNTIME)
            try:
                with os.fdopen(fd, 'w') as target:
                    json.dump(dict(fields, id=ident, action=action), target)
                self.busy = action
                os.replace(temporary, RUNTIME / 'request.json')
                deadline = time.monotonic() + 80
                while time.monotonic() < deadline:
                    try:
                        response = json.loads((RUNTIME / 'result.json').read_text())
                        if response.get('id') == ident:
                            if not response.get('ok'):
                                raise ValueError(response.get('error', '维护操作失败'))
                            self.cache_at = self.services_at = 0.0
                            return response.get('result', {})
                    except (FileNotFoundError, json.JSONDecodeError):
                        pass
                    await asyncio.sleep(.15)
                raise ValueError('尚未确认维护结果，操作可能仍在进行，请查看服务状态')
            finally:
                self.busy = None
                if os.path.exists(temporary):
                    os.unlink(temporary)

    async def service_status(self):
        if time.monotonic() - self.services_at < 5:
            return self.services
        output = await command('systemctl', 'show', *ALL_SERVICES.values(), '-p', 'Id', '-p', 'ActiveState',
                               '-p', 'SubState', '-p', 'MemoryCurrent', '-p', 'NRestarts', '-p', 'MainPID')
        found = {}
        for block in output.strip().split('\n\n'):
            item = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
            key = next((k for k, v in ALL_SERVICES.items() if v == item.get('Id')), None)
            if key:
                found[key] = {'state': item.get('ActiveState', 'unknown'), 'substate': item.get('SubState'),
                              'memory': int(item['MemoryCurrent']) if item.get('MemoryCurrent', '').isdigit() else None,
                              'restarts': int(item.get('NRestarts') or 0), 'pid': int(item.get('MainPID') or 0)}
        self.services, self.services_at = found, time.monotonic()
        return found

    async def status(self):
        async with self.status_lock:
            if time.monotonic() - self.cache_at < 1.5:
                return dict(self.cache, maintenance=self.busy)
            health, snapshot = None, {}
            try:
                health = await voice('ping', seq=1)
                snapshot = await voice('snapshot')
            except (OSError, ValueError, asyncio.TimeoutError, RuntimeError):
                pass
            try:
                services = await self.service_status()
            except (OSError, RuntimeError, asyncio.TimeoutError):
                services = self.services
            if time.monotonic() - self.volume_at > 10:
                try:
                    self.volume = (await asyncio.to_thread(read_volume))[0]
                except Exception:
                    self.volume = None
                self.volume_at = time.monotonic()
            self.cache = {'version': VERSION, 'time': time.time(), 'health': health, 'snapshot': snapshot,
                          'services': services, 'volume': self.volume, 'wake_word': read_wake_phrase(),
                          'preferences': self.current_preferences(), 'load': list(os.getloadavg()),
                          'maintenance': self.busy}
            try:
                saved = json.loads((RUNTIME / 'config-view.json').read_text())
                wanted = {key: saved[key] for key in MODEL_FIELDS if key in saved
                          and (key != 'expert_model' or saved.get('expert_configured'))}
                pending = [key for key, value in wanted.items() if value != (health or {}).get('models', {}).get(key)]
                self.cache['model_update'] = {'status': 'offline' if not health else 'pending' if pending else 'applied',
                                              'pending': pending, 'desired': wanted}
                self.cache['catalog'] = saved.get('_catalog', {})
            except (OSError, ValueError, KeyError):
                self.cache['model_update'] = {'status': 'unknown'}
            self.cache_at = time.monotonic()
            return self.cache


def origin_allowed(request):
    host = urlsplit('http://' + request.host).hostname
    if host not in ('127.0.0.1', 'localhost', '::1'):
        return False
    origin = request.headers.get('Origin')
    return origin is None or origin == 'http://' + request.host


@web.middleware
async def guard(request, handler):
    try:
        if not origin_allowed(request):
            raise web.HTTPForbidden(text='不允许从这个来源访问')
        if request.method not in ('GET', 'HEAD'):
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), CSRF):
                raise web.HTTPForbidden(text='页面会话已失效，请刷新后重试')
            if request.content_type != 'application/json':
                raise web.HTTPBadRequest(text='请求必须使用JSON')
        response = await handler(request)
    except web.HTTPException as error:
        response = web.json_response({'ok': False, 'error': error.text}, status=error.status)
    except (ValueError, TypeError) as error:
        response = web.json_response({'ok': False, 'error': str(error)}, status=400)
    except (OSError, RuntimeError, asyncio.TimeoutError):
        response = web.json_response({'ok': False, 'error': '设备暂时没有响应，请查看服务状态'}, status=503)
    except Exception:
        logging.exception('网关请求处理失败')
        response = web.json_response({'ok': False, 'error': '操作未完成，请稍后再试'}, status=500)
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                             'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                             'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'"})
    return response


def make_app():
    gateway = Gateway()
    app = web.Application(middlewares=[guard], client_max_size=8192)

    async def index(request):
        name = request.match_info.get('name', 'index.html')
        if name not in ('index.html', 'app.js', 'style.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(WEB / name)

    async def status(_request):
        return web.json_response(await gateway.status())

    async def settings(_request):
        try:
            saved = json.loads((RUNTIME / 'config-view.json').read_text())
            if saved.get('_revision') != config_revision():
                saved = await gateway.root_action('read')
        except (OSError, ValueError):
            saved = await gateway.root_action('read')
        return web.json_response({'preferences': gateway.current_preferences(), 'voice': saved})

    async def save_preferences(request):
        values = preferences(await request.json())
        settings = Settings()
        settings.update(strict=True, **values)
        applied = False
        try:
            await voice('prefs', rate=settings['speech_rate'], think=settings['thinking'], night=settings['night'])
            await voice('mic', on=int(settings['mic']))
            applied = True
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
            pass
        gateway.cache_at = 0
        return web.json_response({'ok': True, 'status': 'applied' if applied else 'saved',
                                  'message': '已保存，语音设置已生效；屏幕会自动同步' if applied else '已保存，语音服务恢复后生效',
                                  'preferences': settings.data})

    async def configure(request):
        values = voice_values(await request.json())
        result = await gateway.root_action('configure', values=values)
        deferred = values.keys() <= MODEL_FIELDS
        return web.json_response({'ok': True, 'status': 'pending' if deferred else 'applied',
                                  'message': '已保存，从下一轮对话开始使用，当前播报继续' if deferred else '配置已应用，语音服务已就绪',
                                  'voice': result})

    async def preview(request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {'model', 'voice'} or not all(isinstance(v, str) for v in body.values()):
            raise ValueError('试听请求无效')
        model, selected_voice = tts_pair(body['model'], body['voice'])
        response = await voice('preview', model=model, voice=selected_voice, reply_timeout=40)
        path = Path('/run/thinksmart-voice') / ('preview-' + response['id'] + '.wav')
        return web.Response(body=path.read_bytes(), content_type='audio/wav')

    async def restart(request):
        body = await request.json()
        if not isinstance(body, dict) or body.get('target') not in SERVICES:
            raise ValueError('不支持重启这个服务')
        await gateway.root_action('restart', target=body['target'])
        return web.json_response({'ok': True, 'message': '服务已重新启动'})

    async def action(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('操作无效')
        name = body.get('action')
        if name in ('talk', 'stop', 'alarm_stop', 'alarm_snooze'):
            await voice(name)
        elif name in ('toggle', 'next', 'prev', 'music_stop'):
            await voice('music', a='stop' if name == 'music_stop' else name)
        elif name == 'sleep_timer':
            minutes = round(number(body.get('minutes', 0), 0, 480))
            await voice('music', a='sleep', minutes=minutes)
        elif name == 'timer':
            minutes = round(number(body.get('minutes', 5), 1, 480))
            await voice('alarm_cmd', a='timer', minutes=minutes)
        elif name == 'alarm_cancel':
            ident = str(body.get('id') or '').strip()
            if not re.fullmatch(r'[a-f0-9]{4,12}', ident):
                raise ValueError('编号无效')
            await voice('alarm_cmd', a='cancel', id=ident)
        elif name == 'volume':
            value = round(number(body.get('value'), 0, 100))
            await asyncio.to_thread(write_volume, value)
            actual, _ = await asyncio.to_thread(read_volume)
            if actual is None or abs(actual - value) > 1:
                raise RuntimeError('音量没有生效')
            gateway.volume, gateway.volume_at = actual, time.monotonic()
        else:
            raise ValueError('不支持的操作')
        gateway.cache_at = 0
        return web.json_response({'ok': True, 'message': '音量已生效' if name == 'volume' else '设备已接收操作'})

    async def wake_word(request):
        body = await request.json()
        phrase = body.get('phrase') if isinstance(body, dict) else None
        if not isinstance(phrase, str) or not WAKE_PATTERN.fullmatch(phrase):
            raise ValueError('唤醒词需要2到8个汉字')
        await gateway.root_action('wake_word', phrase=phrase)
        return web.json_response({'ok': True, 'message': '新唤醒词已生效'})

    async def logs(request):
        target = request.query.get('target', 'voice')
        if target not in ALL_SERVICES:
            raise ValueError('日志来源无效')
        output = await command('journalctl', '-u', ALL_SERVICES[target], '--since', '15 minutes ago',
                               '-n', '70', '-o', 'json', '--no-pager')
        rows = []
        for line in output.splitlines():
            try:
                record = json.loads(line)
                message = str(record.get('MESSAGE', ''))
                if re.search(r'api[_-]?key|authorization|bearer|password|secret|access_token', message, re.I):
                    message = '[已隐藏包含敏感字段的日志]'
                message = re.sub(r'sk-[A-Za-z0-9_-]+', '[已隐藏密钥]', message)
                message = re.sub(r'(用户：|助手：|已听到：).*', r'\1[内容已省略]', message)
                rows.append({'time': int(record['__REALTIME_TIMESTAMP']) / 1e6, 'message': message[:500],
                             'level': 'error' if re.search(r'ERROR|CRITICAL|Traceback', message) else 'warning' if 'WARNING' in message else 'info'})
            except (ValueError, KeyError, TypeError):
                continue
        return web.json_response({'rows': rows})

    async def session(_request):
        return web.json_response({'csrf': CSRF})

    app.add_routes([web.get('/', index), web.get('/{name:app.js|style.css}', index),
                    web.get('/api/session', session), web.get('/api/status', status),
                    web.get('/api/settings', settings), web.get('/api/logs', logs),
                    web.post('/api/preferences', save_preferences), web.post('/api/configure', configure),
                    web.post('/api/preview', preview),
                    web.post('/api/restart', restart), web.post('/api/action', action),
                    web.post('/api/wake-word', wake_word)])
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    web.run_app(make_app(), host='127.0.0.1', port=8787, access_log=None, print=None)
