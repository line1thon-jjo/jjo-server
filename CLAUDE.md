# CLAUDE.md — jjo 백엔드

## 1. 기획
jjo: 발표 대본을 녹음하면 말하기 습관(쪼)을 분석해 대본 위 글자 모양으로 보여주는 AI 발표 코칭 서비스
- 습관: 말투(음높이, 늘임, 문장 끝 처리) / 호흡(빠르기, 쉬는 위치). 늘임·빠르기는 해당 녹음 내 본인 평균 기준
- 흐름: Google 로그인 → 약관 동의 → 대본 입력 → 전체 녹음 → 분석(백그라운드) → 결과(전체 결과 / 발화 분석 / AI 피드백) → 전체 재녹음 → 이전/현재 회차 비교
- 명세에 없는 기능은 만들지 않음

## 2. 기술 스택
Python, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, PostgreSQL
- 단일 서버. 음성 분석은 백그라운드 작업으로 실행
- 녹음 파일은 파일 저장소에 두고 DB에는 경로만 저장
- 분석 결과·피드백은 JSONB로 저장

## 팀 작업 규칙 (Claude 필독)
- 작업 전에 `docs/TEAM.md`를 읽는다. 사용자가 누구(BE1/BE2/BE3)인지 확인하고 그 사람의 영역만 수정한다.
- 다른 담당 영역은 수정하지 않는다. 공용 파일과 마이그레이션은 팀원 허락이 있어야 수정한다.
- 브랜치·커밋·PR 규칙은 `docs/CONTRIBUTING.md`를 따른다.

## 3. 폴더 구조와 담당
- app/core: 공통 설정 (config, database, security, storage)
- app/contracts/analysis_result.py: 분석 결과 JSON 스키마 (BE2·BE3·프론트 공용, 수정은 합의 후)
- app/domains/auth, practice: BE1 / app/domains/report(+feedback): BE2
- app/speech: BE3 (음성 분석 엔진)
- 각 도메인: router.py(엔드포인트) / service.py(로직) / models.py(DB 테이블) / schemas.py(요청·응답)

## 4. import 규칙
| 도메인 | import 가능 | import 금지 |
| --- | --- | --- |
| auth | core | 타 도메인, speech |
| practice | core, auth | report, speech |
| report | core, contracts, auth(get_current_user·AuthRoute만), practice(service만), speech(analyze만) | practice 내부 models |
| speech | contracts | core, domains 전체 |
| contracts | 없음 | 전체 |

- speech는 FastAPI·DB를 모르는 순수 분석 모듈 (단독 실행·테스트 가능)
- report는 practice 데이터를 service 함수로만 조회
- report는 auth에서 get_current_user, AuthRoute만 import
