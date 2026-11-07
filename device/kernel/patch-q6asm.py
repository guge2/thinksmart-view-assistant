#!/usr/bin/env python3
"""将上游 PCM 恢复和环形位置修正应用到项目提供的 6.19.5 模块。"""
import hashlib
from pathlib import Path
import struct
import sys

# 对照 gregkh/linux v6.19.5 与 torvalds/linux 的 q6asm-dai.c。
# 精确限定供应方模块；保留其 ABI、重定位和 KCFI 元数据。
SOURCE_SHA256 = "aec3a6a2aea2469d06fdcb0562d45ce108204725df2ea1a3d5355dc1d644371f"
data = bytearray(Path(sys.argv[1]).read_bytes())
if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
    raise SystemExit("不是已核验的 6.19.5 q6asm-dai 模块")
section_offset = struct.unpack_from("<Q", data, 0x28)[0]
entry_size, count, names_index = struct.unpack_from("<HHH", data, 0x3A)
sections = [struct.unpack_from("<IIQQQQIIQQ", data, section_offset + i * entry_size) for i in range(count)]
names = sections[names_index]
strings = data[names[4]:names[4] + names[5]]
text = next(section for section in sections if strings[section[0]:].split(b"\0", 1)[0] == b".text")

patches = [
    # prepare 会重置 ALSA 指针；同步清零排队指针，避免停歌后不再向 DSP 送帧。
    # ALSA 已验证 period_size/frame_bits 非负，复用原先的负数舍入指令空间。
    (0x798, "091d00911f0100f128b1889a", "bf8200f91f2003d51f2003d5"),
    # STOP/EOS 不抹掉仍需由 prepare 清理的已分配会话状态。
    (0xA84, "092901b9", "1f2003d5"),
    (0xC54, "682801b9", "1f2003d5"),
    # prepare 失败后保留 audio_client，让 close 承担释放工作。
    (0x9E4, "a08a40f9", "03000014"),
    # pointer = (hw_periods % runtime->periods) * runtime->period_size。
    (0xADC, "683e40f9097c4093f30b40f9087d099b080500f1e033889a",
     "698240b90a08c91a4981091b683e40f9f30b40f9007d099b"),
]
for offset, before, after in patches:
    before, after = bytes.fromhex(before), bytes.fromhex(after)
    start = text[4] + offset
    if len(before) != len(after) or data[start:start + len(before)] != before:
        raise SystemExit(f"模块指令不匹配：.text+{offset:#x}")
    data[start:start + len(before)] = after
Path(sys.argv[2]).write_bytes(data)
print(hashlib.sha256(data).hexdigest())
