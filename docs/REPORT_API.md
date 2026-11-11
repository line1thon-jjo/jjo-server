# 분석 리포트 API 명세

> 담당: BE2 / 상태: 확정 (주소·필드 구조는 그대로, 판정 기준값과 문구만 조정 중)
> API 주소: `https://54-116-145-102.sslip.io` (해커톤 기간 배포 서버)
> Swagger: `https://54-116-145-102.sslip.io/docs` (오른쪽 위 Authorize 에 access_token 을 넣으면 직접 호출해 볼 수 있음)
> 응답 키는 auth·practice API와 같은 snake_case. 응답 예시는 실제 녹음 분석 결과
> 로그인·연습 생성·녹음 업로드 API는 `AUTH_PRACTICE_API.md` 참고

## 엔드포인트

- 모든 요청은 `Authorization: Bearer {access_token}` 헤더가 필요함
- `/audio` 만 예외. 헤더 없이 주소의 `token` 으로 확인함

| 메서드 | 주소 | 하는 일 | 성공 코드 | 응답 내용 |
| --- | --- | --- | --- | --- |
| GET | `/reports/practices/{practice_id}/attempts` | 회차 목록 | 200 | 1번 |
| POST | `/reports` 본문 `{"recording_id": 정수}` | 분석 시작. 이미 진행 중이거나 완료된 녹음이면 새로 시작하지 않고 현재 상태를 돌려줌 | 202 | 0·1번 (상태 조회와 같음) |
| GET | `/reports/{recording_id}` | 분석 상태와 헤더 정보 | 200 | 0·1번 |
| GET | `/reports/{recording_id}/overview` | 전체 결과 | 200 | 2번 |
| GET | `/reports/{recording_id}/utterance` | 발화 분석 | 200 | 3번 |
| GET | `/reports/{recording_id}/feedback` | AI 피드백 | 200 | 4번 |
| GET | `/reports/{recording_id}/audio-url` | 녹음 재생 주소 발급 | 200 | 5번 |
| GET | `/reports/{recording_id}/audio?token=` | 녹음 파일. 구간 요청(Range) 지원 | 200 / 206 | 음성 파일 |

### 호출 흐름

1. 녹음 업로드: practice API `POST /practices/{practice_id}/recordings` 응답의 `recording_id` 를 받음
2. `POST /reports` 로 분석 시작
3. `GET /reports/{recording_id}` 를 3초 간격으로 조회. 녹음 1분 기준 20~30초 걸림
4. `COMPLETED` 면 결과 조회(overview / utterance / feedback), `FAILED` 면 `failure.code` 확인
5. 실패한 녹음은 `POST /reports` 를 다시 부르면 처음부터 다시 분석함

### 오류 응답

```json
{
  "error": {
    "code": "REPORT_NOT_COMPLETED",
    "message": "REPORT_NOT_COMPLETED",
    "fields": []
  }
}
```

- `message` 대신 `code` 로 구분함
- `fields` 는 422일 때 문제가 된 필드 이름 목록. 그 밖에는 빈 목록

| 상태 | 코드 | 의미 | 대응 |
| --- | --- | --- | --- |
| 401 | ACCESS_CREDENTIAL_INVALID | 로그인 토큰이 없거나 잘못됨·만료됨 | 다시 로그인 |
| 401 | AUDIO_TOKEN_INVALID | 재생 주소의 token 이 잘못됨. 30분 지나면 만료 | `/audio-url` 로 새 주소를 받음 |
| 404 | PRACTICE_NOT_FOUND | 연습이 없거나 본인 것이 아님 | - |
| 404 | RECORDING_NOT_FOUND | 녹음이 없거나 본인 것이 아님 | - |
| 404 | REPORT_NOT_FOUND | 아직 분석을 시작하지 않은 녹음 | `POST /reports` 로 시작 |
| 404 | AUDIO_FILE_NOT_FOUND | 서버에 녹음 파일이 없음 | - |
| 409 | REPORT_NOT_COMPLETED | 분석 중이거나 실패한 상태에서 결과(overview / utterance / feedback)를 조회함 | 상태 조회로 확인 |
| 422 | VALIDATION_ERROR | 요청 형식이 잘못됨 (본문 누락, 주소의 번호가 정수가 아님, token 누락 등) | - |
| 500 | REPORT_DATA_INVALID | 저장된 분석 결과를 읽을 수 없음 (서버 문제) | - |
| 503 | AUTH_CONFIGURATION_ERROR | 서버 인증 설정 문제 (서버 문제) | - |

## 읽는 법

- **글자 번호**: 본문(`spoken_text`)에서 글자의 위치. 0부터 세고 공백·줄바꿈도 한 글자로 셈. 구간은 시작과 끝 번호를 모두 포함
  - 예: 본문이 "카카오톡은 위챗과"이면 카=0, 카=1, 오=2, 톡=3, 은=4, (공백)=5, 위=6, 3~4 구간 = "톡은"
- **시간**은 밀리초(1000 = 1초). 예: 800 = 0.8초
- 해당하는 것이 없으면 **빈 목록**으로 옴. 필드 자체가 없는 경우는 따로 적음
- 본문은 **말한 내용 기준**. 대본을 그대로 읽었으면 대본과 같고, 바꿔 말하거나 덧붙였으면 그 내용이 반영됨

## 0. 리포트 상태

| 필드 | 값 | 의미 |
| --- | --- | --- |
| status | PENDING / ANALYZING / COMPLETED / FAILED | 대기 / 분석 중 / 완료 / 실패 |
| failure.code | 아래 실패 코드 중 하나 | FAILED 일 때만 있음 |
| warnings | 아래 경고 코드 목록 (비어 있을 수 있음) | 분석은 성공했지만 알아둘 점. COMPLETED 일 때만 값이 있음 |

PENDING 또는 ANALYZING 이면 일정 간격(권장 3초)으로 다시 조회하고, COMPLETED 또는 FAILED 가 되면 멈춤.

실패 코드:

| 코드 | 의미 |
| --- | --- |
| EMPTY_SCRIPT | 대본이 비었거나 너무 짧음 (한글 3글자 미만) |
| AUDIO_NOT_FOUND | 녹음 파일이 없음 |
| AUDIO_DECODE_FAILED | 녹음 파일을 읽을 수 없음 |
| AUDIO_TOO_SHORT | 녹음이 1.5초 미만 |
| AUDIO_TOO_LONG | 녹음이 10분 초과 |
| NO_SPEECH | 말소리가 인식되지 않음 |
| ALIGNMENT_LOW_COVERAGE | 대본과 녹음이 너무 달라 분석 불가 |
| ENGINE_UNAVAILABLE | 분석 엔진 문제 (서버 문제) |
| INTERNAL | 알 수 없는 서버 오류 |

경고 코드:

| 코드 | 의미 |
| --- | --- |
| SCRIPT_DEVIATION | 대본 일치율이 70% 미만 (대본과 많이 다르게 말함) |
| PARTIAL_ALIGNMENT | 대본과 대응된 글자가 85% 미만 (일부만 분석됨) |
| PITCH_UNAVAILABLE | 음높이를 잡지 못함 (marks 에 pitch 가 없을 수 있음) |

## 1. 헤더 정보와 회차 목록

리포트는 **녹음(회차) 하나당 하나**. 회차마다 `recording_id` 가 다르고, 그 번호로 2~5번을 조회함.

상태 조회(`GET /reports/{recording_id}`, `POST /reports`) 응답:

| 필드 | 값 | 예시 |
| --- | --- | --- |
| recording_id | 정수 | 42 |
| practice_title | 문자열 | "완벽하지 않아도 괜찮은 편안한 발표의 시작" |
| recorded_at | 날짜·시각 (ISO 8601, 시간대 포함). UTC 기준 | 2026-11-11T16:37:54.268652Z (한국 시간 11월 12일 01:37) |
| attempt_no | 1 이상의 정수 | 1 |
| duration_ms | 정수, 1500 ~ 600000 | 56640 |

```json
{
  "recording_id": 42,
  "status": "COMPLETED",
  "practice_title": "완벽하지 않아도 괜찮은 편안한 발표의 시작",
  "recorded_at": "2026-11-11T16:37:54.268652Z",
  "attempt_no": 1,
  "duration_ms": 56640,
  "warnings": []
}
```

- `failure` 는 FAILED 일 때만 있음. 예: `"failure": { "code": "NO_SPEECH" }`
- `warnings` 는 COMPLETED 일 때만 값이 있음. 그 밖에는 빈 목록

회차 목록(`GET /reports/practices/{practice_id}/attempts`, 회차 번호 오름차순):

| 필드 | 값 | 설명 |
| --- | --- | --- |
| attempts[].recording_id | 정수 | 그 회차 녹음의 ID |
| attempts[].attempt_no | 1 이상의 정수 | 회차 번호 |
| attempts[].recorded_at | 날짜·시각 (ISO 8601, 시간대 포함). UTC 기준 | 녹음 시각 |
| attempts[].status | PENDING / ANALYZING / COMPLETED / FAILED / null | 그 회차의 분석 상태. 분석 중이거나 실패한 회차도 목록에 나옴. 아직 분석을 시작하지 않은 회차는 null |

```json
{
  "attempts": [
    { "recording_id": 42, "attempt_no": 1, "recorded_at": "2026-11-11T16:37:54.268652Z", "status": "COMPLETED" },
    { "recording_id": 57, "attempt_no": 2, "recorded_at": "2026-11-11T16:52:10.402118Z", "status": "ANALYZING" },
    { "recording_id": 58, "attempt_no": 3, "recorded_at": "2026-11-11T16:55:31.077953Z", "status": null }
  ]
}
```

예시의 2·3회차는 상태별 모양을 보여 주려고 만든 값.

## 2. 전체 결과 (`/overview`)

| 필드 | 값·범위 | 의미 | 없을 때 |
| --- | --- | --- | --- |
| summary.headline | 문자열, 보통 20자 안쪽 (드물게 넘을 수 있음) | 총평 첫 줄 | 항상 있음 |
| summary.footer | 문자열, 보통 30자 안쪽 (드물게 넘을 수 있음) | 다음 연습에서 다듬을 점 | 항상 있음 |
| speed.fast | 0 이상의 정수 | 본인 평균보다 빠른 구절 수 | - |
| speed.normal | 0 이상의 정수 | 보통 속도 구절 수 | - |
| speed.slow | 0 이상의 정수 | 본인 평균보다 느린 구절 수 | - |
| speed.measured | fast + normal + slow | 속도를 잰 구절 수. 0이면 속도를 잴 수 있는 구절이 없음 | - |
| habits[].type | 습관 코드 7종 중 하나 (아래 표) | 반복된 말하기 습관 | 빈 목록 = 반복된 습관 없음 |
| habits[].count | 2 이상의 정수 | 그 습관이 나온 횟수 | - |

- habits 는 횟수가 많은 순. 같은 종류가 2번 이상일 때만 나오며 최대 7개
- 빠름·느림 기준은 3-2의 paces 와 같음

습관 코드:

| type | 의미 |
| --- | --- |
| END_RISE | 문장 끝에서 음높이가 올라감 |
| END_STRETCH | 문장 끝 글자를 길게 끎 |
| END_FADE | 문장 끝에서 목소리가 작아짐 |
| MID_STRETCH | 문장 중간에서 글자를 길게 늘임 |
| MID_PAUSE | 문장 중간에서 길게 쉼 |
| LONG_PAUSE | 숨 고를 자리에서 평소보다 오래 쉼 |
| NO_BREAK | 문장이 끝났는데 쉬지 않고 이어 말함 |

```json
{
  "summary": {
    "headline": "전체적으로 무난했어요.",
    "footer": "‘잘’ 소리를 고르게 다듬어 보세요"
  },
  "speed": { "fast": 0, "normal": 17, "slow": 0, "measured": 17 },
  "habits": [
    { "type": "MID_STRETCH", "count": 2 },
    { "type": "NO_BREAK", "count": 2 }
  ]
}
```

## 3. 발화 분석 (`/utterance`)

### 3-1. 본문과 글자 표시

| 필드 | 값·범위 | 의미 | 없을 때 |
| --- | --- | --- | --- |
| spoken_text | 문자열 | 본문. 모든 글자 번호는 이 문자열 기준 | 항상 있음 |
| marks[].idx | 글자 번호 | 표시가 붙는 글자 | marks 가 빈 목록 |
| marks[].pitch | 1, 2, 6, 7 중 하나 | 본인 평균보다 2반음 이상 낮게(1·2) 또는 높게(6·7) 말한 글자. 1과 7은 4반음 이상 차이 | 이 필드 자체가 없음 |
| marks[].stretch | 2.2 이상의 숫자 (문장 끝 글자는 2.6 이상, 상한 없음) | 본인 평균 길이의 몇 배로 늘였는지 | 이 필드 자체가 없음 |

- 평범한 글자는 marks 에 아예 없음
- 한 글자가 pitch 와 stretch 를 둘 다 가질 수도, 하나만 가질 수도 있음

### 3-2. 속도와 쉼

| 필드 | 값·범위 | 의미 | 없을 때 |
| --- | --- | --- | --- |
| paces[].start_idx, end_idx | 글자 번호 | 구절의 시작과 끝 | paces 가 빈 목록 |
| paces[].label | FAST 또는 SLOW | 본인 평균보다 1.4배 이상 빠름 / 0.7배 이하로 느림. 보통 속도 구절은 없음 | - |
| pauses[].after_idx | 글자 번호 | 이 글자 뒤에서 쉼 | pauses 가 빈 목록 |
| pauses[].duration_ms | 400 이상의 정수 | 쉰 길이. 문장 중간은 400 이상, 숨 고를 자리는 700 이상이면서 평소의 2배 이상일 때만 나옴 | - |

구절은 4글자 이상이고 문장부호와 쉼을 기준으로 나뉨. 속도를 못 잰 구절은 paces 에 없어서, paces 가 본문 전체를 덮지 않을 수 있음.

### 3-3. 대본과 다르게 말한 곳

| 필드 | 값·범위 | 의미 | 없을 때 |
| --- | --- | --- | --- |
| replaced[].start_idx, end_idx | 글자 번호 | 대본과 다르게 말한 곳 (본문에는 말한 내용으로 반영됨) | 빈 목록 (대본대로 읽으면 대부분 비어 있음) |
| replaced[].spoken | 문자열, 3글자 이상 | 말한 단어 | - |
| replaced[].script_text | 문자열 | 원래 대본의 해당 부분 | 대응하는 대본 글자를 못 찾으면 이 필드 자체가 없음 |
| added[].start_idx, end_idx | 글자 번호 | 대본에 없는데 덧붙여 말한 곳 (본문에 포함됨) | 빈 목록 |
| added[].spoken | 문자열, 3글자 이상 | 덧붙인 말 | - |
| skipped[].start_idx, end_idx | 글자 번호, 연속 2글자 이상 | 말하지 않고 건너뛴 곳 (본문에 대본 글자가 그대로 남아 있음) | 빈 목록 |

### 3-4. 구절별 재생 구간

| 필드 | 값·범위 | 의미 |
| --- | --- | --- |
| segments[].start_idx, end_idx | 글자 번호 | 구절의 위치. 본문 전체를 빈틈없이 나눔 |
| segments[].start_ms, end_ms | 정수 (밀리초) | 그 구절의 재생 시작·끝 시각 |

### 응답 예시

실제 응답에서 각 목록의 앞 1~2개만 옮김. 대본대로 읽은 녹음이라 paces·replaced·added·skipped 는 빈 목록.

```json
{
  "spoken_text": "저는 어릴 때부터 사람들 앞에서 말하는 일이 유난히 어려웠는데 그 이유를 곰곰이 생각해 보면 ...",
  "marks": [
    { "idx": 8, "pitch": 2 },
    { "idx": 11, "pitch": 6 }
  ],
  "paces": [],
  "pauses": [
    { "after_idx": 388, "duration_ms": 420 }
  ],
  "segments": [
    { "start_idx": 0, "end_idx": 34, "start_ms": 2040, "end_ms": 5660 },
    { "start_idx": 35, "end_idx": 66, "start_ms": 6190, "end_ms": 9420 }
  ],
  "replaced": [],
  "added": [],
  "skipped": []
}
```

## 4. AI 피드백 (`/feedback`)

피드백 문구는 Gemini 가 쓰고, 실패하면 규칙 문구로 대신 채움. 어느 쪽이든 응답 형식은 같음. 같은 녹음이라도 분석할 때마다 문구는 달라질 수 있음.

| 필드 | 값·범위 | 의미 | 없을 때 |
| --- | --- | --- | --- |
| items | 0~3개, 배열 순서 = rank 순서 | 다음 연습에서 고칠 점 | 빈 목록 = 고칠 점이 발견되지 않음 |
| items[].rank | 1, 2, 3 | 우선순위. 1이 가장 먼저 연습할 항목 | - |
| items[].title | 문자열, 20자 이내 | 무엇을 연습할지 | - |
| items[].quote | 문자열, 40자 이내 | 문제가 나온 구절. 길면 문제 위치가 들어가게 잘라냄. start_idx~end_idx 구간의 본문과 같고 줄바꿈만 공백으로 바뀜 | - |
| items[].body | 문자열, 90자 이내 | 무슨 일이 있었고 어떻게 연습할지 | - |
| items[].start_ms, end_ms | 정수 (밀리초) | 그 구절의 재생 구간 | - |
| items[].start_idx, end_idx | 글자 번호 | 본문에서 그 구절의 위치 | - |
| items[].kind | 습관 코드 7종 / PACE / PAUSE | 어떤 분석에서 나온 피드백인지. PACE 는 빠르거나 느린 구절, PAUSE 는 긴 쉼 | - |

```json
{
  "items": [
    {
      "rank": 1,
      "title": "중간에 늘이지 않기",
      "quote": "아마도 친구 앞에서는 잘 보이고 싶은 마음보다 그냥 전하고 싶은 마음이",
      "body": "‘잘’ 소리가 길게 늘어졌어요. 다음 말을 고를 시간이 필요하면 늘이지 말고 잠깐 멈춰 보세요.",
      "start_ms": 29690,
      "end_ms": 33680,
      "start_idx": 229,
      "end_idx": 267,
      "kind": "MID_STRETCH"
    },
    {
      "rank": 2,
      "title": "마침표에서 한 번 쉬기",
      "quote": "어렵지 않아요. 그저",
      "body": "‘않아요’에서 문장이 끝났는데 쉬지 않고 다음 말로 넘어갔어요. 마침표마다 숨 한 번씩 넣어 보세요.",
      "start_ms": 48120,
      "end_ms": 48990,
      "start_idx": 378,
      "end_idx": 388,
      "kind": "NO_BREAK"
    },
    {
      "rank": 3,
      "title": "늘이는 대신 잠깐 쉬기",
      "quote": "그 이유를 곰곰이 생각해 보면 틀리면 어쩌지 하는 걱정이",
      "body": "‘걱정이’의 ‘이’ 소리를 끌면서 말이 이어졌어요. 글자마다 길이를 비슷하게 맞춰 보세요.",
      "start_ms": 6190,
      "end_ms": 9420,
      "start_idx": 35,
      "end_idx": 65,
      "kind": "MID_STRETCH"
    }
  ]
}
```

## 5. 재생 (`/audio-url`)

| 필드 | 값 | 설명 |
| --- | --- | --- |
| audio_url | 주소 문자열 | 녹음 재생 주소. **API 주소 기준 상대 경로**라 앞에 API 주소를 붙여서 씀. 로그인 헤더 없이 열 수 있고 구간 요청(Range) 지원 |
| expires_in_seconds | 1800 | 30분간 유효 |

```json
{
  "audio_url": "/reports/42/audio?token=eyJhbGciOiJIUzI1NiIs...(생략)",
  "expires_in_seconds": 1800
}
```

- 30분이 지나면 AUDIO_TOKEN_INVALID 가 오므로 `/audio-url` 로 새 주소를 받음
- 구간 재생에는 segments 와 items 의 start_ms, end_ms 를 사용

## 6. 유의

- 글자 수: 피드백 제목 20자·설명 90자는 서버가 보장. 총평(headline 20자, footer 30자)은 드물게 넘을 수 있음
- marks 의 음높이 기준(현재 2반음)은 4반음 안팎으로 조정 검토 중. 필드 구조는 그대로
- 구절·습관 판정 기준값도 실제 녹음 결과를 보며 조정함. 필드 구조는 그대로
- BE3가 분석 엔진 정확도를 계속 개선 중이라 같은 녹음이라도 판정 결과가 달라질 수 있음
- 회차 사이 비교 수치는 현재 제공하지 않음
