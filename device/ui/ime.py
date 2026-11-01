import bisect
import glob
import importlib.util
import logging
import unicodedata

VENV_GLOB = "/data/thinksmart/voice/venv/lib/python3*/site-packages/pypinyin/pinyin_dict.py"
COMMON = (
    "的一是不了在人有我他这个们中来上大为和国地到以说时要就出会可也你对生能而子那得于着下自之"
    "年过发后作里用道行所然家种事成方多经么去法学如都同现当没动面起看定天分还进好小部其些主样"
    "理心她本前开但因只从想实日意无力它与长把机十民第公此已工使情明性知全三又关点正业外将两高间"
    "由问很最重并物手应向头文体美相见被利什二等产或新己制身果加西月话合回特代内信表化老给世位次"
    "度门任常先海通教儿原东声提立及比员解水名真论处走义各入几口认条平系气题活更别打女变四神总何"
    "电数安少报才结反受目太量再感建务做接必场件计期市直资命山金指许统区保至队形社便空决治展马科"
    "司五基眼书非则听白界达光放强即像难且思王象完设式色路记南品住告类求据程北边张该交规万取拉格"
    "望觉术领共确传师观清今切院让识候带导争运笑飞风步改收根干造言联持组每济车亲极林服快办议往元"
    "英士近失转夫令准布始怎呢存未远叫台单影具罗字爱击流备连调深商算质团集百需价花华城石级整府离"
    "况亚请技际约示复病息究线似官火断精满支视消越器容照须九增研写称企八功吗包片史委乎查轻易早曾"
    "除农找装广显吧阿李标谈吃图念六引历首医局突专费号尽另周较注语仅考落青随选列武红响虽推势参希"
    "古众构房半节土投某案黑维划致陈律足态护七兴派孩验责营星够章音跟志底站严巴例防族供效续施留讲"
    "型料终答紧黄绝奇察母京段依批群项故按河米围江织害斗双境客纪采举攻父苏密低朝友诉止细愿千值仍"
    "男钱破网热助倒育属坐限船脸职速刻乐否刚威毛状率甚独球般普怕弹校苦创假久错承印晚兰试股拿脑预"
    "谁益阳若哪微尼继送急血惊伤素药适波夜省初喜卫源食险待述陆习置居劳财环排福纳欢雷警获模充负云"
    "停木游龙树疑层冷洲冲射略范竟句室异激汉村哈策演简卡判担州静退既衣您宗积余痛检差富灵协角占配"
    "征修皮挥胜降阶审沉坚善妈刘读啊超免压银买皇养伊怀执副乱抗犯追帮宣佛岁航优怪香著田铁控税左右"
    "份穿艺背阵草脚概恶块顿敢守酒岛托央户烈洋哥索胡款靠评版宝座释景顾弟登货互付伯慢欧换闻危忙核"
    "暗姐介坏讨丽良序升监临亮露永呼味野架域沙掉括鱼杂误湾吉减编楚肯测败屋跑梦散温困剑渐封救贵缺"
    "楼县尚毫移娘朋画班智亦耳恩短掌恐遗固席松秘谢鲁遇康虑幸均销钟诗藏赶剧票损忽巨旧端探湖录叶春"
    "乡附吸予礼港雨呀板庭妇归睛饭额含顺输摇招婚脱补谓督油疗旅泽材灭逐莫笔亡鲜词圣择寻厂睡博勒烟"
    "授诺伦岸奥唐卖俄载洛健堂旁宫喝借君禁阴园谋宋避抓荣姑孙逃牙束跳顶玉镇雪午练迫爷篇肉嘴馆遍凡"
    "础洞卷坦牛宁纸诸训私庄祖丝翻暴森塔默握戏隐熟骨访弱蒙歌店鬼软典欲萨伙遭盘爸扩盖弄雄稳忘亿刺"
    "拥徒姆杨齐赛趣曲刀床迎冰虚玩析窗醒妻透购替塞努休虎扬途侵刑绿兄迅套贸毕唯谷轮库迹尤竞街促延"
    "震弃甲伟麻川申缓潜闪售灯针哲络抵朱埃抱鼓植纯夏忍页杰筑折郑贝尊吴秀混臣雅振染盛怒舞圆搞狂措"
    "姓残秋培迷诚宽宇猛摆梅毁伸摩盟末乃悲拍丁赵硬麦蒋操耶阻订彩抽赞魔纷沿喊违妹浪汇币丰蓝殊献桌"
    "啦瓦莱援译夺汽烧距裁偏符勇触课敬哭懂墙袭召罚侠厅拜巧侧韩冒债曼融惯享戴童犹乘挂奖绍厚纵障讯"
    "涉彻刊丈爆乌役描洗玛患妙镜唱烦签仙彼弗症仿倾牌陷鸟轰咱菜闭奋庆撤泪茶疾缘播朗杜奶季丹狗尾仪"
    "偷奔珠虫驻孔宜艾桥淡翼恨繁寒伴叹旦愈潮粮缩罢聚径恰挑袋灰捕徐珍幕映裂泰隔启尖忠累炎暂估泛荒"
    "偿横拒瑞忆孤鼻闹羊呆厉衡胞零穷舍码赫婆魂灾洪腿胆津俗辩胸晓劲贫仁偶辑邦恢赖圈摸仰润堆碰艇稍"
    "迟辆废净凶署壁御奉旋冬矿抬蛋晨伏吹鸡倍糊秦盾杯租骑乏隆诊奴摄丧污渡旗甘耐凭扎抢绪粗肩梁幻菲"
    "皆碎宙叔岩荡综爬荷悉蒂返井壮薄悄扫敏碍殖详迪矛霍允幅撒剩凯颗骂赏液番箱贴漫酸郎腰舒眉忧浮辛"
    "恋餐吓挺励辞艘键伍峰尺昨黎辈贯侦滑券崇扰宪绕趋慈乔阅汗枝拖墨胁插箭腊粉泥氏彭拔骗凤慧媒佩"
    "苏杭沪渝蓉津冀晋辽吉黑皖闽赣鲁豫鄂湘粤桂琼川贵滇藏陕甘青宁新港澳台厦深圳珠汕佛莞惠"
)


UMLAUT = str.maketrans({char: "v" for char in "üǖǘǚǜ"})


def strip_tone(reading):
    plain = unicodedata.normalize("NFD", reading.translate(UMLAUT))
    return "".join(char for char in plain if not unicodedata.combining(char)).lower()


def load_pinyin_dict():
    for path in sorted(glob.glob(VENV_GLOB)):
        try:
            spec = importlib.util.spec_from_file_location("thinksmart_pinyin_dict", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.pinyin_dict
        except Exception as error:
            logging.warning("拼音词典加载失败：%s", error)
    return None


class PinyinInput:
    def __init__(self):
        self.loaded = False
        self.index = {}
        self.keys = []
        self.rank = {}

    def available(self):
        if not self.loaded:
            self.loaded = True
            self.build()
        return bool(self.index)

    def build(self):
        table = load_pinyin_dict()
        if not table:
            return
        common = {char: position for position, char in enumerate(COMMON)}
        entries = {}
        for codepoint, readings in table.items():
            if not 0x4E00 <= codepoint <= 0x9FFF:
                continue
            char = chr(codepoint)
            try:
                encoded = char.encode("gb2312")
            except UnicodeEncodeError:
                continue
            if char in common:
                rank = (0, common[char])
            else:
                rank = (1 if encoded[0] < 0xD8 else 2, encoded[0] * 256 + encoded[1])
            self.rank[char] = rank
            for reading in readings.split(","):
                key = strip_tone(reading.strip())
                if key.isascii() and key.isalpha():
                    entries.setdefault(key, set()).add(char)
        self.index = {
            key: sorted(chars, key=self.rank.__getitem__)
            for key, chars in entries.items()
        }
        self.keys = sorted(self.index)

    def has_prefix(self, prefix):
        position = bisect.bisect_left(self.keys, prefix)
        return position < len(self.keys) and self.keys[position].startswith(prefix)

    def candidates(self, buffer, limit=96):
        """Returns candidate characters and how many buffer letters a pick consumes."""
        if not buffer or not self.available():
            return [], 0
        if self.has_prefix(buffer):
            chosen = list(self.index.get(buffer, ()))
            extra = set()
            position = bisect.bisect_left(self.keys, buffer)
            while position < len(self.keys) and self.keys[position].startswith(buffer):
                if self.keys[position] != buffer:
                    extra.update(self.index[self.keys[position]])
                position += 1
            seen = set(chosen)
            chosen.extend(sorted(extra - seen, key=self.rank.__getitem__))
            return chosen[:limit], len(buffer)
        for length in range(min(len(buffer) - 1, 6), 0, -1):
            head = buffer[:length]
            if head in self.index:
                return self.index[head][:limit], length
        return [], 0
