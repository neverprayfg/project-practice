from __future__ import annotations

from collections import defaultdict
from typing import Any

MEASURED_SEGMENTS = (
    "retrieval",
    "model_generation",
    "compile",
    "trial_generation",
    "validation",
    "review",
)


def summarize_agent4_timings(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        run_id = event.get("run_id")
        if isinstance(run_id, str) and run_id:
            grouped[run_id].append(event)

    summaries: list[dict[str, Any]] = []
    for run_id, run_events in grouped.items():
        segment_totals = {segment: 0.0 for segment in MEASURED_SEGMENTS}
        segment_calls = {segment: 0 for segment in MEASURED_SEGMENTS}
        round_totals: dict[int, dict[str, float]] = defaultdict(
            lambda: {segment: 0.0 for segment in MEASURED_SEGMENTS}
        )
        workflow_total_ms = 0.0
        for event in run_events:
            segment = event.get("segment")
            duration_ms = _duration(event.get("duration_ms"))
            if segment == "workflow_total":
                workflow_total_ms = max(workflow_total_ms, duration_ms)
                continue
            if segment not in segment_totals:
                continue
            segment_totals[segment] += duration_ms
            segment_calls[segment] += 1
            round_index = event.get("round")
            if isinstance(round_index, int) and round_index > 0:
                round_totals[round_index][segment] += duration_ms

        measured_ms = sum(segment_totals.values())
        segments = {
            segment: {
                "duration_ms": round(segment_totals[segment], 3),
                "calls": segment_calls[segment],
                "share_of_measured_percent": _percent(
                    segment_totals[segment], measured_ms
                ),
                "share_of_workflow_percent": _percent(
                    segment_totals[segment], workflow_total_ms
                ),
            }
            for segment in MEASURED_SEGMENTS
        }
        rounds = []
        for round_index in sorted(round_totals):
            values = round_totals[round_index]
            round_measured_ms = sum(values.values())
            rounds.append(
                {
                    "round": round_index,
                    "measured_ms": round(round_measured_ms, 3),
                    "segments": {
                        segment: round(values[segment], 3)
                        for segment in MEASURED_SEGMENTS
                    },
                }
            )
        summaries.append(
            {
                "run_id": run_id,
                "workflow_total_ms": round(workflow_total_ms, 3),
                "measured_segments_ms": round(measured_ms, 3),
                "unaccounted_ms": round(max(workflow_total_ms - measured_ms, 0.0), 3),
                "segments": segments,
                "rounds": rounds,
            }
        )

    return summaries


def _duration(value: Any) -> float:
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return 0.0


def _percent(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(part * 100 / whole, 2)
