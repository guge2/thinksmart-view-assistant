import math
import re
from pathlib import Path

VOICE_FIELDS = {
    "kws_score": ("THINKSMART_KWS_SCORE", 1.0, 1.0, 10.0),
    "kws_threshold": ("THINKSMART_KWS_THRESHOLD", 0.25, 0.01, 0.9),
    "speech_wait_seconds": ("THINKSMART_SPEECH_WAIT_SECONDS", 8.0, 3.0, 20.0),
    "max_speech_seconds": ("THINKSMART_MAX_SPEECH_SECONDS", 25.0, 5.0, 60.0),
    "followup_seconds": ("THINKSMART_FOLLOWUP_SECONDS", 4.0, 0.0, 15.0),
    "question_followup_seconds": ("THINKSMART_QUESTION_FOLLOWUP_SECONDS", 8.0, 0.0, 30.0),
    "asr_model": ("THINKSMART_ASR_MODEL", "qwen-audio-3.1-asr-flash-message"),
    "tts_model": ("THINKSMART_TTS_MODEL", "qwen-audio-3.1-tts-flash"),
    "tts_voice": ("THINKSMART_TTS_VOICE", "longxiaocheng_v3.1"),
    "chat_model": ("THINKSMART_CHAT_MODEL", "qwen3.8-flash"),
    "chat_provider": ("THINKSMART_CHAT_PROVIDER", "bailian"),
    "expert_model": ("THINKSMART_EXPERT_MODEL", ""),
}
MODEL_FIELDS = frozenset(('chat_model', 'chat_provider', 'expert_model', 'tts_model', 'tts_voice'))
PROVIDER_NAMES = {'bailian': '阿里云百炼', 'openrouter': 'OpenRouter', 'openai': 'OpenAI'}
CHAT_MODELS = {
    'openrouter': [('google/gemini-3.8-flash', 'Google · Gemini 3.8 Flash'),
                   ('deepseek/deepseek-v4.1-flash', 'DeepSeek V4.1 Flash'),
                   ('deepseek/deepseek-v4-flash', 'DeepSeek V4 Flash'),
                   ('qwen/qwen3.8-flash', 'Qwen3.8 Flash'),
                   ('minimax/minimax-m2.7', 'MiniMax M2.7'),
                   ('moonshotai/kimi-k2.6', 'Kimi K2.6')],
    'openai': [('gpt-6.1-terra', 'GPT-6.1 Terra'), ('gpt-6.1-sol', 'GPT-6.1 Sol'), ('gpt-6-astra', 'GPT-6 Astra'),
               ('gpt-6-sol', 'GPT-6 Sol'), ('gpt-6-luna', 'GPT-6 Luna')],
    'bailian': [('qwen3.8-flash', 'Qwen3.8 Flash')],
}
# Chinese system voices, grouped by model as published in the official voice list:
# https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list
PLUS_VOICES = [('longanlingxin_v3.1', '龙安灵心 · 女声'), ('xunanchuan_v3.1', '许南川 · 男声'),
               ('longanhuan_v3.1', '龙安欢 · 女声'), ('longanfengyue_v3.1', '龙安风悦 · 女声')]
FLASH_VOICES = [('longxiaocheng_v3.1', '龙小诚 · 磁性低音'),
                ('longanwen_v3.1', '龙安温 · 知性女声'), ('longanlang_v3.1', '龙安朗 · 清爽男声'),
                ('longyingtao_v3.1', '龙应桃 · 温柔女声'), ('longanyang_v3.1', '龙安洋 · 阳光男声'),
                ('longxiaoxia_v3.1', '龙小夏 · 沉稳女声')] + PLUS_VOICES
TTS_MODELS = {'qwen-audio-3.1-tts-flash': ('Qwen Audio 3.1 TTS Flash', FLASH_VOICES),
              'qwen-audio-3.1-tts-plus': ('Qwen Audio 3.1 TTS Plus', PLUS_VOICES)}


def model_catalog(current):
    catalog = {}
    for slot in ('chat', 'expert'):
        provider = current.get(slot + '_provider', '')
        providers = current.get('chat_services', [provider]) if slot == 'chat' else [provider]
        providers = sorted(set(providers), key=lambda p: (p != 'openai', p))
        choices = [{'id': ident, 'name': name, 'provider': service,
                    'service': PROVIDER_NAMES.get(service, service),
                    'group': '日常低价' if service == 'openrouter' else 'GPT 模型' if service == 'openai' else ''}
                   for service in providers for ident, name in CHAT_MODELS.get(service, [])]
        model = current.get(slot + '_model')
        if model and not any(c['id'] == model and c['provider'] == provider for c in choices):
            choices.insert(0, {'id': model, 'name': model, 'provider': provider,
                               'service': PROVIDER_NAMES.get(provider, provider), 'group': '当前配置'})
        catalog[slot] = choices
    catalog['tts'] = [{'id': ident, 'name': name, 'provider': 'bailian', 'service': '阿里云百炼',
                       'voices': [{'id': voice, 'name': label} for voice, label in voices]}
                      for ident, (name, voices) in TTS_MODELS.items()]
    return catalog


def tts_pair(model, voice):
    if model not in TTS_MODELS or voice not in dict(TTS_MODELS[model][1]):
        raise ValueError('请选择语音合成模型支持的音色')
    return model, voice


def model_values(values, current):
    if {'chat_model', 'chat_provider'} & values.keys():
        model = values.get('chat_model', current['chat_model'])
        choices = [m for m in model_catalog(current)['chat'] if m['id'] == model]
        provider = values.get('chat_provider') or (choices[0]['provider'] if len(choices) == 1 else current['chat_provider'])
        if not any(m['provider'] == provider for m in choices):
            raise ValueError('请选择已配置服务支持的日常对话模型')
        values['chat_provider'] = provider
    for slot in ('chat', 'expert'):
        key = slot + '_model'
        if key not in values:
            continue
        if slot == 'expert' and not current.get('expert_configured'):
            raise ValueError('设备尚未配置复杂任务模型凭据')
        if values[key] not in {m['id'] for m in model_catalog(current)[slot]}:
            raise ValueError('请选择当前服务支持的对话模型')
    if {'tts_model', 'tts_voice'} & values.keys():
        tts_pair(values.get('tts_model', current['tts_model']), values.get('tts_voice', current['tts_voice']))
    return values
SERVICES = {"voice": "thinksmart-voice.service", "ui": "thinksmart-ui.service",
            "keys": "thinksmart-volume-keys.service"}


def config_revision():
    paths = ['/etc/thinksmart-voice.conf', '/etc/thinksmart-voice/gateway.conf',
             '/etc/thinksmart-voice/chat.json', '/etc/thinksmart-voice/expert.json']
    revision = []
    for path in paths:
        try:
            info = Path(path).stat()
            revision.append([info.st_mtime_ns, info.st_size])
        except FileNotFoundError:
            revision.append(None)
    return revision


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"数值必须在 {low:g} 到 {high:g} 之间")
    return value


def voice_values(values):
    if not isinstance(values, dict) or not values or set(values) - VOICE_FIELDS.keys():
        raise ValueError("语音配置字段无效")
    result = {}
    for key, value in values.items():
        field = VOICE_FIELDS[key]
        if len(field) == 4:
            result[key] = number(value, field[2], field[3])
        elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:+@-]{0,127}", value):
            raise ValueError("模型或音色名称只能包含字母、数字和常用标识符")
        else:
            result[key] = value
    return result


def preferences(values):
    allowed = {"speech_rate", "thinking", "mic", "night", "brightness", "dim_after"}
    if not isinstance(values, dict) or not values or set(values) - allowed:
        raise ValueError("设置字段无效")
    result = dict(values)
    for key in ("thinking", "mic"):
        if key in result and type(result[key]) is not bool:
            raise ValueError("开关值无效")
    if "speech_rate" in result:
        result["speech_rate"] = round(number(result["speech_rate"], 0.5, 2.0), 1)
    if "brightness" in result:
        result["brightness"] = round(number(result["brightness"], 1, 100))
    if "dim_after" in result and (type(result["dim_after"]) is not int or result["dim_after"] not in (0, 60, 300, 900, 1800)):
        raise ValueError("自动调暗时间无效")
    if "night" in result:
        night = result["night"]
        if not isinstance(night, dict) or set(night) != {"enabled", "start", "end"} or type(night["enabled"]) is not bool:
            raise ValueError("夜间模式设置无效")
        if any(type(night[k]) is not int or not 0 <= night[k] < 1440 for k in ("start", "end")):
            raise ValueError("夜间时间无效")
    return result
