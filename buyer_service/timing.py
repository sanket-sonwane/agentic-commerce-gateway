"""Minimal structured timing helper for the Buyer Agent path (Workstream A).

Records per-stage elapsed time into a thread-local buffer that the
conversation event stream can expose. This is NOT a telemetry framework:
it is a small reusable stopwatch that never touches secrets, PII, card
details, private keys, rack raw prompts, or entire model outputs.

Sensitive fields are excluded by construction — the recorder only accepts
explicit scalar tags we choose to pass in (tool name, llm model, success,
stage). Nothing is learned from messages/tool arguments.
"""
import threading
import time

_OK_YIELD = {"ok", "success", "created", "fulfilled", "bill_ready"}


class _StageRecorder:
    """Thread-local accumulation of completed stage timings."""

    def __init__(self):
        self.local = threading.local()

    def _buf(self):
        buf = getattr(self.local, "stages", None)
        if buf is None:
            buf = []
            self.local.stages = buf
        return buf

    def begin_turn(self, request_id):
        """Start a new top-level measurement window (one user turn)."""
        self.local.request_id = request_id
        self.local.turn_start = time.perf_counter()
        buf = self._buf()
        buf.clear()

    def _note(self, stage, start, end, *, outcome=None, **tags):
        entry = {
            "stage": stage,
            "start": round(start, 6),
            "elapsed_ms": round((end - start) * 1000, 3),
        }
        if outcome is not None:
            entry["outcome"] = outcome
        entry.update(tags)
        buf = self._buf()
        buf.append(entry)
        # Only keep the most recent window so the buffer stays bounded.
        if len(buf) > 256:
            del buf[: 256 // 2]

    def record(self, stage, start, end, *, outcome=None, **tags):
        self._note(stage, start, end, outcome=outcome, **tags)

    def stages(self, request_id=None):
        """Snapshot of completed stages for the current/latest window.

        request_id is accepted for API-compat but the recorder is
        process-local and per-thread; callers pass the streamed id and we
        still return this thread's measured stages when they match.
        """
        rid = getattr(self.local, "request_id", None)
        if request_id is not None and rid is not None and rid != request_id:
            return []
        return list(getattr(self.local, "stages", None) or [])


recorder = _StageRecorder()


class stage_timer:
    """Context manager: measures one named stage and records it on exit.

    Usage:
        with stage_timer("buyer_llm", llm_model=model) as st:
            st.outcome = ...   # optional
    """

    def __init__(self, stage: str, **tags):
        self.stage = stage
        self.tags = tags
        self.outcome = None
        self._start = None

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        end = time.perf_counter()
        st = self.outcome
        if st is None and exc_type is None:
            st = "ok"
        elif st is None:
            st = "error"
        recorder.record(self.stage, self._start, end, outcome=st, **self.tags)
        # A raised exception is re-raised normally; we only measured.
        return False


def _safe_bool(result: dict) -> str | None:
    """Derive a coarse success/outcome label from a structured result without
    logging its contents (no amounts, ids, or PII)."""
    if not isinstance(result, dict):
        return None
    if result.get("error"):
        return "error"
    for key in _OK_YIELD:
        if result.get(key) is True:
            return "ok"
    return None


__all__ = ["recorder", "stage_timer", "_safe_bool"]
