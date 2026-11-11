"""규칙 기반 피드백 문구 (BE2 담당). Gemini 를 쓸 수 없거나 결과가 잘못됐을 때 쓰고, Gemini 에 예시로도 준다.

모든 문구는 글자 수 상한(제목 20, 설명 90, 총평 20/30) 안에 들어가야 한다. 테스트가 확인한다.
"""
import string
from typing import Any

from app.domains.report.feedback.candidates import GAP_KINDS, Candidate

HEADLINE_MAX = 20  # 명세(REPORT_API.md) 기준. 규칙 문구는 이 안에 들어간다
FOOTER_MAX = 30
TITLE_MAX = 20
BODY_MAX = 90
# Gemini 총평에 주는 길이. 모델은 글자 수를 정확히 못 세서 명세보다 짧게 시키고,
# 조금 넘어도 그대로 쓴다(넘었다고 규칙 문구로 바꾸지 않음).
HEADLINE_TARGET = 15
FOOTER_TARGET = 24

KIND_NAMES = {
    "END_RISE": "끝 올림", "END_STRETCH": "끝 끌기", "END_FADE": "끝 흐림", "MID_STRETCH": "중간 늘임",
    "MID_PAUSE": "중간 쉼", "LONG_PAUSE": "긴 쉼", "NO_BREAK": "이어 말하기", "PACE": "말 빠르기", "PAUSE": "쉼",
}

# 같은 종류가 최대 3개까지 나올 수 있어 종류마다 문구 3개를 둔다. 나온 순서대로 첫째·둘째·셋째 문구를 쓴다.
# {word}: 문제 글자가 든 단어(쉼 종류는 쉼 앞 단어), {char}: 문제 글자, {sec}: 쉼 길이(초, 소수 첫째 자리),
# {spot}: "‘단어’의 ‘글자’"(단어가 그 글자 하나면 "‘글자’").
# 자리 표시 뒤에는 받침에 따라 바뀌는 조사(을/를, 이/가)를 붙이지 않는다.
_TEMPLATES: dict[str, tuple[tuple[str, str], ...]] = {
    "END_STRETCH": (
        ("문장 끝 짧게 맺기", "{spot} 소리가 길게 끌렸어요. 마지막 글자를 짧게 끊고 마침표에서 숨을 쉬어 보세요."),
        ("끝음 툭 끊기", "문장 끝을 길게 늘였어요. ‘{word}’까지 같은 박자로 말하고 바로 멈춰 보세요."),
        ("마무리 깔끔하게 하기", "여기서도 ‘{char}’ 소리가 끌렸어요. 끝 글자를 다른 글자와 같은 길이로 말해 보세요."),
    ),
    "MID_STRETCH": (
        ("중간에 늘이지 않기", "{spot} 소리가 길게 늘어졌어요. 다음 말을 고를 시간이 필요하면 늘이지 말고 잠깐 멈춰 보세요."),
        ("늘이는 대신 잠깐 쉬기", "{spot} 소리를 끌면서 말이 이어졌어요. 글자마다 길이를 비슷하게 맞춰 보세요."),
        ("글자 길이 고르게 맞추기", "여기서도 {spot} 소리가 길어졌어요. 늘이지 말고 다음 단어로 가볍게 넘어가 보세요."),
    ),
    "END_RISE": (
        ("문장 끝 내려 맺기", "‘{word}’에서 끝이 올라가 질문처럼 들렸어요. 마지막 글자를 살짝 내려 말해 보세요."),
        ("끝 글자 낮게 맺기", "설명하는 문장인데 끝이 올라갔어요. ‘{word}’ 부분을 말할 때 목소리를 아래로 내려놓는다고 생각해 보세요."),
        ("평서문처럼 끝내기", "여기서도 ‘{word}’에서 끝이 올라갔어요. 마침표를 떠올리며 끝 글자를 한 단계 낮춰 보세요."),
    ),
    "END_FADE": (
        ("끝까지 또렷하게", "‘{word}’에서 목소리가 작아져 끝이 잘 안 들렸어요. 마지막 글자까지 같은 크기로 말해 보세요."),
        ("끝 단어 또렷하게 말하기", "문장 끝으로 갈수록 소리가 줄었어요. 끝 단어 ‘{word}’ 부분을 오히려 또렷하게 내 보세요."),
        ("끝까지 같은 크기로 말하기", "여기서도 ‘{word}’에서 소리가 작아졌어요. 마지막 단어에 숨을 조금 남겨 두세요."),
    ),
    "MID_PAUSE": (
        ("문장 안에서 이어 말하기", "‘{word}’ 뒤에서 {sec}초 멈췄어요. 한 문장은 한 호흡에 이어서 말해 보세요."),
        ("한 호흡에 읽기", "문장 중간에 멈추면 말이 끊겨 들려요. ‘{word}’부터 문장 끝까지 미리 소리 내어 이어 읽어 보세요."),
        ("쉼 자리 옮기기", "‘{word}’ 뒤에서도 {sec}초 쉬었어요. 쉼은 쉼표나 마침표 자리로 옮겨 보세요."),
    ),
    "LONG_PAUSE": (
        ("쉼 길이 줄이기", "‘{word}’ 뒤에서 {sec}초 쉬었어요. 숨은 1초 안쪽으로 고르고 다음 문장을 시작해 보세요."),
        ("쉼 짧게 하기", "문장 사이 쉼이 평소보다 길었어요. 다음 문장의 첫 단어를 미리 떠올려 두면 쉼이 짧아져요."),
        ("흐름 이어 가기", "‘{word}’ 뒤에서도 {sec}초 쉬었어요. 한 박자 정도만 쉬고 이어 말해 보세요."),
    ),
    "NO_BREAK": (
        ("마침표에서 한 번 쉬기", "‘{word}’에서 문장이 끝났는데 쉬지 않고 다음 말로 넘어갔어요. 마침표마다 숨 한 번씩 넣어 보세요."),
        ("문장 구분해 말하기", "문장이 쉼 없이 붙어서 경계가 흐려졌어요. ‘{word}’ 뒤에서 반 박자 멈춰 보세요."),
        ("마침표에서 숨 쉬기", "‘{word}’ 뒤에서도 바로 다음 문장으로 넘어갔어요. 문장 끝에서 짧게 숨을 쉬어 보세요."),
    ),
    "PACE_FAST": (
        ("빠른 구절 천천히 말하기", "이 구절이 평소보다 빠르게 지나갔어요. 단어 사이를 반 박자씩 띄우며 또박또박 말해 보세요."),
        ("빠른 구절 속도 늦추기", "이 구절도 평소보다 빨랐어요. 단어 하나하나를 또박또박 말해 보세요."),
        ("한 박자 늦춰 읽기", "빨리 말하면 듣는 사람이 놓치기 쉬워요. 이 구절은 한 박자 늦춰 읽어 보세요."),
    ),
    "PACE_SLOW": (
        ("느려진 구절 속도 맞추기", "이 구절에서 속도가 많이 느려졌어요. 앞 문장과 같은 속도로 이어서 읽어 보세요."),
        ("느린 구절 당기기", "이 구절도 평소보다 느렸어요. 다른 구절처럼 자연스러운 속도로 읽어 보세요."),
        ("속도 고르게 맞추기", "한 구절만 느려지면 흐름이 처져 들려요. 앞 구절의 속도를 이어 가 보세요."),
    ),
    "PAUSE": (
        ("멈춘 자리 다듬기", "‘{word}’ 뒤에서 {sec}초 멈췄어요. 강조하려던 게 아니라면 쉼을 짧게 줄여 보세요."),
        ("쉼 길이 줄이기", "‘{word}’ 뒤에서도 {sec}초 쉬었어요. 숨만 고르고 바로 다음 말로 넘어가 보세요."),
        ("쉼 한 박자로 줄이기", "{sec}초 쉼은 길게 느껴질 수 있어요. 한 박자 정도로 줄여 보세요."),
    ),
}

# 단어·쉼 길이를 채울 수 없거나 채운 뒤 상한을 넘을 때 쓰는 문구(자리 표시 없음).
_GENERIC: dict[str, tuple[tuple[str, str], ...]] = {
    "END_RISE": (
        ("문장 끝 내려 맺기", "문장 끝에서 음높이가 올라갔어요. 마지막 글자를 살짝 내려 말해 보세요."),
        ("끝 글자 낮게 맺기", "여기서도 문장 끝이 올라갔어요. 마침표를 떠올리며 끝 글자를 한 단계 낮춰 보세요."),
        ("평서문처럼 끝내기", "끝이 올라가면 묻는 말처럼 들릴 수 있어요. 이 문장은 끝을 내리며 마무리해 보세요."),
    ),
    "END_STRETCH": (
        ("문장 끝 짧게 맺기", "문장 끝을 길게 끌었어요. 마지막 글자를 짧고 분명하게 끝내 보세요."),
        ("끝 글자 끌지 않기", "이 문장도 끝 글자를 늘였어요. 끝 글자를 다른 글자와 같은 길이로 말해 보세요."),
        ("마무리 깔끔하게 하기", "문장 끝이 길어지면 말이 늘어져 들려요. 끝 글자에서 바로 멈춰 보세요."),
    ),
    "END_FADE": (
        ("끝까지 또렷하게", "문장 끝에서 목소리가 작아졌어요. 마지막 단어까지 같은 크기로 말해 보세요."),
        ("끝 단어 또렷하게 말하기", "여기서도 끝으로 갈수록 목소리가 작아졌어요. 마지막 단어에 숨을 조금 남겨 두세요."),
        ("끝까지 같은 크기로 말하기", "문장 끝이 작아지면 마지막 말이 잘 안 들려요. 끝 단어를 또박또박 말해 보세요."),
    ),
    "MID_STRETCH": (
        ("중간에 늘이지 않기", "문장 중간에서 말을 길게 늘였어요. 다음 단어를 미리 보고 고르게 이어 말해 보세요."),
        ("글자 길이 고르게 맞추기", "이 부분에서도 중간 글자를 늘였어요. 글자마다 길이를 비슷하게 맞춰 보세요."),
        ("늘이는 대신 잠깐 쉬기", "말을 늘이면 망설이는 듯 들릴 수 있어요. 생각할 틈이 필요하면 잠깐 멈춰 보세요."),
    ),
    "MID_PAUSE": (
        ("문장 안에서 이어 말하기", "문장 중간에서 길게 쉬었어요. 한 문장은 한 호흡에 이어 말해 보세요."),
        ("쉼 자리 옮기기", "이 문장도 중간에서 끊겼어요. 쉼은 쉼표나 마침표 자리로 옮겨 보세요."),
        ("한 호흡에 읽기", "중간에 오래 쉬면 문장이 둘로 나뉘어 들려요. 미리 숨을 쉬고 한 번에 읽어 보세요."),
    ),
    "LONG_PAUSE": (
        ("쉼 길이 줄이기", "숨 고를 자리에서 평소보다 오래 쉬었어요. 쉼은 짧게 하고 바로 이어 가 보세요."),
        ("쉼 짧게 하기", "이 자리에서도 오래 쉬었어요. 숨만 고르고 다음 문장으로 넘어가 보세요."),
        ("흐름 이어 가기", "쉼이 길면 흐름이 끊겨 들려요. 한 박자 정도만 쉬고 이어 말해 보세요."),
    ),
    "NO_BREAK": (
        ("마침표에서 한 번 쉬기", "문장이 끝났는데 쉬지 않고 이어 말했어요. 마침표 뒤에서 한 박자 쉬어 보세요."),
        ("마침표에서 숨 쉬기", "이 문장도 끝나자마자 다음 문장으로 넘어갔어요. 문장 끝에서 짧게 숨을 쉬어 보세요."),
        ("문장 구분해 말하기", "쉬지 않고 이어 말하면 문장 구분이 흐려져요. 문장 사이에 잠깐 멈춰 보세요."),
    ),
    "PACE_FAST": _TEMPLATES["PACE_FAST"],
    "PACE_SLOW": _TEMPLATES["PACE_SLOW"],
    "PAUSE": (
        ("멈춘 자리 다듬기", "이 부분에서 오래 멈췄어요. 강조하려던 게 아니라면 쉼을 짧게 줄여 보세요."),
        ("쉼 길이 줄이기", "여기서도 오래 쉬었어요. 숨만 고르고 바로 다음 말로 넘어가 보세요."),
        ("쉼 한 박자로 줄이기", "긴 쉼은 흐름을 끊을 수 있어요. 한 박자 정도로 줄여 보세요."),
    ),
}

# Gemini 에만 주는 추가 예시. 조건이 맞을 때만 쓰게 한다(규칙 문구는 조건을 판단할 수 없어 쓰지 않음).
CONDITIONAL_EXAMPLES = {
    "MID_STRETCH": [{"when": "늘인 글자가 조사일 때", "body": "{spot} 소리를 끌면서 말이 이어졌어요. 조사는 앞 단어에 붙여서 가볍게 넘겨 보세요."}],
}

# 프롬프트에 후보마다 주는 연습 방향. 문제와 다른 조언(예: 쉼 항목에 속도 이야기)을 막는다.
ADVICE = {
    "END_RISE": "문장 끝 음높이를 내려 맺기",
    "END_STRETCH": "끝 글자를 짧고 분명하게 끝내기",
    "END_FADE": "마지막 단어까지 같은 크기로 말하기",
    "MID_STRETCH": "글자 길이를 고르게 맞추고, 생각할 틈은 늘임 대신 짧은 쉼으로 두기",
    "MID_PAUSE": "문장 중간에서 끊지 말고 쉼표·마침표 자리에서 쉬기",
    "LONG_PAUSE": "숨만 고르고 쉼을 짧게 하기",
    "NO_BREAK": "문장이 끝나면 한 박자 쉬기",
    "PACE_FAST": "이 구절을 단어 사이를 띄우며 천천히 읽기",
    "PACE_SLOW": "이 구절을 앞뒤 구절과 같은 속도로 읽기",
    "PAUSE": "쉼을 한 박자 정도로 줄이기",
}

NO_ISSUE_SUMMARY = ("안정적으로 잘 말했어요.", "지금처럼 꾸준히 연습해 보세요.")

# Gemini 총평에 이 말이 들어가면 그 줄을 규칙 문구로 바꾼다. 분석 수치로 알 수 없는 인상·감정·내용 평가라서다.
SUMMARY_BANNED = ("차분", "자신감", "자신 있", "감정", "마음", "완벽", "멋진", "멋지", "훌륭", "내용")


def template_key(candidate: Candidate) -> str:
    if candidate.kind == "PACE":
        return "PACE_SLOW" if candidate.metrics.get("speed") == "SLOW" else "PACE_FAST"
    return candidate.kind


def pause_sec(candidate: Candidate) -> str | None:
    """쉼 길이를 화면 표기와 같은 소수 첫째 자리 초로."""
    ms = candidate.metrics.get("duration_ms")
    return f"{ms / 1000:.1f}" if ms else None


def fill_values(candidate: Candidate) -> dict[str, str]:
    """문구의 {word}·{char}·{sec} 에 넣을 값. 없는 값은 빠진다."""
    focus = candidate.focus
    word = focus.get("word_before") if candidate.kind in GAP_KINDS else focus.get("word")
    char = focus.get("char")
    spot = (f"‘{char}’" if not word or word == char else f"‘{word}’의 ‘{char}’") if char else None
    values = {"word": word, "char": char, "spot": spot, "sec": pause_sec(candidate)}
    return {k: v for k, v in values.items() if v}


def examples(candidate: Candidate) -> dict[str, Any]:
    """Gemini 에 주는 그 종류의 예시(자리 표시 그대로)."""
    key = template_key(candidate)
    out: dict[str, Any] = {"title": [t for t, _ in _TEMPLATES[key]], "body": [b for _, b in _TEMPLATES[key]]}
    if key in CONDITIONAL_EXAMPLES:
        out["conditional"] = CONDITIONAL_EXAMPLES[key]
    return out


def _needed(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def rule_text(candidate: Candidate, variant: int = 0) -> tuple[str, str]:
    """후보 하나의 (제목, 설명). variant 는 같은 종류 중 몇 번째인지(0부터)."""
    key = template_key(candidate)
    values = fill_values(candidate)
    title, body = _TEMPLATES[key][variant % 3]
    if _needed(body) <= values.keys():
        body = body.format(**values)
        if len(body) <= BODY_MAX:
            return title, body
    return _GENERIC[key][variant % 3]


def rule_texts(candidates: list[Candidate]) -> list[tuple[str, str]]:
    """여러 후보의 (제목, 설명). 같은 종류가 겹치면 나온 순서대로 다른 문구를 쓴다."""
    seen: dict[str, int] = {}
    texts = []
    for c in candidates:
        key = template_key(c)
        texts.append(rule_text(c, seen.get(key, 0)))
        seen[key] = seen.get(key, 0) + 1
    return texts


def rule_summary(chosen: list[Candidate]) -> tuple[str, str]:
    """(headline, footer). 고칠 점이 없으면 칭찬, 있으면 첫 번째 항목을 짚는다."""
    if not chosen:
        return NO_ISSUE_SUMMARY
    return "전체적으로 무난했어요.", f"{KIND_NAMES[chosen[0].kind]} 부분만 다듬어 보세요."


def rule_selection(candidates: list[Candidate], limit: int = 3) -> list[Candidate]:
    """후보가 limit 개 이하면 전부, 넘으면 종류가 겹치지 않게 앞에서부터 고르고 모자라면 순서대로 채운다."""
    if len(candidates) <= limit:
        return list(candidates)
    chosen: list[Candidate] = []
    kinds: set[str] = set()
    for c in candidates:
        if len(chosen) < limit and c.kind not in kinds:
            chosen.append(c)
            kinds.add(c.kind)
    for c in candidates:
        if len(chosen) < limit and c not in chosen:
            chosen.append(c)
    return sorted(chosen, key=lambda c: c.id)  # 후보 순서가 곧 우선순위
