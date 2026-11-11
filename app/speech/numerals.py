"""Whisper 는 숫자를 아라비아 숫자("7시", "200쪽")로 적는다. 대본은 한글("일곱 시", "이백 쪽")이므로
숫자를 소리 나는 한글로 바꿔야 비교할 수 있다. 한자어(이백)·고유어(일곱) 읽기를 모두 만들어 두고, 대본에 실제로 있는 쪽을 고른다.
대본에 해당 읽기가 없으면(대본이 숫자로 적혀 있거나 다르게 읽은 경우) 글자를 바꾸지 않는다.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import replace

_SINO = ["영", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]
_NATIVE_UNIT = ["", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉"]
_NATIVE_UNIT_DET = ["", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉"]
_NATIVE_TENS = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]
_NATIVE_TENS_DET = ["", "열", "스무", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]
_BIG = ["", "만", "억", "조"]

_DIGITS = re.compile(r"\d[\d,]*(?:\.\d+)?|[^\d]+")


def sino(n: int) -> str:
    if n == 0:
        return _SINO[0]
    groups: list[int] = []
    while n:
        groups.append(n % 10000)
        n //= 10000
    parts: list[str] = []
    for gi in range(len(groups) - 1, -1, -1):
        g = groups[gi]
        if g == 0:
            continue
        s = ""
        for pos, unit in ((3, "천"), (2, "백"), (1, "십"), (0, "")):
            d = (g // 10**pos) % 10
            if d == 0:
                continue
            s += ("" if d == 1 and unit else _SINO[d]) + unit
        if gi == 1 and g == 1:
            s = ""
        parts.append(s + _BIG[gi])
    return "".join(parts)


def native_forms(n: int) -> list[str]:
    if not 1 <= n <= 99:
        return []
    t, u = divmod(n, 10)
    if u == 0:
        return [_NATIVE_TENS_DET[t]]
    forms = {_NATIVE_TENS[t] + _NATIVE_UNIT[u], _NATIVE_TENS[t] + _NATIVE_UNIT_DET[u]}
    return sorted(forms)


def _candidates(num: str) -> list[str]:
    try:
        n = int(num.replace(",", "")) if "." not in num else None
    except ValueError:
        n = None
    if n is None:
        return []
    out = [sino(n)] + native_forms(n)
    if n == 6:
        out.append("유")
    if n == 10:
        out.append("시")
    return out


def _hangul_only(s: str) -> str:
    return "".join(c for c in s if "가" <= c <= "힣")


def normalize_numerals(words: list, script: str) -> list:
    """words: WordTiming 목록. 숫자가 든 어절을 대본의 읽기에 맞춰 한글로 바꾼 새 목록을 돌려준다."""
    script_h = _hangul_only(script)
    out = []
    for w in words:
        if not any(c.isdigit() for c in w.text):
            out.append(w)
            continue
        pieces = _DIGITS.findall(w.text)
        options: list[list[str]] = []
        for p in pieces:
            if p[0].isdigit():
                options.append(_candidates(p) or [p])
            else:
                options.append([_hangul_only(p)])
        chosen = None
        for combo in itertools.product(*options):
            cand = "".join(combo)
            if cand and cand in script_h and not any(c.isdigit() for c in cand):
                chosen = cand
                break
        out.append(replace(w, text=chosen) if chosen else w)
    return out
