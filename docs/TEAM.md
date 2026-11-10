# TEAM.md — 팀 작업 가이드 (사람 + Claude 공용)

> **Claude에게**: 이 프로젝트에서 작업을 시작하기 전에 이 문서를 먼저 읽는다.
> 1. 사용자가 누구(BE1/BE2/BE3)인지 확인하고, **그 사람의 영역만** 수정한다.
> 2. 다른 담당의 영역은 수정하지 않는다. 필요하면 변경을 제안하고 사용자가 해당 담당에게 요청하게 한다.
> 3. 공용 파일과 마이그레이션은 아래 4장 절차를 따른다. 팀원 허락이 없으면 수정하지 않는다.
> 4. 명세에 없는 기능은 만들지 않는다.

## 1. 서비스
jjo: 발표 대본을 녹음하면 말하기 습관(쪼)을 분석해 대본 위 글자 모양으로 보여주는 AI 발표 코칭 서비스.
흐름: Google 로그인 → 약관 동의 → 대본 입력 → 전체 녹음 → 분석(백그라운드) → 결과(전체 결과 / 발화 분석 / AI 피드백) → 전체 재녹음 → 이전/현재 회차 비교

스택: Python, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, PostgreSQL. 단일 서버, 음성 분석은 백그라운드 작업.
녹음 파일은 파일 저장소에 두고 DB에는 경로만 저장. 분석 결과와 피드백은 JSONB로 저장.

## 2. 담당과 영역

| 담당 | 이름 | 영역 (이 폴더/파일만 수정) | 책임 |
| --- | --- | --- | --- |
| BE1 | 최정훈 | `app/domains/auth/`, `app/domains/practice/`, `app/core/security.py`, `app/core/storage.py` | 로그인/회원가입, 약관, 설정, 연습 목록, 대본 저장, 녹음 업로드, 회차 관리, 제목 자동 생성, 서버 배포·인프라 |
| BE2 | 노희윤 | `app/domains/report/` (하위 `feedback/` 포함) | 분석 요청·상태, 결과 조회(탭 3개), 이전/현재 비교, AI 피드백(Gemini) |
| BE3 | 김현수 | `app/speech/` | 대본-음성 정렬, 음높이·늘임·쉼·빠르기 추출, 본인 평균 기준 습관 판정 |
| 공용 | — | 4장 참고 | — |

노희윤(BE2)은 프로젝트 세팅과 ERD도 맡는다. 테이블 구조(ERD) 변경은 노희윤에게 먼저 알린다.

## 3. 폴더 구조와 import 규칙

```
app/
├── main.py              # 앱 실행, 라우터 등록
├── core/                # config, database, security, storage
├── contracts/           # 분석 결과 JSON 스키마 (공용 계약)
├── domains/
│   ├── auth/            # BE1
│   ├── practice/        # BE1
│   └── report/          # BE2 (feedback/ 포함)
└── speech/              # BE3
```
각 도메인은 `router.py`(엔드포인트) / `service.py`(비즈니스 로직) / `models.py`(DB 테이블) / `schemas.py`(요청·응답)로 나눈다.

| 도메인 | import 가능 | import 금지 |
| --- | --- | --- |
| auth | core | 타 도메인, speech |
| practice | core, auth | report, speech |
| report | core, contracts, auth(get_current_user·AuthRoute만), practice(service만), speech(analyze만) | practice 내부 models |
| speech | contracts | core, domains 전체 |
| contracts | 없음 | 전체 |

- `speech`는 FastAPI·DB를 모르는 순수 분석 모듈이다. 단독으로 실행하고 테스트할 수 있어야 한다.
- `report`는 `practice` 데이터를 service 함수로만 조회한다. 다른 도메인의 테이블을 직접 조회하지 않는다.
- `report`는 로그인 확인과 에러 응답 형식을 맞추기 위해 `auth`의 `get_current_user`와 `AuthRoute`만 가져다 쓴다.
- 분석 요청 흐름: 녹음 업로드(practice) → 분석 요청(report가 상태 저장 후 백그라운드 시작) → `speech.analyze(audio_path, script)` 실행 → 분석 JSON 저장 → AI 피드백 생성 → 완료. 프론트는 상태를 주기적으로 조회한다.

## 4. 팀원 허락이 필요한 변경

아래는 **작업 전에** 팀원 전원에게 알리고 허락을 받는다. 허락 없이 수정하지 않는다. (Claude는 사용자에게 허락을 받았는지 먼저 확인한다.)

1. **공용 파일**: `app/contracts/`, `app/core/config.py`, `app/core/database.py`, `app/main.py`, `pyproject.toml`, `alembic/env.py`, `.env.example`, `docs/`
2. **마이그레이션**: `alembic/versions/` 아래 새 파일 추가 및 테이블·컬럼 변경
3. **다른 담당 영역**: 원칙적으로 수정 금지. 급하면 해당 담당에게 요청한다.

`contracts/analysis_result.py`는 BE3 PoC 결과가 나온 뒤 BE2·BE3·프론트가 합의해 확정한다. 확정 전에는 형태 참고용이다.

## 5. 마이그레이션 규칙
- 마이그레이션을 만들기 전에 `main`을 최신으로 받는다. 번호가 겹치지 않게 한 번에 한 명만 만든다.
- 리비전 번호는 순서대로 `0003`, `0004`처럼 지정한다: `alembic revision --autogenerate -m "설명" --rev-id 0003`
- autogenerate는 CHECK 제약, enum 값 변경 등을 감지하지 못할 수 있다. 생성된 파일을 열어서 확인한다.
- 새 `models.py` 파일을 만들면 `alembic/env.py`에 import를 추가해야 autogenerate에 잡힌다. (공용 파일이므로 허락 필요)
- 이미 `main`에 들어간 마이그레이션 파일은 고치지 않는다. 바꿀 일이 있으면 새 마이그레이션을 추가한다.

## 6. 로컬 환경 설정

```bash
# 1. PostgreSQL (Docker)
docker run -d --name jjo-pg -e POSTGRES_USER=jjo -e POSTGRES_PASSWORD=jjo -e POSTGRES_DB=jjo -p 5432:5432 -v jjo-pg-data:/var/lib/postgresql/data postgres:17

# 2. 파이썬 환경
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"

# 3. 환경변수: .env.example을 복사해서 .env로 만들고 값을 채운다 (.env는 커밋 금지)
copy .env.example .env

# 4. DB 테이블 생성
alembic upgrade head

# 5. 서버 실행 / 테스트
uvicorn app.main:app --reload
pytest
```
- 5432 포트를 이미 쓰고 있으면 `-p 5433:5432`로 바꾸고 `.env`의 `DATABASE_URL` 포트도 맞춘다.
- `GEMINI_API_KEY`는 BE2에게 받는다. 키는 채팅, 이슈, 커밋에 올리지 않는다.

## 7. 지금 알아둘 것 (PoC 전 미확정)
- 분석 결과 JSON의 `idx` 기준, `pitchLevel` 범위, `speedLabel` 값, `habits.type` 목록은 미확정이다.
- 모든 `models.py`는 ERD 기준으로 작성되어 있다. 변경하려면 4장 절차를 따른다.
- 라우터 경로(`/auth`, `/practices`, `/reports`)는 임시로 정한 것이다. 바꿔도 되지만 프론트와 맞춘다.
