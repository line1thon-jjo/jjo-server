"""대본 문자열을 글자 단위(Unit)로 분해하고, 어절·문장·쉼 경계를 정한다.
문장부호가 없는 대본은 어미 규칙으로 문장 끝과 절 끝(숨 고를 자리)을 추정한다.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

_HANGUL_FIRST = 0xAC00
_HANGUL_LAST = 0xD7A3
_JONG_B = 17

SENTENCE_END_CHARS = frozenset(".?!…。？！")
QUESTION_CHARS = frozenset("?？")
COMMA_CHARS = frozenset(",;:、，；：")

B_NONE, B_WORD, B_COMMA, B_LINE, B_SENTENCE = "NONE", "WORD", "COMMA", "LINE", "SENTENCE"
_RANK = {B_NONE: 0, B_WORD: 1, B_COMMA: 2, B_LINE: 3, B_SENTENCE: 4}
_STRENGTH = {B_NONE: 0, B_WORD: 1, B_COMMA: 2, B_LINE: 2, B_SENTENCE: 3}

_NOUN_YO = frozenset({"필요", "불필요", "중요", "주요", "수요", "개요", "요"})
_NOUN_DA = frozenset({"다", "바다"})
_YO_STEMS = frozenset("어아해에예세네군래데까나지게워여가")
_CLAUSE_ENDINGS = (
    "는데", "지만", "니까", "으며", "면서", "어서", "아서", "해서", "라서", "인데",
    "다면", "으니", "으면", "라면", "거나", "든지", "더니", "다가",
    "하고", "이고", "되고", "있고", "없고", "했고", "였고", "겠고",
)


def is_hangul_syllable(ch: str) -> bool:
    return _HANGUL_FIRST <= ord(ch) <= _HANGUL_LAST


def has_batchim(ch: str) -> bool:
    return is_hangul_syllable(ch) and (ord(ch) - _HANGUL_FIRST) % 28 != 0


def _jong(ch: str) -> int:
    return (ord(ch) - _HANGUL_FIRST) % 28


def _is_question_word(w: str) -> bool:
    if w.endswith("니까"):
        return len(w) >= 3 and is_hangul_syllable(w[-3]) and _jong(w[-3]) == _JONG_B
    if w.endswith(("까요", "나요")):
        return True
    return len(w) >= 2 and w.endswith("까")


def _is_sentence_final(w: str) -> bool:
    if w in _NOUN_YO or w in _NOUN_DA:
        return False
    if _is_question_word(w):
        return True
    if w.endswith("다"):
        return len(w) >= 2
    if w.endswith("요"):
        return len(w) >= 2 and w[-2] in _YO_STEMS
    return len(w) >= 2 and w.endswith("죠")


def _is_clause_end(w: str) -> bool:
    return w.endswith(_CLAUSE_ENDINGS)


@dataclass(frozen=True)
class Unit:
    idx: int
    char: str
    is_hangul: bool
    word_id: int
    boundary_after: str
    strength: int
    sentence_id: int
    is_sentence_end: bool
    is_question_end: bool

    @property
    def has_batchim(self) -> bool:
        return has_batchim(self.char)


def _infer_boundaries(raw: list[list], allow_sentence: bool) -> None:
    texts: dict[int, str] = {}
    lasts: dict[int, int] = {}
    for k, r in enumerate(raw):
        texts[r[2]] = texts.get(r[2], "") + r[1]
        lasts[r[2]] = k
    for wid, k in lasts.items():
        r = raw[k]
        if r[3] not in (B_WORD, B_NONE):
            continue
        w = "".join(c for c in texts[wid] if is_hangul_syllable(c))
        if not w:
            continue
        if allow_sentence and _is_sentence_final(w):
            r[3] = B_SENTENCE
            r[4] = _is_question_word(w)
        elif r[3] == B_WORD and _is_clause_end(w):
            r[3] = B_COMMA


def parse_script(script: str) -> list[Unit]:
    raw: list[list] = []
    word_id = 0
    in_word = False

    def upgrade(boundary: str) -> None:
        if raw and _RANK[boundary] > _RANK[raw[-1][3]]:
            raw[-1][3] = boundary

    n = len(script)
    for idx, ch in enumerate(script):
        if ch.isspace():
            if in_word:
                word_id += 1
                in_word = False
            upgrade(B_LINE if ch in "\r\n" else B_WORD)
        elif ch in SENTENCE_END_CHARS:
            if ch == "." and 0 < idx < n - 1 and script[idx - 1].isdigit() and script[idx + 1].isdigit():
                continue
            upgrade(B_SENTENCE)
            if raw and ch in QUESTION_CHARS:
                raw[-1][4] = True
        elif ch in COMMA_CHARS:
            upgrade(B_COMMA)
        elif unicodedata.category(ch)[0] in "PSZC":
            continue
        else:
            raw.append([idx, ch, word_id, B_NONE, False])
            in_word = True

    if not raw:
        return []

    has_sentence_punct = any(r[3] == B_SENTENCE for r in raw)
    _infer_boundaries(raw, allow_sentence=not has_sentence_punct)
    ends_sentence = (
        (lambda b: b == B_SENTENCE) if has_sentence_punct else (lambda b: b in (B_LINE, B_SENTENCE))
    )

    units: list[Unit] = []
    sentence_id = 0
    for k, (idx, ch, wid, boundary, is_q) in enumerate(raw):
        is_end = ends_sentence(boundary) or k == len(raw) - 1
        units.append(
            Unit(
                idx=idx,
                char=ch,
                is_hangul=is_hangul_syllable(ch),
                word_id=wid,
                boundary_after=boundary,
                strength=3 if is_end else _STRENGTH[boundary],
                sentence_id=sentence_id,
                is_sentence_end=is_end,
                is_question_end=is_end and is_q,
            )
        )
        if is_end:
            sentence_id += 1
    return units
