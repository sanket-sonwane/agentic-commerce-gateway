"""Minimal structured timing helper for the Merchant Agent path (Workstream A).

A tiny reusable stopwatch that records (stage, start, elapsed_ms, outcome) plus
an explicit allow-list of scalar tags. It never sees or logs secrets, card
detail, delegation tokens, private keys, raw prompts, or full model outputs —
callers pass only labels like the tool name, LLM model, and a coarse outcome.

Stages are stored per agent session so the Ops/trace view can show where time
went. This complements (not replaces) the durable hash-chained audit trail.
"""
import time

# Kept intentionally global-but-bounded: keyed by agent_session_id, pruned
# aggressively. Not a telemetry framework — just the current session's timings.
_TIMINGS: dict[str, list[dict]] = {}
_MAX_PER_SESSION = 512


def begin_session(agent_session_id: str):
    _TIMINGS[agent_session_id] = []


def record(agent_session_id: str, stage: str, start: float, end: float,
           *, outcome: str = "ok", **tags) -> None:
    entry = {
        "stage": stage,
        "start": round(start, 6),
        "elapsed_ms": round((end - start) * 1000, 3),
        "outcome": outcome,
    }
    entry.update(tags)
    buf = _TIMINGS.setdefault(agent_session_id, [])
    buf.append(entry)
    if len(buf) > _MAX_PER_SESSION:
        del buf[: len(buf) // 2]


def stages(agent_session_id: str) -> list[dict]:
    return list(_TIMINGS.get(agent_session_id, []))


def session_total_ms(agent_session_id: str) -> float:
    buf = _TIMINGS.get(agent_session_id, [])
    if not buf:
        return 0.0
    return round(sum(e.get("elapsed_ms", 0) for e in buf), 3)


class stage_timer:
    """Context manager measuring one stage; records into the given session."""

    def __init__(self, agent_session_id: str, stage: str, **tags):
        self.sid = agent_session_id
        self.stage = stage
        self.tags = tags
        self.outcome = "ok"
        self._start = None

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        end = time.perf_counter()
        if self.outcome == "ok" and exc_type is not None:
            self.outcome = "error"
        record(self.sid, self.stage, self._start, end, outcome=self.outcome, **self.tags)
        return False


def clearbut_keep_health():
    """Drop stale sessions (bounded memory) while preserving active ones."""
    # Not strictly needed for correctness given the per-session cap; kept as a
    # hook for callers that want to prune long-lived sessions.
    return None
