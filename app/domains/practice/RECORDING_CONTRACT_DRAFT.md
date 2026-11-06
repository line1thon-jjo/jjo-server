# 녹음 업로드·회차 생성 계약 초안

사용자가 제시한 구현 제안 기준이다. 기존 팀 합의나 BE3의 분석 지원 확정으로
간주하지 않는다. 프론트엔드·BE2·BE3 검토가 필요하다.

## 프론트엔드

- `POST /practices/{practice_id}/recordings`, 기존 access token 인증.
- `multipart/form-data`: 필수 `file`, 필수 `duration_ms`.
- `duration_ms`: 클라이언트가 제공하는 양의 정수. 기존 PostgreSQL INTEGER
  범위에 맞춰 `1..2147483647`을 허용한다. 서버는 실제 길이를 추출하지 않는다.
- 사용자 ID, 회차 번호, 대본 스냅샷은 서버에서 결정한다. 추가 폼 필드로
  전송해도 해당 값을 사용하지 않는다.
- 응답은 감싸기 없이 `201`과 다음 객체를 반환한다. ID는 기존 API처럼 JSON 정수다.

```json
{
  "recording_id": 1,
  "practice_id": 1,
  "attempt_no": 1,
  "duration_ms": 1234,
  "created_at": "2026-11-06T00:00:00Z"
}
```

- 서버 내부 파일 경로는 응답에 포함하지 않는다.
- `401` 인증 실패, `404` 미존재 또는 타인 연습,
  `413 AUDIO_FILE_TOO_LARGE` 또는 `MULTIPART_INPUT_TOO_LARGE`, `415 UNSUPPORTED_AUDIO_FORMAT`,
  `422 EMPTY_AUDIO_FILE` 또는 필수 필드·길이 검증 실패.
- 검증 오류는 기존 `error: {code, message, fields}` 형식이다.
  저장소·DB 예외는 기존 FastAPI의 일반 `500 Internal Server Error` 처리에 맡긴다.
- 파일 본문 한도는 정확히 `20971520`바이트다. WAV, WebM, M4A, MP3를
  헤더로 식별하며 파일명과 Content-Type을 신뢰하거나 저장 키로 사용하지 않는다.
  확장자/MIME가 없어도 헤더가 허용되면 수락한다.
- multipart 파싱 단계에서도 파일 본문을 20 MiB로 제한한다. 요청 본문 전체는
  framing/메타데이터 여유를 포함해 `20 MiB + 64 KiB`로 제한한다.
  Content-Length가 크면 읽기 전에 거부하며, 헤더가 없거나 작게 선언되어도
  스트림의 실제 바이트 수를 검사한다. 제한 초과는 `413`이다.
- 파일은 1개, 일반 폼 필드는 8개까지 허용한다. 이를 넘으면 `422`다.
  일반 필드 본문은 각 1 KiB, 파트 헤더와 Content-Type은 각 16 KiB로
  제한하며 크기 초과는 `413`이다. 필드 수에는 무시하는 추가 필드도 포함한다.
- 파서에 공급하는 청크와 영속 저장소의 복사 청크는 최대 64 KiB다.
  성공·검증 오류·DB 오류·파싱 중 오류/취소/연결 종료 시 스풀 파일을 닫는다.
  이 처리는 ASGI 서버/프록시가 이미 받은 네트워크 버퍼까지 제한하지는 않는다.

## BE2 내부 조회

```python
from app.domains.practice.service import (
    RecordingAnalysisInput,
    RecordingNotFoundError,
    get_recording_for_analysis,
)

data: RecordingAnalysisInput = get_recording_for_analysis(
    db, recording_id=recording_id, user_id=authenticated_user_id,
)
```

- 동기 SQLAlchemy `Session`, 정수 ID를 사용한다.
- 미존재와 타인 소유는 모두 `RecordingNotFoundError`다.
- 반환값은 변경 불가능한 명시적 Pydantic DTO이며 ORM 객체가 아니다.
  필드: `recording_id`, `practice_id`, `attempt_no`, `script_snapshot`,
  `audio_path`, `duration_ms`.
- `audio_path`는 분석기가 같은 서버에서 읽을 수 있는 내부 절대 경로다.
  외부 공개 URL이 아니다. report에서 practice 모델을 직접 import하지 않는다.
- `duration_ms`는 검증된 음원 길이가 아닌 클라이언트 메타데이터다.
  분석 구간 계산이나 음원 검증의 신뢰 기준으로 사용하면 안 된다.
- 실제 report 호출부와 분석 요청·상태 처리는 변경하지 않았다.

## 회차와 스냅샷

- PostgreSQL READ COMMITTED에서 연습 행을 `FOR UPDATE`로 잠그고 기존
  최대 회차 + 1을 부여한다. 첫 회차는 1이다. 기존 유일 제약도 유지한다.
- 잠금 후 서버에서 읽은 현재 대본을 그대로 저장한다. 이후 수정과 무관하다.
- 현재 모델에는 강조 구절 등 추가 스냅샷 필드가 없으므로 추가하지 않는다.
- 업로드 서비스가 파일 저장, flush, 응답 검증, commit을 수행한다.
  전용 요청 Session을 사용하며 예외 시 rollback과 파일 삭제를 수행한다.
  저장소는 부분 저장 실패도 자체적으로 정리한다.
- 파일 복사는 행 잠금 전에 수행한다. 초기 소유권 확인 후 파일을 저장하고,
  잠금 조회에서 소유권·존재를 재확인하며 최신 대본을 읽는다. 복사 중 연습이
  삭제되거나 소유자가 바뀌었다면 파일을 정리하고 미존재와 동일하게 처리한다.
- rollback/삭제/임시 파일 닫기 자체가 실패하더라도 원래 오류를 유지하고
  정리 실패를 서버 로그에 기록한다. 실제 삭제 장애 시에는 수동 복구가 필요하다.
- 파일과 DB는 하나의 원자적 트랜잭션에 참여하지 않는다. 프로세스 강제 종료,
  삭제 자체의 파일시스템 장애, DB commit 결과를 확인할 수 없는 연결 단절까지
  보장하는 복구 프로토콜/정리 작업은 이번 구현에 없다. 이 경우의 운영 복구는
  별도 합의가 필요하다.
- 회차 목록·상세 HTTP API와 AI 분석 실행·상태 관리는 이번 범위에 없다.

## 배포 전 확인 및 보류한 변경

- 연습 목록·상세 조회(PR #6)와 TXT 대본 업로드(PR #7)는 main에 머지됐다.
  기능 순서상 여섯 번째인 TXT 업로드 PR의 머지 대기는 해소됐다.
- `origin/main`의 `92cd56dde4575469c51bc72d5aee0c8a8f30bd83`을
  현재 브랜치에 병합하여 두 업로드와 조회 기능을 함께 반영했다.
  해당 main에는 `"python-multipart>=0.0.20,<0.1"` 의존성이 선언되어 있다.
  별도 의존성 추가나 선행 기능 브랜치 병합은 필요하지 않다.
- 배포 시 병합된 `pyproject.toml` 기준으로 의존성을 재설치한다.
  저장소에는 추적 중인 잠금 파일이 없다. 검증 가상환경은
  python-multipart 0.0.33을 사용한다. TXT 업로드 라우터는 앱 구성 시에도
  이 패키지가 필요하므로, 누락 환경에서 앱 기동을 보장하지 않는다.
- TEAM.md 2장에 app/core/storage.py와 app/domains/practice/가 BE1 담당으로
  명시되어 있다. 이 초안은 practice 내부의 제안 문서이며 공용 docs/나
  app/contracts/에 확정 계약을 추가하는 변경이 아니다. 현 변경 범위에는
  별도 파일 수정 허락이 필요하지 않지만, 계약 채택은 프론트엔드·BE2·BE3
  협의가 필요하다. 공용 파일이나 마이그레이션은 변경하지 않았다.
- 로컬 파일 기본 위치는 프로젝트의 `uploads/recordings/`다. 운영 시 영속
  볼륨과 백업, 비공개 접근, 저장 파일의 버전 관리 제외 정책을 확인해야 한다.
  테스트는 임시 디렉터리만 사용한다. 공용 config와 .gitignore는 수정하지 않았다.
- BE3 확인: 네 가지 제안 형식의 디코딩 지원, 브라우저별 코덱,
  전처리·변환 필요 여부. 헤더 검사만 수행하며 실제 디코딩 검증은 하지 않는다.
  특히 MP4 ftyp brand는 오디오 트랙 존재를, ID3는 MPEG 프레임 존재를
  보장하지 않는다. WebM은 EBML/DocType, WAV는 RIFF/WAVE 표식만 검사한다.
- 실제 음원 길이 검증과 분석 결과 길이의 반영 방식은 별도 합의 사항이다.

## 검증 실행

```sh
pytest -q tests app/domains/practice/tests
AUTH_TEST_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:PORT/DB pytest -q tests app/domains/practice/tests
```

기존 pytest 기본 testpaths는 `tests`만 포함하므로 practice 경로도 지정해야 한다.
PostgreSQL 테스트는 임의의 별도 스키마를 만들고 정리한다. 스키마 생성 권한이
있는 테스트 전용 DB를 사용한다. SQLite 실행은 PostgreSQL 동시성 검증이 아니다.

커밋 전 검토에서 실행한 검증:

- DB 환경변수 없이 전체 테스트: `119 passed, 77 skipped`.
- 임시 PostgreSQL 16을 연결한 전체 테스트: `192 passed, 4 skipped`.
  네 skip은 SQLite 파라미터에서 의도적으로 제외한 PostgreSQL 전용 항목이다.
- 실제 DB에서 동시 업로드 6건이 회차 1~6을 생성하는지, 유일 제약이
  중복을 거부하는지 확인했다. 각 작업의 pg_backend_pid와 txid_current를
  비교해 별도 물리 연결·트랜잭션임을 확인하고, pg_blocking_pids로 실제
  행 잠금 대기를 확인했다. 복사 중 별도 트랜잭션의 대본 수정·삭제도 검증했다.
- 저장 중 읽기/쓰기 오류, flush/commit 오류의 rollback·파일 정리와
  서버의 일반 500 응답에 내부 오류가 포함되지 않는 것을 확인했다.
- OpenAPI multipart 요청·201 응답 스키마와 `git diff --check`를 확인했다.
- 원시 ASGI 스트림으로 Content-Length 누락·허위 선언·초과 입력의 조기
  중단, 파싱 중 연결 종료·취소·I/O 오류·잘린 multipart의 스풀 닫기를 검증했다.
- 실제 업로드 성공·20 MiB 경계·오류 응답에서 스풀 파일 닫기를 확인했다.
  삭제 실패가 원래 DB/저장 오류를 덮어쓰지 않는지도 확인했다.
- 병합 전 녹음 전용 코드에서는 python-multipart import 차단 상태의 기동을
  확인했다. TXT 업로드가 포함된 병합 후 앱에는 이 결과를 적용하지 않는다.

main 병합 후 통합 검증:

- 별도 PostgreSQL 테스트 DB로 전체 테스트: `263 passed, 4 skipped`.
  네 skip은 SQLite 조합의 PostgreSQL 전용 검사이며 실제 PostgreSQL 동시성·
  행 잠금 검사는 통과했다. 기존 수동 테스트 DB의 임의 스키마만 사용했다.
- 새 프로세스의 OpenAPI에 연습 생성·목록·상세, TXT 업로드, 녹음 업로드가
  함께 존재하며, 두 multipart API의 스키마와 정적 경로 등록을 확인했다.
- `pip check`, 일반 및 staged `git diff --check`를 통과했다.
- 기존 수동 HTTP 서버는 재시작하지 않았다. 이전 실제 M4A 검증은 병합 전
  코드의 결과이며, 이번 통합 코드의 실제 HTTP·음원 재검증을 뜻하지 않는다.
