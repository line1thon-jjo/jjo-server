"""정답을 아는 합성 음성 생성기. 글자마다 지정한 길이·음높이·크기의 소리를 이어 붙여 실제 목소리 없이 분석 로직을 검증한다.
python -m app.speech.tests.synth samples/demo.wav 로 습관을 심은 데모 음성(wav, 대본, 어절 시각 json)을 만든다.
"""
from __future__ import annotations

import json
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.speech.align import WordTiming
from app.speech.text import B_COMMA, B_LINE, B_NONE, B_SENTENCE, B_WORD, parse_script

SR = 16000


@dataclass
class Truth:
    idx: int
    start_ms: float
    end_ms: float
    f0: float


def _burst(f0_a: float, f0_b: float, dur_ms: float, amp: float = 0.3) -> np.ndarray:
    n = int(SR * dur_ms / 1000)
    f0 = np.linspace(f0_a, f0_b, n)
    phase = 2 * np.pi * np.cumsum(f0) / SR
    sig = sum(np.sin(h * phase) / h for h in range(1, 6))
    x = np.linspace(0, 1, n)
    env = 0.25 + 0.75 * np.sin(np.pi * x) ** 0.6
    return (amp * sig * env).astype(np.float32)


def synthesize(
    script: str,
    base_ms: float = 150,
    base_f0: float = 120,
    overrides: dict | None = None,
    gaps: dict | None = None,
    inserts: dict | None = None,
    lead_ms: int = 400,
    tail_ms: int = 400,
    seed: int = 0,
):
    overrides, gaps, inserts = overrides or {}, gaps or {}, inserts or {}
    units = [u for u in parse_script(script) if u.is_hangul]
    default_gap = {B_NONE: 0, B_WORD: 60, B_COMMA: 250, B_LINE: 300, B_SENTENCE: 450}

    parts = [np.zeros(int(SR * lead_ms / 1000), dtype=np.float32)]
    t_ms = float(lead_ms)
    truth: list[Truth] = []
    for k, u in enumerate(units):
        ov = overrides.get(u.idx, {})
        dur = ov.get("dur", base_ms * (1.2 if u.has_batchim else 1.0))
        f_a, f_b = ov.get("f0", (base_f0 * (1 + 0.03 * np.sin(k)),) * 2)
        parts.append(_burst(f_a, f_b, dur, ov.get("amp", 0.3)))
        truth.append(Truth(u.idx, t_ms, t_ms + dur, float((f_a + f_b) / 2)))
        t_ms += dur
        gap = gaps.get(u.idx, default_gap[u.boundary_after])
        if k == len(units) - 1:
            gap = 0
        parts.append(np.zeros(int(SR * gap / 1000), dtype=np.float32))
        t_ms += gap
        ins = inserts.get(u.idx)
        if ins and k < len(units) - 1:
            pre, dur_i, post = ins.get("pre", 100), ins.get("dur", 250), ins.get("post", 100)
            parts.append(np.zeros(int(SR * pre / 1000), dtype=np.float32))
            if ins.get("noise"):
                rng_i = np.random.default_rng(1)
                parts.append((rng_i.standard_normal(int(SR * dur_i / 1000)) * ins.get("amp", 0.05)).astype(np.float32))
            else:
                f_i = ins.get("f0", 140)
                parts.append(_burst(f_i, f_i, dur_i, ins.get("amp", 0.25)))
            parts.append(np.zeros(int(SR * post / 1000), dtype=np.float32))
            t_ms += pre + dur_i + post
    parts.append(np.zeros(int(SR * tail_ms / 1000), dtype=np.float32))

    y = np.concatenate(parts)
    rng = np.random.default_rng(seed)
    y = y + (rng.standard_normal(len(y)) * 0.001).astype(np.float32)
    return y, truth, units


def words_from_truth(units, truth, pad_ms: float = 25, sloppy_end_ms: float = 300) -> list[WordTiming]:
    by_idx = {t.idx: t for t in truth}
    words: list[WordTiming] = []
    k = 0
    while k < len(units):
        j = k
        while j + 1 < len(units) and units[j + 1].word_id == units[k].word_id:
            j += 1
        start = by_idx[units[k].idx].start_ms - pad_ms
        end = by_idx[units[j].idx].end_ms + pad_ms
        if j + 1 < len(units):
            nxt = by_idx[units[j + 1].idx].start_ms - 10
            end = min(end + sloppy_end_ms, nxt) if nxt - end > 100 else end
        text = "".join(u.char for u in units[k : j + 1])
        words.append(WordTiming(text=text, start=start / 1000, end=end / 1000))
        k = j + 1
    return words


def write_wav(path: str | Path, y: np.ndarray) -> None:
    pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


DEMO_SCRIPT = (
    "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다. "
    "먼저 배경을 설명하겠습니다. 감사합니다."
)


def demo_spec():
    s = DEMO_SCRIPT
    up = 120 * 2 ** (5 / 12)

    def idx_of(sub: str, offset: int = 0, nth: int = 0) -> int:
        pos = -1
        for _ in range(nth + 1):
            pos = s.index(sub, pos + 1)
        return pos + offset

    end1 = idx_of("겠습니다", 3, 0)
    end2 = idx_of("문화입니다", 4, 0)
    end3 = idx_of("겠습니다", 3, 1)
    end4 = idx_of("감사합니다", 4, 0)
    overrides = {
        end1: {"dur": 450},
        end2: {"dur": 150, "f0": (up, up)},
        end3: {"dur": 450, "f0": (up, up)},
    }
    gaps = {idx_of("오늘", 1): 700, idx_of("주제는", 2): 650}
    start4 = idx_of("감사합니다")
    for i in range(start4, end4 + 1):
        overrides[i] = {"dur": 100}
    return overrides, gaps, {"end1": end1, "end2": end2, "end3": end3, "end4": end4, "start4": start4}


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/demo.wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    ov, gp, _ = demo_spec()
    y, truth, units = synthesize(DEMO_SCRIPT, overrides=ov, gaps=gp)
    write_wav(out, y)
    out.with_suffix(".txt").write_text(DEMO_SCRIPT, encoding="utf-8")
    words_path = out.with_name(out.stem + "_words.json")
    words_path.write_text(
        json.dumps([w.__dict__ for w in words_from_truth(units, truth)], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {out}, {out.with_suffix('.txt')}, {words_path}")
