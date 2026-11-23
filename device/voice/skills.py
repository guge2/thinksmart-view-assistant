"""Device skills: each entry holds one capability's tool definition, the phrases handled without
the model, and its offline commands.

A skill runs as the provider method of the same name (see Skills), which takes the tool arguments
and returns a Result. Run this file to print the offline keyword list for text2token."""

import re
from collections import namedtuple

Result = namedtuple("Result", "message ok started", defaults=(False,))
# parameters None keeps a skill local: phrases reach it, the model does not.
# kind "action" changes the device and its result is spoken as is, unless retry sends a failure
# back to the model; "info" results go back to the model; None steers the conversation, records
# nothing, and its acknowledgement is dropped once the model has spoken. kind may be a function
# of the arguments. force: phrases that make the model call this tool before anything else.
Skill = namedtuple("Skill", "name summary description parameters kind retry force phrases offline",
                   defaults=("", None, "action", False, None, (), ()))
# What phrase parsers see: the utterance without the wake phrase, that text without punctuation,
# the same without polite openers (empty when too long for a command), and the device state.
Said = namedtuple("Said", "text clean command music_active wake_phrase offline recipe_active", defaults=(False,))

PUNCT = re.compile(r"[\s，。！？、；：,.!?;:~～…“”\"'‘’]+")
_CLEAN = re.compile(r"[\s，。！？、,.!?~～…]+")
_REPEATED_COMMAND = re.compile(r"(.{2,16}?)\1{1,3}")
_POLITE = re.compile(r"^(?:请你?|麻烦你?|帮我|给我|你|再)+")
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
MODES = ("shuffle", "order", "repeat_one")
QUICK_MUSIC_ACTIONS = frozenset(("pause", "resume", "next", "previous", "stop"))
OFFLINE_HINT = "离线时请说音量调大、音量调小、暂停播放、继续播放、上一首、下一首或现在几点。"


def parse_cn_number(text):
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "半":
        return 0.5
    if text == "十":
        return 10
    if text in ("一百", "百"):
        return 100
    if "十" in text:
        left, _, right = text.partition("十")
        tens = 1 if not left else _CN_DIGITS.get(left)
        ones = 0 if not right else _CN_DIGITS.get(right)
        if tens is not None and ones is not None:
            return tens * 10 + ones
        return None
    return _CN_DIGITS.get(text)


def _fixed(*rows, music_only=False, recipe_only=False):
    """Whole-command phrases with fixed arguments; music_only ones apply only while music is on,
    recipe_only ones only while a recipe is being cooked."""
    table = [(re.compile(pattern), args) for pattern, args in rows]

    def parse(said):
        if music_only and not said.music_active or recipe_only and not said.recipe_active:
            return None
        for pattern, args in table:
            if pattern.match(said.command):
                return dict(args)
        return None
    return parse


_FAREWELL = re.compile(r"退下|没事|不用|再见|拜拜|休息|去休息|别说|不聊|结束对话|就这样")
_SLEEP = re.compile(r"关机|关屏|关闭屏幕|关掉屏幕|息屏|熄屏|睡觉|去睡觉|晚安")
# Sleep words that also stop music; screen-only words leave playback running.
_POWER_OFF = re.compile(r"关机|睡觉|晚安")


def _farewell(said):
    normalized = said.clean.replace(said.wake_phrase, "") if said.wake_phrase else said.clean
    normalized = re.sub(r"^(?:好了|好的|行了|那|嗯|你|请)+", "", normalized)
    normalized = re.sub(r"(?:吧|啦|了|呀|哦|啊)+$", "", re.sub(r"(?:谢谢你?|谢了)$", "", normalized))
    if _SLEEP.fullmatch(normalized):
        return {"screen_off": True, "stop_music": bool(_POWER_OFF.search(said.text))}
    if _FAREWELL.fullmatch(normalized):
        return {"screen_off": False, "stop_music": False}
    return None


_RECIPE_NAV = _fixed(
    (r"^(?:下一步|下一个步骤|然后呢|接下来呢?|接着呢|然后怎么做|接下来怎么做|做好了|好了下一步)[吧呀啊呢了]?$",
     {"action": "next"}),
    (r"^(?:上一步|上一个步骤|回到上一步|退回上一步)[吧呀啊呢]?$", {"action": "previous"}),
    # The polite-opener filter has already dropped a leading 再 or 帮我.
    (r"^(?:再?说一遍|再?说一次|重复一遍|重复一下|这一步是什么|这一步怎么做|刚才说什么|没听清)[吧呀啊呢]?$",
     {"action": "repeat"}),
    (r"^(?:要|需要)?(?:准备)?(?:哪些|什么)?(?:食材|材料|配料|原料)(?:有哪些|是什么|都有什么|有什么)?[吧呀啊呢]?$|"
     r"^(?:要|需要)准备(?:什么|哪些)(?:东西)?[吧呀啊呢]?$", {"action": "ingredients"}),
    (r"^(?:开始计时|开始倒计时|计时开始|计时|这一步计时)[吧呀啊]?$", {"action": "timer"}),
    (r"^(?:退出菜谱|关闭菜谱|结束菜谱|菜谱结束|不做了|做完了)[吧呀啊了]?$", {"action": "stop"}),
    recipe_only=True,
)
_RECIPE_STEP = re.compile(r"^(?:跳到|回到|去|看)?第(\d{1,2}|[一二两三四五六七八九十]+)步[吧呀啊]?$")


def _recipe_step(said):
    match = _RECIPE_STEP.match(said.command) if said.recipe_active else None
    step = parse_cn_number(match.group(1)) if match else None
    return {"action": "goto", "step": int(step)} if step else None


# Asking to be taught a dish starts recipe mode; asking only how it is made is left to the model.
_COOK = re.compile(r"(?:教|带)我(?:做|煮|炒|烤|炖|蒸|煎|包)|跟着你做|一步一步.{0,4}(?:做|教)")
_PLAY = re.compile(r"^(?:播放|我想听听?|我要听|想听|来一首|来首|放一首|放首|唱一首|唱首|搜索)(.*)$")


def _play(said):
    match = _PLAY.match(said.command)
    if not match:
        return None
    query = re.sub(r"^(?:一首|一下|首|点)", "", match.group(1))
    return {"query": re.sub(r"(?:吧|呗|好吗|可以吗|好不好)$", "", query)}


_SLEEP_TIMER = re.compile(
    r"^(?:定时)?(\d+|[一二两三四五六七八九十半]+)(?:个)?(分钟?|小时|钟头)后?"
    r"(?:停止播放|关掉?音乐|关闭音乐|停播|停止音乐|别放了)$"
)


def _sleep_timer(said):
    match = _SLEEP_TIMER.match(said.command) if said.music_active else None
    amount = parse_cn_number(match.group(1)) if match else None
    if amount is None:
        return None
    minutes = round(amount * (60 if match.group(2) in ("小时", "钟头") else 1))
    return {"action": "sleep", "minutes": minutes} if 1 <= minutes <= 480 else None


_MUSIC_CONTROL = _fixed(
    (r"^(?:停止|停止播放|停止音乐|关闭音乐|关掉音乐|把音乐关掉|关闭播放|别放了|不听了|不要放了)[吧呀啊]?$", {"action": "stop"}),
    (r"^(?:暂停|暂停播放|暂停音乐|暂停一下|停一下|先停一下|停)[吧呀啊]?$", {"action": "pause"}),
    (r"^(?:继续|继续播放|继续放|接着放|接着播放|恢复播放|继续音乐)[吧呀啊]?$", {"action": "resume"}),
    (r"^(?:下一首|下一曲|下一首歌|换一首|换首歌|换一首歌|切歌|换歌|换一个|下首)[吧呀啊]?$", {"action": "next"}),
    (r"^(?:上一首|上一曲|上一首歌|上首)[吧呀啊]?$", {"action": "previous"}),
    music_only=True,
)
_MUSIC_MODE = _fixed(
    (r"^(?:取消|关闭)(?:伴睡|定时停播|定时关闭|睡眠定时|音乐定时)$", {"action": "cancel_sleep"}),
    (r"^(?:切换到?|切到|改成|换成|改为|设为|开启)?(?:随机播放|乱序播放|随机模式|乱序模式|打乱顺序|随机放歌?|随机听歌?)(?:模式)?$",
     {"action": "shuffle"}),
    (r"^(?:切换到?|切到|改成|换成|改为|设为|开启)?(?:顺序播放|按顺序播放|顺序模式|列表循环|按顺序放|按顺序听)(?:模式)?$",
     {"action": "order"}),
    (r"^(?:切换到?|切到|改成|换成|改为|设为|开启)?(?:单曲循环|单曲重复|(?:循环|重复|一直)(?:播放|放)?这首歌?)(?:模式)?$",
     {"action": "repeat_one"}),
)
_VOL_SET = re.compile(
    r"^(?:把)?(?:设备)?(?:播放)?(?:声音|音量)(?:大小)?(?:调整?到|调整?至|调整?为|调到|调至|调为|设为|设置?为?|设置?到|调成|开到)?(?:百分之)?"
    r"(\d{1,3}|一百|[一二两三四五六七八九]?十[一二三四五六七八九]?|[零一二两三四五六七八九])(?:%|格)?$"
)


def _volume_level(said):
    match = _VOL_SET.match(said.command)
    level = parse_cn_number(match.group(1)) if match else None
    return {"level": int(level)} if level is not None and 0 <= level <= 100 else None


_VOLUME = _fixed(
    (r"^(?:把)?(?:声音|音量)(?:调|开)?(?:大|高|响)(?:一)?[点些]?$|"
     r"^大声(?:一)?点$|^大[点些]声$|^(?:声音|音量)?再?大(?:一)?点$|"
     r"^(?:你说话)?(?:声音|音量)(?:太|好|有点)(?:小|低|轻)了?[哦呀啊呢吧]*$|^太小声了?[哦呀啊呢吧]*$", {"change": 10}),
    (r"^(?:把)?(?:声音|音量)(?:调|开)?(?:小|低|轻)(?:一)?[点些]?$|"
     r"^小声(?:一)?点$|^小[点些]声$|^轻(?:一)?点声$|^(?:声音|音量)?再?小(?:一)?点$|"
     r"^(?:你说话)?(?:声音|音量)(?:太|好|有点)(?:大|高|响|吵)了?[哦呀啊呢吧]*$|^太大声了?[哦呀啊呢吧]*$|^太吵了?[哦呀啊呢吧]*$",
     {"change": -10}),
    (r"^(?:静音|开启静音|把声音关掉|关闭声音)$", {"mute": "mute"}),
    (r"^(?:取消静音|解除静音|恢复声音|打开声音)$", {"mute": "unmute"}),
)
_VOLUME_STATUS = _fixed(
    (r"^(?:现在|当前|目前)?(?:设备)?(?:的)?(?:声音|音量)(?:是)?(?:多少|多大|几格)[呀啊呢吗吧]*$", {"item": "volume"}),
)
_SONG_STATUS = _fixed(
    (r"^(?:这首歌?叫什么(?:名字)?|这是什么歌|现在(?:放|播|播放)的是什么歌|当前(?:放|播|播放)的是什么歌|什么歌|谁唱的|这首歌是谁唱的)$",
     {"item": "song"}),
    music_only=True,
)
_TIME_QUERY = re.compile(
    r"^(?:请?问|告诉我|帮我看一下)?(?:现在|今天|当前)?(?:是)?"
    r"(?:几点(?:钟|几分)?了?|什么时间|(?P<date>几月几[号日]|几号|星期几|礼拜几|周几|什么日子))+[呀啊呢吗吧么]*$"
)


def _time(said):
    query = _TIME_QUERY.match(said.clean)
    if query or (said.offline and any(word in said.text for word in ("几点", "几号", "星期"))):
        return {"clock_only": bool(query and not query.group("date"))}
    return None


_DISMISS = ("停止闹钟", "关闭闹钟", "停止提醒", "关掉闹钟", "别响了")
_SNOOZE = re.compile(
    r"^(?:贪睡|稍后提醒|等会儿?再响|过会儿?再响|"
    r"再睡(?:(\d+|[一二两三四五六七八九十]+)(?:分钟?))?|"
    r"(\d+|[一二两三四五六七八九十]+)(?:分钟?)后再(?:响|提醒|叫我))$"
)
_TIMER = re.compile(
    r"^(?:请你?|帮我|给我)?(?:定个?|设个?|设置(?:一个)?|来个?|开启(?:一个)?)?"
    r"(\d+|[一二两三四五六七八九十半]+)(?:个)?(秒钟?|分钟?|小时|钟头)(?:的)?(?:倒计时|计时器?)$"
)


def _dismiss(said):
    return {"action": "dismiss"} if said.clean in _DISMISS else None


def _snooze(said):
    match = _SNOOZE.match(said.clean)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    minutes = int(parse_cn_number(raw) or 5) if raw else 5
    return {"action": "snooze", "minutes": max(1, min(60, minutes or 5))}


def _timer(said):
    match = _TIMER.match(said.clean)
    amount = parse_cn_number(match.group(1)) if match else None
    if amount is None:
        return None
    unit = match.group(2)
    if unit.startswith("秒"):
        return {"action": "create", "kind": "timer", "seconds": max(1, round(amount))}
    if unit.startswith("分"):
        return {"action": "create", "kind": "timer", "minutes": int(amount), "seconds": round((amount - int(amount)) * 60)}
    total = max(1, round(amount * 60))
    return {"action": "create", "kind": "timer", "hours": total // 60, "minutes": total % 60}


# Without thinking the model often answers weather from web search or memory, so these turns force the tool.
_WEATHER = re.compile(
    r"天气|气温|天气预报|下雪|降雨|带伞|刮风|穿什么"
    r"|(?:今[天日]|明[天日]|后天|外面|室外|出门|[早晚]上|这[里边]|本地|当地).{0,6}(?:几度|多少度|温度|下雨|冷不冷|热不热)"
    r"|^(?:现在|今天|明天|外面)?(?:多少度|几度|冷不冷|热不热)[？?吗呢吧呀啊]*$"
)

_MEMORY = re.compile(
    r"^(?!.*(?:提醒|闹钟|叫醒|叫我|计时)).*"
    r"(?:记住|记下来|记一下|别忘了|不要忘了|忘掉|忘了吧|别记|不用记|你都记得|你记得我什么|你还记得什么)"
)
_BRIEFING = re.compile(r"(?:早安|早间|今日|每日)简报|今天(?:有什么|有啥|的)安排|早上好|早安")

SKILLS = (
    Skill(
        "manage_recipe", "厨房菜谱模式：逐步显示和播报做菜步骤，需要等待的步骤可计时",
        "用户想做一道菜、让你教他做菜或问某道菜具体怎么做时，调用start进入菜谱模式：屏幕逐步显示，用户说“下一步”翻页。"
        "start时把菜谱整理成ingredients（每项带用量，如“五花肉 500克”）和steps（不超过12步，每步一句口语化的动作，"
        "需要等待的步骤在minutes填等待分钟数，如“小火炖40分钟”填40，其余填0；设备不会自动计时，用户说开始计时才计时），按家常做法给出。"
        "做菜过程中用户说下一步、上一步、再说一遍、第几步、要准备什么、开始计时、不做了时调用对应action。"
        "工具返回的内容会直接播报，不要再复述步骤。",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "next", "previous", "repeat", "goto", "ingredients", "timer", "stop"],
                       "description": "start开始新菜谱；next下一步；previous上一步；repeat重说当前步骤；goto跳到第step步；"
                                      "ingredients列出食材；timer为当前步骤开始计时；stop退出菜谱"},
            "title": {"type": "string", "description": "start时的菜名，如“红烧肉”"},
            "ingredients": {"type": "array", "items": {"type": "string"}, "description": "start时的食材和用量"},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string", "description": "这一步做什么，一句话"},
                "minutes": {"type": "integer", "description": "这一步需要等待的分钟数，不需要等待填0"},
            }, "required": ["text", "minutes"]}, "description": "start时的操作步骤"},
            "step": {"type": "integer", "description": "goto时跳到的步骤序号，从1开始"},
        }, "required": ["action"]},
        retry=True, force=_COOK, phrases=(_RECIPE_NAV, _recipe_step),
        offline=("下一步", "上一步", "再说一遍"),
    ),
    Skill(
        "end_conversation", "结束对话，可同时关屏或停止音乐",
        "用户想结束对话时调用，例如让你退下、说没事了、不用了、你去忙吧。调用后不再继续聆听。你无法关闭设备电源。",
        {"type": "object", "properties": {
            "screen_off": {"type": "boolean", "description": "用户要求关机、关屏或睡觉时为true"},
            "stop_music": {"type": "boolean", "description": "用户要求关机、睡觉或说晚安时为true，同时停止播放音乐；只关屏时为false"},
        }},
        kind=None, phrases=(_farewell,),
    ),
    Skill(
        "control_device", "调节屏幕亮度、播报语速，开关深度思考，修改默认天气城市，切换触屏页面或息屏",
        "调节或切换设备本地设置与触屏界面：屏幕亮度、播报语速、深度思考模式、"
        "默认天气城市、切换触屏页面（主页/天气/闹钟/设置/音乐）或息屏。"
        "当用户要求修改这些设置，或抱怨屏幕太亮/太暗、说话太快/太慢时直接调用。",
        {"type": "object", "properties": {
            "brightness": {"type": "integer", "description": "目标屏幕亮度百分比1到100；不设具体数值时填-1"},
            "brightness_change": {"type": "integer", "description": "相对调节屏幕亮度百分比，调亮为正（如20），调暗为负（如-20）；不用时填0"},
            "speech_rate": {"type": "number", "description": "目标播报语速倍率0.5到2.0（默认1.5）；不用时填0"},
            "speech_rate_change": {"type": "number", "description": "相对调节播报语速，加快为正（如0.2），放慢为负（如-0.2）；不用时填0"},
            "thinking_mode": {"type": "string", "enum": ["", "on", "off"], "description": "开启深度思考填on，关闭填off，不改填空字符串"},
            "weather_city": {"type": "string", "description": "修改设备默认天气城市名称；不改填空字符串"},
            "screen": {
                "type": "string",
                "enum": ["", "home", "weather", "alarms", "settings", "music", "off"],
                "description": "切换触屏页面：home主页、weather天气页、alarms闹钟页、settings设置页、music音乐页、off息屏；不切换填空字符串",
            },
        }},
    ),
    Skill(
        "play_music", "点歌、搜歌手、播放收藏或推荐",
        "搜索并播放歌曲。用户想听歌、点歌、听某位歌手或某种风格的音乐时调用。",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词，如“周杰伦 晴天”“陈奕迅”“轻音乐”；想听推荐时留空，想听收藏时填“我喜欢”"},
        }, "required": ["query"]},
        phrases=(_play,),
    ),
    Skill(
        "control_music", "暂停、继续、切歌、单曲循环、随机或顺序播放、伴睡定时停播",
        "控制当前的音乐播放，或设置伴睡定时停播。",
        {"type": "object", "properties": {
            "action": {
                "type": "string",
                "enum": ["pause", "resume", "next", "previous", "stop", "sleep", "cancel_sleep", *MODES],
                "description": "暂停、继续、下一首、上一首、停止、伴睡定时停播(sleep)、取消定时停播(cancel_sleep)、切换为随机播放、顺序播放、单曲循环",
            },
            "minutes": {"type": "integer", "minimum": 1, "maximum": 480, "description": "action为sleep时的定时分钟数，如30表示30分钟后停止播放"},
        }, "required": ["action"]},
        phrases=(_sleep_timer, _MUSIC_CONTROL, _MUSIC_MODE),
        offline=("暂停播放", "继续播放", "停止播放", "下一首歌@下一首", "上一首歌@上一首"),
    ),
    Skill(
        "set_volume", "设音量百分比、调大调小、静音或取消静音",
        "调节设备的播放音量或静音状态。用户说声音太小、太大、太吵或指定音量百分比时直接调用。",
        {"type": "object", "properties": {
            "level": {"type": "integer", "description": "目标音量百分比（0 到 100）；使用相对调整或仅切换静音时传 -1"},
            "change": {"type": "integer", "description": "相对音量增减（如 10 或 -10）；指定了目标音量 level 时传 0"},
            "mute": {"type": "string", "enum": ["", "mute", "unmute"], "description": "静音传 mute，取消静音传 unmute；正常调节音量大小时传空字符串"},
        }},
        phrases=(_volume_level, _VOLUME),
        offline=("音量调大", "音量调小", "声音大一点", "声音小一点"),
    ),
    Skill(
        "get_current_time", "当前时间、日期和星期",
        "查询设备所在时区的当前日期、星期和时间。用户询问现在几点或今天日期时调用。",
        {"type": "object", "properties": {
            "clock_only": {"type": "boolean", "description": "用户只问几点时为true，只返回钟点"},
        }},
        kind="info", phrases=(_time,),
        offline=("现在几点了", "现在几点钟@现在几点了", "今天几号", "今天星期几"),
    ),
    Skill(
        "daily_briefing", "今日简报：日期、天气和今天剩下的闹钟与提醒",
        "汇总今天的日期、本地天气和今天剩下的闹钟与提醒。用于早安问候、闹钟关掉后的早间播报，或用户问今天有什么安排。"
        "拿到结果后用三四句自然口语播报：先问好，再说天气并给出穿衣或带伞建议，最后说今天的安排；没有安排就不提。",
        {"type": "object", "properties": {}},
        kind="info", force=_BRIEFING,
    ),
    Skill(
        "manage_memory", "长期记住、查看或忘掉用户的称呼、家人、喜好等",
        "长期记住用户明确要求你记住的事，或查看、修改、忘掉已记住的内容；已记住的内容附在【设备实时状态】中。"
        "用户说“记住…”“别忘了…”“忘掉…”“你都记得我什么”时调用；用户只是闲聊提到时不要擅自记录。"
        "text用用户自己的口吻简洁记录，如“我女儿叫小米”“我不吃香菜”；与已有记忆冲突时用update覆盖旧编号。"
        "不要记录密码、证件号、银行卡号等敏感信息。",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "update", "forget", "clear", "list"],
                       "description": "新增add、覆盖update、忘掉一条forget、全部忘掉clear（仅当用户明确要求全部忘掉）、查看list"},
            "text": {"type": "string", "description": "add或update时要记住的内容，用用户口吻，最多80字"},
            "id": {"type": "string", "description": "update或forget时的记忆编号"},
        }, "required": ["action"]},
        kind=lambda args: "info" if args.get("action") == "list" else "action", retry=True, force=_MEMORY,
    ),
    Skill(
        "get_weather", "实时天气与三天预报",
        "查询实时天气或未来两天天气（含气温、体感、最高最低温、湿度、风速）。用户询问天气、气温、穿衣、带伞时必须调用，不能自行编造。",
        {"type": "object", "properties": {
            "city": {"type": "string", "description": "用户明确指定的城市；未指定则留空，使用屏幕设置的默认城市"},
            "day": {"type": "integer", "description": "今天为0，明天为1，后天为2，未来三天全部预报为3"},
        }},
        kind="info", force=_WEATHER,
    ),
    Skill(
        "manage_alarm", "创建、查看、取消闹钟、倒计时和提醒，贪睡，停止提示音",
        "设置、查看、取消闹钟、倒计时和提醒，或停止、延后正在响的提示音。设置成功前必须调用此工具，不得假装已设置。"
        "设置闹钟时传钟点time和相对天数days_from_today，由设备计算日期，不要自行计算完整日期；"
        "多久以后传hours/minutes/seconds；重复传repeat规则。"
        "取消时根据描述匹配编号，不明确时先询问；编号仅供传参，不要读给用户。",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "cancel", "dismiss", "snooze"]},
            "kind": {"type": "string", "enum": ["alarm", "timer", "reminder"]},
            "time": {"type": "string", "description": "当地钟点 HH:MM 或 HH:MM:SS，如早上七点传07:00。未指定日期时由设备选择下一次该钟点。"},
            "days_from_today": {"type": "integer", "minimum": 0, "maximum": 366, "description": "今天0、明天1、后天2；由设备计算具体日期，不要自行换算。"},
            "date": {"type": "string", "description": "仅当用户明确指定日历日期时传 YYYY-MM-DD；不能与days_from_today同时使用。"},
            "hours": {"type": "integer", "minimum": 0, "description": "多久以后：小时部分，可与minutes和seconds相加。"},
            "minutes": {"type": "integer", "minimum": 0, "description": "多久以后：分钟部分，可与hours和seconds相加；贪睡时为延后分钟数。"},
            "repeat": {"type": "string", "enum": ["once", "daily", "workdays", "weekly", "monthly"], "description": "闹钟重复规则；weekly同时传weekdays，monthly同时传month_day。"},
            "month_day": {"type": "integer", "minimum": 0, "maximum": 31, "description": "仅当repeat为monthly时传1到31号；其他情况填0。"},
            "seconds": {"type": "integer", "minimum": 0, "description": "多久以后：秒部分。相对时长总计1秒至7天；不能与钟点、日期或重复规则混用。"},
            "label": {"type": "string", "description": "提醒内容或闹钟名称，最多60字。"},
            "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}, "description": "重复星期，0代表周一，6代表周日；空或省略表示一次。仅闹钟可重复。"},
            "id": {"type": "string", "description": "取消时指定列表中的编号；多项时不得猜测。"},
        }, "required": ["action"]},
        kind=lambda args: "info" if args.get("action") == "list" else "action", retry=True,
        phrases=(_dismiss, _snooze, _timer),
        offline=("停止闹钟", "关闭闹钟@停止闹钟", "停止提醒"),
    ),
    Skill(
        "playback_status", "", kind="info", phrases=(_VOLUME_STATUS, _SONG_STATUS),
    ),
)
_BY_NAME = {skill.name: skill for skill in SKILLS}


def tools():
    return [{"type": "function", "function": {
        "name": skill.name, "description": skill.description, "parameters": skill.parameters,
    }} for skill in SKILLS if skill.parameters is not None]


def catalogue():
    return "；".join(f"{skill.name}：{skill.summary}" for skill in SKILLS if skill.parameters is not None)


def forced(text):
    """The tool a turn must call first, if its words demand one."""
    return next((skill.name for skill in SKILLS if skill.force and skill.force.search(text or "")), None)


def match(text, music_active, wake_phrase="", offline=False, recipe_active=False):
    """Returns (skill name, arguments) for a phrase handled without the model, else None."""
    text = text or ""
    local = _match(text, music_active, wake_phrase, offline, recipe_active)
    if local or not music_active:
        return local
    repetition = _REPEATED_COMMAND.fullmatch(PUNCT.sub("", text))
    if repetition:
        command = _match(repetition.group(1), music_active, wake_phrase, offline, recipe_active)
        if is_quick_playback_command(command):
            return command
    return None


def is_quick_playback_command(local):
    if local is None:
        return False
    name, args = local
    return (name == "control_music" and args.get("action") in QUICK_MUSIC_ACTIONS
            or name == "set_volume"
            or name == "manage_recipe"
            or name == "end_conversation" and args.get("stop_music"))


def _match(text, music_active, wake_phrase, offline, recipe_active=False):
    command = _POLITE.sub("", PUNCT.sub("", text))
    said = Said(text, _CLEAN.sub("", text), command if len(command) <= 30 else "", music_active, wake_phrase, offline,
                recipe_active)
    for skill in SKILLS:
        for phrase in skill.phrases:
            args = phrase(said)
            if args is not None:
                return skill.name, args
    return None


def offline_keywords():
    lines = []
    for skill in SKILLS:
        for entry in skill.offline:
            phrase, _, label = entry.partition("@")
            lines.append(f"{phrase} @{label or phrase}")
    return "\n".join(lines) + "\n"


class Skills:
    def __init__(self, *providers):
        self.handlers = {}
        for skill in SKILLS:
            found = [getattr(p, skill.name) for p in providers if callable(getattr(p, skill.name, None))]
            if len(found) != 1:
                raise RuntimeError(f"skill {skill.name} has {len(found)} handlers")
            self.handlers[skill.name] = found[0]

    def run(self, name, args, has_reply=False):
        """Returns (result, effect, final): effect is "info", "action", "failed" or None, and a final
        result is spoken as is instead of going back to the model."""
        skill = _BY_NAME.get(name)
        if skill is None:
            return Result("不支持这个操作", False), "failed", True
        args = args if isinstance(args, dict) else {}
        result = self.handlers[name](args)
        kind = skill.kind(args) if callable(skill.kind) else skill.kind
        if kind is None:
            return result._replace(message="" if has_reply else result.message), None, True
        return result, kind if result.ok else "failed", kind == "action" and (result.ok or not skill.retry)


if __name__ == "__main__":
    print(offline_keywords(), end="")
