# 로그인·연습 API 명세

> 담당: BE1 (문서는 BE2가 코드 기준으로 정리, BE1 확인 필요)
> API 주소: `https://54-116-145-102.sslip.io` / Swagger: `https://54-116-145-102.sslip.io/docs`
> 분석 결과 API는 `REPORT_API.md` 참고. 오류 응답 형식도 같음
> 응답 키는 snake_case. 시각은 ISO 8601, 시간대 포함

## 엔드포인트

| 메서드 | 주소 | 로그인 | 하는 일 | 성공 코드 |
| --- | --- | --- | --- | --- |
| POST | `/auth/google` | 필요 없음 | 구글 로그인. 가입한 사용자면 access_token 발급, 처음이면 가입 토큰 발급 | 200 |
| POST | `/auth/signup` | 필요 없음 (가입 토큰 사용) | 약관 동의로 가입 완료, access_token 발급 | 201 |
| GET | `/auth/me` | 필요 | 내 정보와 다음 단계 | 200 |
| POST | `/auth/onboarding/complete` | 필요 | 온보딩 완료 처리 (본문 없음) | 200 |
| DELETE | `/auth/me` | 필요 | 회원 탈퇴 (본문 없음) | 204 |
| GET | `/practices` | 필요 | 내 연습 목록 (최신순) | 200 |
| POST | `/practices` | 필요 | 대본으로 연습 생성 (제목 자동 생성) | 201 |
| GET | `/practices/{practice_id}` | 필요 | 연습 하나 조회 | 200 |
| POST | `/practices/script-upload` | 필요 | TXT 파일에서 대본 글자만 꺼내기 (저장하지 않음) | 200 |
| POST | `/practices/{practice_id}/recordings` | 필요 | 녹음 업로드 (분석은 하지 않음) | 201 |

- "필요"는 `Authorization: Bearer {access_token}` 헤더를 뜻함
- access_token 은 1시간(3600초) 유효. 만료되면 401 `ACCESS_CREDENTIAL_INVALID` → 구글 로그인부터 다시 함 (리프레시 토큰 없음)

## 로그인 흐름

1. 프론트에서 구글 로그인으로 **ID 토큰**을 받음
2. `POST /auth/google` 에 ID 토큰을 보냄
   - `status: "authenticated"` → 이미 가입한 사용자. `access_token` 을 저장하고 `next_step` 으로 이동
   - `status: "signup_required"` → 처음 온 사용자. `signup_token` 을 들고 약관 동의 화면으로 (`next_step: "terms"`)
3. 약관 동의 후 `POST /auth/signup` 에 `signup_token` 과 동의 내용을 보냄 → `access_token` 발급
   - `signup_token` 은 10분(600초) 유효
4. `next_step` 이 `"onboarding"` 이면 온보딩을 마친 뒤 `POST /auth/onboarding/complete` 호출 → `next_step: "practices"`
5. 앱을 다시 열면 `GET /auth/me` 로 로그인 상태와 `next_step` 확인

`next_step` 값:

| 값 | 의미 |
| --- | --- |
| terms | 약관 동의가 필요함 (가입 전) |
| onboarding | 가입은 했지만 온보딩을 마치지 않음 |
| practices | 온보딩까지 끝남 |

## 1. 로그인·가입

### POST /auth/google

요청:

```json
{ "id_token": "구글 ID 토큰" }
```

응답 (가입한 사용자):

```json
{
  "status": "authenticated",
  "next_step": "practices",
  "user": {
    "id": 1,
    "name": "노희윤",
    "email": "user@example.com",
    "profile_image_url": "https://lh3.googleusercontent.com/...",
    "onboarding_completed": true
  },
  "access_token": "eyJhbGciOiJIUzI1NiIs...(생략)",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

응답 (처음 온 사용자):

```json
{
  "status": "signup_required",
  "next_step": "terms",
  "signup_token": "eyJhbGciOiJIUzI1NiIs...(생략)",
  "signup_token_expires_in": 600
}
```

### POST /auth/signup

요청:

```json
{
  "signup_token": "POST /auth/google 에서 받은 signup_token",
  "agreements": {
    "service_terms": true,
    "privacy_collection_use": true,
    "marketing": false
  }
}
```

| 필드 | 값 | 설명 |
| --- | --- | --- |
| agreements.service_terms | true / false | 서비스 이용약관 (필수, true 여야 함) |
| agreements.privacy_collection_use | true / false | 개인정보 수집·이용 (필수, true 여야 함) |
| agreements.marketing | true / false, 기본 false | 마케팅 수신 (선택) |

- 값은 진짜 불리언이어야 함. `"true"` 같은 문자열은 422
- 응답은 `POST /auth/google` 의 `authenticated` 응답과 같은 형태 (201). 가입 직후라 `next_step` 은 `"onboarding"`

### user 객체

| 필드 | 값 | 설명 |
| --- | --- | --- |
| id | 정수 | 사용자 ID |
| name | 문자열 | 구글 계정 이름 |
| email | 문자열 | 구글 계정 이메일 |
| profile_image_url | 문자열 또는 null | 구글 프로필 사진 주소 |
| onboarding_completed | true / false | 온보딩을 마쳤는지 |

## 2. 내 정보·온보딩·탈퇴

### GET /auth/me

```json
{
  "user": { "id": 1, "name": "노희윤", "email": "user@example.com", "profile_image_url": null, "onboarding_completed": false },
  "next_step": "onboarding"
}
```

### POST /auth/onboarding/complete

본문 없음. 응답은 `{ "user": {...}, "next_step": "practices" }`. 이미 완료한 사용자가 다시 불러도 같은 응답.

### DELETE /auth/me

본문 없음. 성공하면 204 (응답 본문 없음). 사용자, 연습, 녹음, 분석 결과가 모두 지워짐.

- 성공 후에는 저장한 토큰을 지움. 같은 토큰으로 다시 부르면 401
- 503 `ACCOUNT_DELETED_CLEANUP_PENDING`: 계정은 이미 지워졌고 서버 파일 정리만 남은 상태. 탈퇴된 것으로 보고 토큰을 지움
- 503 `ACCOUNT_DELETION_UNCONFIRMED`: 삭제가 확정되지 않음. 잠시 후 다시 시도

## 3. 연습

### 연습 객체

| 필드 | 값 | 설명 |
| --- | --- | --- |
| id | 정수 | 연습 ID (`practice_id`) |
| title | 문자열, 100자 이내 | 대본으로 자동 생성한 제목. 생성에 실패하면 대본의 첫 줄(100자까지) |
| script | 문자열 | 대본 원문 (공백·줄바꿈 그대로) |
| created_at | 날짜·시각 | 만든 시각 |
| updated_at | 날짜·시각 | 마지막으로 바뀐 시각 |

### POST /practices

요청:

```json
{ "script": "안녕하세요. 오늘은 ... 발표를 시작하겠습니다." }
```

- `script`: 1~1500자. 공백만 있으면 422. 앞뒤 공백·줄바꿈은 지우지 않고 그대로 저장함
- 제목을 Gemini 로 만들어서 응답이 몇 초 걸릴 수 있음
- 응답: 연습 객체 (201)

### GET /practices

내 연습 목록. 최근에 만든 순. 연습 객체의 배열.

### GET /practices/{practice_id}

연습 객체 하나. 없거나 남의 연습이면 404 `PRACTICE_NOT_FOUND`.

### POST /practices/script-upload

TXT 파일에서 대본 글자만 꺼내서 돌려줌. 연습을 만들거나 파일을 저장하지 않음. 돌려받은 `script` 로 `POST /practices` 를 부르면 됨.

- 요청: `multipart/form-data`, 필드 `file` (확장자 `.txt`, 1MB 이하, UTF-8)
- 응답: `{ "script": "파일 내용" }`

### POST /practices/{practice_id}/recordings

녹음 파일을 올림. 올리기만 하고 분석은 하지 않음. 응답의 `recording_id` 로 `POST /reports` 를 불러 분석을 시작함 (`REPORT_API.md`).

- 요청: `multipart/form-data`
  - `file`: 녹음 파일. WAV / WebM / M4A / MP3, 20MB 이하
  - `duration_ms`: 녹음 길이(밀리초), 1 이상의 정수. 프론트가 잰 값
- 같은 연습에 올릴 때마다 회차(`attempt_no`)가 1, 2, 3… 으로 늘어남
- 녹음 시점의 대본이 녹음과 함께 저장되어 분석에 쓰임

응답 (201):

```json
{
  "recording_id": 42,
  "practice_id": 7,
  "attempt_no": 1,
  "duration_ms": 56640,
  "created_at": "2026-11-11T16:37:54.268652Z"
}
```

## 오류 코드

형식은 `{"error": {"code", "message", "fields"}}` (REPORT_API.md 와 같음). `message` 대신 `code` 로 구분함.

| 상태 | 코드 | 언제 |
| --- | --- | --- |
| 400 | REQUIRED_AGREEMENT_MISSING | 필수 약관(service_terms, privacy_collection_use)이 false. `fields` 에 빠진 항목 |
| 401 | GOOGLE_CREDENTIAL_INVALID | 구글 ID 토큰이 잘못됐거나 만료됨 |
| 401 | SIGNUP_CREDENTIAL_INVALID | 가입 토큰이 잘못됐거나 만료됨(10분) → 구글 로그인부터 다시 |
| 401 | ACCESS_CREDENTIAL_INVALID | 로그인 토큰이 없거나 잘못됐거나 만료됨, 또는 탈퇴한 계정 → 다시 로그인 |
| 404 | PRACTICE_NOT_FOUND | 연습이 없거나 남의 연습 |
| 409 | SIGNUP_ALREADY_COMPLETED | 이미 가입을 마친 계정으로 다시 가입함 |
| 409 | ACCOUNT_STATE_INVALID | 계정 상태가 비정상 (가입이 끝나지 않은 계정으로 로그인) |
| 413 | AUDIO_FILE_TOO_LARGE | 녹음 파일이 20MB 초과 |
| 413 | MULTIPART_INPUT_TOO_LARGE | 업로드 요청 전체나 필드가 너무 큼 |
| 413 | SCRIPT_FILE_TOO_LARGE | 대본 파일이 1MB 초과 |
| 415 | UNSUPPORTED_AUDIO_FORMAT | WAV / WebM / M4A / MP3 가 아닌 녹음 파일 |
| 415 | SCRIPT_FILE_TYPE_UNSUPPORTED | `.txt` 가 아닌 대본 파일 |
| 422 | VALIDATION_ERROR | 요청 형식이 틀림. `fields` 에 틀린 항목 (예: `script`, `file`, `duration_ms`) |
| 422 | EMPTY_AUDIO_FILE | 빈 녹음 파일 |
| 422 | SCRIPT_FILE_INVALID_ENCODING | 대본 파일이 UTF-8 이 아님 |
| 422 | SCRIPT_FILE_TEXT_EMPTY | 대본 파일 내용이 비어 있음 |
| 503 | GOOGLE_VERIFICATION_UNAVAILABLE | 구글 서버에 연결할 수 없음. 잠시 후 다시 시도 |
| 503 | AUTH_CONFIGURATION_ERROR | 서버 인증 설정 문제 (서버 문제) |
| 503 | UPLOAD_CONFIGURATION_ERROR | 서버 업로드 설정 문제 (서버 문제) |
| 503 | ACCOUNT_DELETION_UNCONFIRMED / ACCOUNT_DELETED_CLEANUP_PENDING | 탈퇴 처리 상태 (위 DELETE /auth/me 참고) |
