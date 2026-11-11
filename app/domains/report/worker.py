"""분석 작업 실행기 (BE2 담당). 분석은 CPU를 많이 쓰므로 요청 스레드와 분리한 전용 스레드에서 돌린다.

기본은 한 번에 하나만 돌린다. 실제 녹음으로 시간·메모리를 확인한 뒤 ANALYSIS_WORKERS 를 올릴 수 있지만,
BE3 의 analyze() 가 동시 호출에 안전한지 먼저 확인해야 한다.
"""
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from app.contracts.analysis_result import AnalysisError, AnalysisResult
from app.core.database import SessionLocal
from app.domains.report import service
from app.domains.report.feedback.service import generate_feedback

logger = logging.getLogger(__name__)

ANALYSIS_WORKERS = 1

AnalyzeFn = Callable[[str, str], AnalysisResult]
SessionFactory = Callable[[], Session]
FeedbackFn = Callable[[AnalysisResult], dict[str, Any]]

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=ANALYSIS_WORKERS, thread_name_prefix="analysis")
        return _executor


def _default_analyze() -> AnalyzeFn:
    # 분석 엔진은 무거운 의존성을 쓰므로 실제로 필요할 때 불러온다.
    from app.speech.analyze import analyze

    return analyze


def run_job(
    report_id: int,
    recording: service.AnalysisInput,
    analyze_fn: AnalyzeFn | None = None,
    session_factory: SessionFactory = SessionLocal,
    feedback_fn: FeedbackFn | None = None,
) -> None:
    """리포트 하나를 분석하고 결과나 실패 사유를 저장한다. 어떤 경우에도 예외를 밖으로 내지 않는다."""
    try:
        with session_factory() as db:
            if not service.claim_for_analysis(db, report_id):
                return  # 이미 다른 작업이 맡았거나 그 사이 삭제됨

        result: AnalysisResult | None = None
        code: str | None = None
        try:
            analyze = analyze_fn or _default_analyze()
            result = analyze(recording.audio_path, recording.script_snapshot)
        except AnalysisError as exc:
            code = exc.code.value
        except Exception:
            logger.exception("Analysis crashed for report %s", report_id)
            code = service.INTERNAL_FAILURE
        if result is not None and not service.is_supported_version(result):
            logger.error("Unsupported analysis schema version %r for report %s", result.version, report_id)
            result, code = None, service.INTERNAL_FAILURE

        feedback: dict[str, Any] | None = None
        if result is not None:
            # DB 세션 밖에서 만든다(Gemini 호출은 최대 15초). 실패해도 분석 결과는 COMPLETED 로 저장하고,
            # 피드백이 비면 조회할 때 규칙 피드백으로 대신한다.
            try:
                feedback = (feedback_fn or generate_feedback)(result)
            except Exception:
                logger.exception("Feedback generation crashed for report %s", report_id)

        with session_factory() as db:
            if result is not None:
                service.save_success(db, report_id, result, feedback)
            else:
                service.save_failure(db, report_id, code or service.INTERNAL_FAILURE)
    except Exception:
        # 저장 중 DB 오류 등. ANALYZING 으로 남은 리포트는 다음 조회 때 FAILED 로 정리된다.
        logger.exception("Failed to finish analysis job for report %s", report_id)
    finally:
        service.clear_active(report_id)


def enqueue(
    report_id: int,
    recording: service.AnalysisInput,
    analyze_fn: AnalyzeFn | None = None,
    session_factory: SessionFactory = SessionLocal,
    feedback_fn: FeedbackFn | None = None,
) -> Future[None]:
    """분석 작업을 대기열에 넣는다. 앞선 작업이 있으면 PENDING 으로 기다린다."""
    return _get_executor().submit(run_job, report_id, recording, analyze_fn, session_factory, feedback_fn)
