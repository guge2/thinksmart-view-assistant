"""Text rules for speech: what recognition heard (wake word, unfinished sentences) and what the
model wrote (links, citations and Markdown that must not be read aloud)."""

import re
from itertools import groupby

# Recognition ends sentences at short pauses, sometimes before the pause. Listening stops once the
# user has been quiet a while after a sentence that sounds finished, or longer after one ending in a
# comma or connective.
UNFINISHED = re.compile(
    r"(?:[，、,：:；;]|然后|还有|而且|并且|或者|以及|另外|接着|因为|所以|但是|不过|如果|要是|比如|就是|还是"
    r"|那么|那个|这个|那|和|跟|与|把|给|让|帮我|我想|我要|嗯|呃|额)[。.！!]*$"
)
# Follow-up windows hear the room: a lone syllable or filler is noise unless the reply asked something.
FILLER = re.compile(r"[嗯啊呃额哦噢喔唉哎诶欸哈呵]+")
QUESTION_END = re.compile(r"(?:[？?]|[吗呢么][。！!～~]?)[\s”」』\"')）]*$")
PUNCTUATION = re.compile(r"[\s，。！？、,.!?~～…]+")
SPOKEN_CONTENT = re.compile(r"[^\s，。！？、,.!?：:；;…]")

LINK = re.compile(r"\[([^\]\n]*)\]\((?:[^()\s]|\([^()\s]*\))*\)")
# A parenthesised aside holding a link is a source note, e.g. "([site.com](https://...))".
LINK_ASIDE = re.compile(r"[（(][^（()）\n]*?" + LINK.pattern + r"[^（()）\n]*?[)）]")
URL = re.compile(r"(?:https?://|www\.)[^\s，。！？；、）)】\]]*")
DOMAIN = re.compile(r"[\w.-]+\.[A-Za-z]{2,}(?:/\S*)?")
CITATION = re.compile(r".*?|[-]|\[\^?\d+\]|【\d+[^】]*】")
EMPTY_ASIDE = re.compile(r"[（(]\s*(?:(?:来源|参考|出处|引自)[:：]?)?[\s,，、;；]*[)）]")
MARKDOWN = re.compile(r"\*\*|__|`|(?<=\n)[ \t]*(?:#+|>)[ \t]*")
URL_START = re.compile(r"(?:https?:/{0,2}|https?|htt|ht|h|www\.?|ww|w)$")
MARKDOWN_START = re.compile(r"(?:\n[ \t]*[#>]*[ \t]*|[*_]+)$")


def strip_wake_prefix(text, phrase):
    if not text or not phrase:
        return text
    punctuation = r"[\s，。！？、,.!?~～…`]*"
    word = punctuation.join(re.escape(char) for char in phrase)
    return re.sub(r"^(?:" + punctuation + word + r")+" + punctuation, "", text)


def compact(text):
    return PUNCTUATION.sub("", text)


def spoken_text(text, line_start=True):
    """Removes links, URLs and citation marks that web search adds to replies."""
    if line_start:
        return spoken_text("\n" + text, False)[1:]
    text = LINK_ASIDE.sub("", text)
    text = LINK.sub(lambda m: "" if not m.group(1).strip() or DOMAIN.fullmatch(m.group(1).strip()) else m.group(1), text)
    text = CITATION.sub("", URL.sub("", text))
    text = re.sub(r"[（(]\s*" + DOMAIN.pattern + r"\s*[)）]", "", text)
    return MARKDOWN.sub("", EMPTY_ASIDE.sub("", text))


class SpokenStream:
    """Holds back a possibly unfinished link or URL until it can be removed whole."""
    HOLD = 400

    def __init__(self):
        self.pending = ""
        self.line_start = True

    def feed(self, text):
        self.pending += text
        cut = self._safe_end(self.pending)
        out, self.pending = self.pending[:cut], self.pending[cut:]
        return self._emit(out)

    def flush(self):
        out, self.pending = self.pending, ""
        return self._emit(out)

    def _emit(self, text):
        if not text:
            return ""
        cleaned = spoken_text(text, self.line_start)
        self.line_start = text.endswith("\n")
        return cleaned

    def _safe_end(self, text):
        if len(text) > self.HOLD:
            return len(text)
        stack, hold = [], len(text)
        pairs = {"]": "[", ")": "(", "）": "(", "】": "【", "": ""}
        label = None  # (start, end) of the last "[text]", which a "(url)" may follow
        for index, char in enumerate(text):
            opener = "(" if char == "（" else char
            if opener in ("[", "(", "【", ""):
                linked = opener == "(" and label is not None and label[1] == index - 1
                stack.append((opener, label[0] if linked else index))
            elif char in pairs and stack and stack[-1][0] == pairs[char]:
                start = stack.pop()[1]
                if char == "]":
                    label = (start, index)
                    if index == len(text) - 1:
                        hold = min(hold, start)
        if stack:
            hold = min(hold, stack[0][1])
        url = URL.search(text)
        while url and url.end() != len(text):
            url = URL.search(text, url.end())
        if url:
            hold = min(hold, url.start())
        partial = URL_START.search(text)
        if partial:
            hold = min(hold, partial.start())
        prefix = "\n" if self.line_start else ""
        partial = MARKDOWN_START.search(prefix + text)
        if partial:
            hold = min(hold, max(0, partial.start() - len(prefix)))
        return hold


def wake_word_parts(fragments, phrase):
    """Yield (is_wake_word, text), keeping only an unfinished match between chunks."""
    prefixes = [r"\s*".join(map(re.escape, phrase[:n])) for n in range(1, len(phrase) + 1)]
    word = re.compile("(" + prefixes[-1] + ")")
    suffix = re.compile("(?:" + "|".join(prefixes[:-1]) + r")\s*\Z") if len(prefixes) > 1 else None
    pending = ""
    for fragment in fragments:
        parts = word.split(pending + fragment)
        rest = parts.pop()
        match = suffix.search(rest) if suffix else None
        end = match.start() if match else len(rest)
        pending = rest[end:]
        for index, text in enumerate(parts):
            if text:
                yield bool(index % 2), text
        if end:
            yield False, rest[:end]
    if pending:
        yield False, pending


def speech_segments(fragments, phrase):
    """Ordinary text streams in one TTS session; each spoken wake word is drained on its own, so the
    raw microphone can ignore the device saying its own name."""
    for guarded, group in groupby(wake_word_parts(fragments, phrase), key=lambda part: part[0]):
        if guarded:
            for _, word in group:
                yield True, (word,)
        else:
            yield False, (text for _, text in group)
