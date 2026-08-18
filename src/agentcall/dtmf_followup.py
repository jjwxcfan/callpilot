"""Conservative parsing for an Agent's spoken DTMF execution intent."""

from __future__ import annotations

import re

_DIGIT_MAP = str.maketrans(
    {
        "零": "0",
        "〇": "0",
        "一": "1",
        "幺": "1",
        "二": "2",
        "两": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
    }
)
_INTENT_RE = re.compile(
    r"(?:^|[\s，。；！,;!])"
    r"我(?:来|帮(?:您|你))?按(?:一下|下)?(?:键)?\s*"
    r"(?P<digits>(?:[0-9零〇一二三四五六七八九幺两#*]|井号|星号|\s)+)"
)
_BLOCKED_RE = re.compile(
    r"[?？]|(?:吗|么|吧|是否|是不是|要不要)\s*[?？。！]?$|"
    r"(?:不|没|没有|还没|别|不用|不要)\s*(?:来|帮(?:您|你))?按|"
    r"我(?:来|帮(?:您|你))?按(?:错|错了)|"
    r"(?:如果|要是|假如|除非|还是|或者|或是)|"
    r"(?<!帮)(?:您按|你按)|"
    r"(?:请按|他说|她说|它说|对方说|客服说|系统说|系统提示|菜单说|播报|原话|复述)"
)

# 英文变体（WIL-120 一期）：AGENT_LANGUAGE=en 时 AI 说 "let me press 2"，
# 中文正则完全不命中，护窗对英文通话失效。同一套保守原则：
# 只认第一人称肯定式，拒绝疑问/否定/条件/指示对方/复述菜单原话。
_EN_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "star": "*", "pound": "#", "hash": "#",
}
_EN_DIGIT_TOKEN = r"(?:[0-9#*]|zero|one|two|three|four|five|six|seven|eight|nine|star|pound|hash)"
_INTENT_RE_EN = re.compile(
    r"(?:^|[\s,.;!])"
    r"(?:i(?:'ll|\s+will|'m\s+going\s+to|\s+am\s+going\s+to)\s+|let\s+me\s+)"
    r"(?:go\s+ahead\s+and\s+)?press(?:ing)?\s+"
    rf"(?P<digits>{_EN_DIGIT_TOKEN}(?:(?:[\s,]|and)+{_EN_DIGIT_TOKEN})*)",
    re.IGNORECASE,
)
_BLOCKED_RE_EN = re.compile(
    r"\b(?:won't|will\s+not|don't|do\s+not|not\s+going\s+to|no\s+need\s+to"
    r"|shouldn't|should\s+not|can't|cannot)\b[^.!?]*\bpress\b|"
    r"\b(?:if|unless|whether|or)\s+(?:i\s+)?press\b|"
    r"\b(?:you|please)\b[^.!?]*\bpress\b|"
    r"\bpress\s+\S+\s+for\b|"  # "press 1 for billing" = 复述菜单
    r"\b(?:it|they|he|she|the\s+menu|the\s+system|the\s+agent|the\s+rep)\s+"
    r"(?:says?|said|asked|prompts?)\b|"
    r"\bpressed\s+the\s+wrong\b",
    re.IGNORECASE,
)


def extract_spoken_dtmf(text: str) -> str | None:
    """Return a legal DTMF sequence from a narrow affirmative self-statement.

    The guard deliberately rejects questions, negation, conditions and quoted
    menu wording. It executes an action the Agent says it is taking; it does not
    interpret the remote party's IVR menu.
    """

    normalized = " ".join(str(text or "").strip().split())
    # 任一语言的拦截正则命中即拒绝：宁可漏执行（AI 会被追问），不可误按键。
    if not normalized or _BLOCKED_RE.search(normalized) or _BLOCKED_RE_EN.search(
        normalized
    ):
        return None
    match = _INTENT_RE.search(normalized)
    if match is not None:
        raw = match.group("digits").replace("井号", "#").replace("星号", "*")
        digits = re.sub(r"\s+", "", raw).translate(_DIGIT_MAP)
        if re.fullmatch(r"[0-9*#]{1,32}", digits):
            return digits
        return None
    match = _INTENT_RE_EN.search(normalized)
    if match is None:
        return None
    tokens = re.split(r"[\s,]+", match.group("digits").strip())
    digits = "".join(
        _EN_DIGIT_WORDS.get(token.lower(), token)
        for token in tokens
        if token and token.lower() != "and"
    )
    if not re.fullmatch(r"[0-9*#]{1,32}", digits):
        return None
    return digits
