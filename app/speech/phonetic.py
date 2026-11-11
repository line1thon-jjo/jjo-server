"""한글 소리 비교: 두 글자열이 자모 단위로 얼마나 다른지 0~1 로 잰다 (ㅐ/ㅔ 등 구분하기 어려운 모음은 같게 본다)."""
from __future__ import annotations

_VOWEL_MERGE = {3: 1, 5: 1, 7: 1}


# 준말(되어→돼, 보아→봐, 하여→해, 주는→준, 것인→건)은 같은 소리로 본다: 양쪽을 준말 꼴로 맞춘 뒤 비교한다
_VOWEL_JOIN = {
    (8, 0): 9, (13, 4): 14, (11, 4): 10, (20, 4): 6, (0, 6): 1, (18, 4): 4,
    (1, 4): 1, (0, 0): 0, (0, 4): 0, (4, 4): 4, (5, 4): 5, (20, 6): 6, (8, 4): 4,
}
_YEO_A_EO = {0, 4, 6}  # ㅏ ㅓ ㅕ (초성 ㅇ 인 음절 아/어/여)


def _split(ch: str):
    code = ord(ch) - 0xAC00
    if not 0 <= code < 11172:
        return None
    return code // 588, (code % 588) // 28, code % 28


def _join(cho: int, jung: int, jong: int) -> str:
    return chr(0xAC00 + cho * 588 + jung * 28 + jong)


def _contract(text: str) -> str:
    out: list[str] = []
    for ch in text:
        cur = _split(ch)
        prev = _split(out[-1]) if out else None
        if cur is None or prev is None:
            out.append(ch)
            continue
        c_cho, c_jung, c_jong = cur
        p_cho, p_jung, p_jong = prev
        if out[-1] == "것" and ch in "이은을인":
            out[-1] = {"이": "게", "은": "건", "을": "걸", "인": "건"}[ch]
            continue
        if p_jong == 0 and c_cho == 11 and c_jung in _YEO_A_EO and (p_jung, c_jung) in _VOWEL_JOIN:
            out[-1] = _join(p_cho, _VOWEL_JOIN[(p_jung, c_jung)], c_jong)
            continue
        if p_jong == 0 and ch in "는를":
            out[-1] = _join(p_cho, p_jung, 4 if ch == "는" else 8)
            continue
        out.append(ch)
    return "".join(out)


def _tokens(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if not 0 <= code < 11172:
            continue
        cho, jung, jong = code // 588, (code % 588) // 28, code % 28
        out.append(("c", cho))
        out.append(("v", _VOWEL_MERGE.get(jung, jung)))
        if jong:
            out.append(("f", jong))
    return out


def sound_distance(a: str, b: str) -> float:
    x, y = _tokens(_contract(a)), _tokens(_contract(b))
    if not x and not y:
        return 0.0
    prev = list(range(len(y) + 1))
    for i, tx in enumerate(x, 1):
        cur = [i]
        for j, ty in enumerate(y, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (tx != ty)))
        prev = cur
    return prev[-1] / max(len(x), len(y))
