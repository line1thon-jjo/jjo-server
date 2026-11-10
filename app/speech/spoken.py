"""실제로 말한 내용을 기준 텍스트로 만든다. 대본과 확실히 다르게 말한 부분은 말한 단어로 바꾸고, 대본에 없이 덧붙인 말은 그 자리에 넣는다.
발음이 비슷한 차이(음성인식 오류 가능성)·군말·반복·신뢰도 낮은 인식은 대본 그대로 둔다. 대본을 그대로 읽었으면 텍스트는 대본과 같다.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.contracts.analysis_result import SpokenDifference, SpokenKind

from .align import WordTiming, match_script
from .config import SpeechConfig
from .disfluency import FILLERS
from .phonetic import sound_distance
from .text import Unit, _is_sentence_final

def build_spoken_text(
    script: str, units: list[Unit], words: list[WordTiming], cfg: SpeechConfig
) -> tuple[str, list[SpokenDifference], float]:
    hangul_total = sum(u.is_hangul for u in units)
    if not words or hangul_total == 0:
        return script, [], 0.0

    rec, hangul_pos, blocks = match_script(units, words, cfg)
    a_chars = [units[i].char for i in hangul_pos]
    ratio = sum(b.size for b in blocks) / hangul_total

    edges = [SimpleNamespace(a=0, b=0, size=0)] + list(blocks) + [SimpleNamespace(a=len(a_chars), b=len(rec), size=0)]
    replace_at: dict[int, tuple[int, str]] = {}
    insert_at: dict[int, list[str]] = {}

    def text_of(chars: list) -> str:
        pieces, last_word = [], None
        for r in chars:
            if last_word is not None and r[3] != last_word:
                pieces.append(" ")
            pieces.append(r[0])
            last_word = r[3]
        return "".join(pieces)

    for prev, nxt in zip(edges, edges[1:]):
        a_lo, a_hi = prev.a + prev.size, nxt.a
        b_lo, b_hi = prev.b + prev.size, nxt.b
        if b_hi <= b_lo:
            continue
        part = rec[b_lo:b_hi]
        probs = [r[4] for r in part]
        mean_prob = sum(probs) / len(probs)
        # 짧은 차이는 확신이 높아야 채택한다. 길게(5음절 이상) 다른 말로 인식되면 우연한 오인식일 가능성이 낮아 기준을 조금 낮춘다
        strong = len(part) >= cfg.deviation_min_syllables and mean_prob >= cfg.deviation_min_prob
        long_enough = len(part) >= cfg.deviation_long_syllables and mean_prob >= cfg.deviation_long_prob
        if not (strong or long_enough) or min(probs) < cfg.deviation_min_word_prob:
            continue

        if a_hi > a_lo:
            whole = "".join(r[0] for r in part)
            if sound_distance("".join(a_chars[a_lo:a_hi]), whole) <= cfg.substitute_min_distance:
                continue

        head_n = 0
        if b_lo > 0 and a_lo > 0:
            while head_n < len(part) and part[head_n][3] == rec[b_lo - 1][3]:
                head_n += 1
        tail_n = 0
        if b_hi < len(rec) and a_hi < len(a_chars):
            while tail_n < len(part) - head_n and part[len(part) - 1 - tail_n][3] == rec[b_hi][3]:
                tail_n += 1
        head, mid, tail = part[:head_n], part[head_n : len(part) - tail_n], part[len(part) - tail_n :]

        if a_hi > a_lo and not mid:
            if len(part) < cfg.deviation_long_syllables:
                continue
            # 두 어절에 걸쳐 길게 다른 말을 한 경우: 어절 경계로 나누지 않고 전체를 말한 내용으로 바꾼다
            head, mid, tail = [], part, []
        if head:
            insert_at.setdefault(units[hangul_pos[a_lo - 1]].idx + 1, []).append(text_of(head))

        spoken = text_of(mid) if mid else ""
        compact = spoken.replace(" ", "")
        keep_mid = not mid or compact in FILLERS
        if not keep_mid and a_hi > a_lo:
            c_lo, c_hi = units[hangul_pos[a_lo]].idx, units[hangul_pos[a_hi - 1]].idx
            if sound_distance("".join(a_chars[a_lo:a_hi]), compact) <= cfg.substitute_min_distance:
                keep_mid = True
            else:
                glued_head = bool(head) and c_lo == units[hangul_pos[a_lo - 1]].idx + 1
                replace_at[c_lo] = (c_hi, (" " if glued_head else "") + spoken + (" " if tail else ""))
        elif not keep_mid:
            before = "".join(a_chars[max(0, a_lo - len(compact)) : a_lo])
            if before == compact or "".join(a_chars[a_lo : a_lo + len(compact)]).startswith(compact):
                keep_mid = True
            else:
                pos = units[hangul_pos[a_lo]].idx if a_lo < len(hangul_pos) else len(script)
                sep = ". " if _is_sentence_final(spoken.split(" ")[-1]) else " "
                insert_at.setdefault(pos, []).append(spoken + sep)

        if tail:
            insert_at.setdefault(units[hangul_pos[a_hi]].idx, []).append(text_of(tail))

    if not replace_at and not insert_at:
        return script, [], ratio

    out: list[str] = []
    diffs: list[SpokenDifference] = []
    size = 0

    def emit(piece: str) -> int:
        nonlocal size
        out.append(piece)
        start = size
        size += len(piece)
        return start

    i = 0
    while i <= len(script):
        for piece in insert_at.get(i, []):
            text = piece.rstrip(". ")
            start = emit(piece)
            diffs.append(
                SpokenDifference(kind=SpokenKind.ADDED, start_idx=start, end_idx=start + len(text) - 1, spoken=text)
            )
        if i == len(script):
            break
        if i in replace_at:
            c_hi, spoken = replace_at[i]
            start = emit(spoken)
            core = spoken.strip()
            lead = len(spoken) - len(spoken.lstrip())
            diffs.append(
                SpokenDifference(
                    kind=SpokenKind.REPLACED,
                    start_idx=start + lead,
                    end_idx=start + lead + len(core) - 1,
                    spoken=core,
                    script_text=script[i : c_hi + 1],
                )
            )
            i = c_hi + 1
            continue
        emit(script[i])
        i += 1
    final = "".join(out).rstrip()
    return final, _merge_adjacent(final, diffs), ratio


def _merge_adjacent(text: str, diffs: list[SpokenDifference]) -> list[SpokenDifference]:
    merged: list[SpokenDifference] = []
    for d in sorted(diffs, key=lambda x: x.start_idx):
        if merged and d.start_idx - merged[-1].end_idx <= 2:
            m = merged[-1]
            scripts = [t for t in (m.script_text, d.script_text) if t]
            kind = SpokenKind.REPLACED if scripts else SpokenKind.ADDED
            merged[-1] = SpokenDifference(
                kind=kind,
                start_idx=m.start_idx,
                end_idx=d.end_idx,
                spoken=text[m.start_idx : d.end_idx + 1],
                script_text=" ".join(scripts) if scripts else None,
            )
        else:
            merged.append(d)
    return merged
