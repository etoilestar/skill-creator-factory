"""Business-agnostic bounded semantic refinement orchestration."""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateEvaluation:
    accepted: bool
    candidate: Any
    acceptance_facts: list[dict[str, Any]]
    semantic_comparable: bool = True


class BoundedRefinementFailed(RuntimeError):
    def __init__(self, *, candidate: Any, evaluation: CandidateEvaluation, attempt: int,
                 semantic_changed: bool | None) -> None:
        super().__init__("bounded refinement budget exhausted")
        self.candidate = candidate
        self.evaluation = evaluation
        self.attempt = attempt
        self.semantic_changed = semantic_changed


async def bounded_refine_candidate(
    *,
    stage: str,
    initial_candidate: Any,
    initial_evaluation: CandidateEvaluation,
    propose: Callable[[Any, dict[str, Any]], Awaitable[Any]],
    evaluate: Callable[[Any], Awaitable[CandidateEvaluation]],
    semantic_signature: Callable[[Any], Any],
    max_attempts: int,
) -> Any:
    """Refine until accepted or budget exhaustion, without interpreting facts."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    previous = initial_candidate
    evaluation = initial_evaluation
    semantic_changed: bool | None = None
    for attempt in range(1, max_attempts + 1):
        if evaluation.accepted:
            return evaluation.candidate
        feedback = {
            "acceptance_facts": evaluation.acceptance_facts,
            "progress": {"semantic_changed": semantic_changed},
            "attempt": attempt,
            "max_attempts": max_attempts,
        }
        candidate = await propose(previous, feedback)
        next_evaluation = await evaluate(candidate)
        semantic_changed = None
        if evaluation.semantic_comparable and next_evaluation.semantic_comparable:
            semantic_changed = semantic_signature(candidate) != semantic_signature(previous)
        digest = hashlib.sha256(
            json.dumps(semantic_signature(candidate), sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()[:16]
        before_issue_codes = sorted({
            str(fact.get("code")) for fact in evaluation.acceptance_facts
            if fact.get("code")
        })
        after_issue_codes = sorted({
            str(fact.get("code")) for fact in next_evaluation.acceptance_facts
            if fact.get("code")
        })
        logger.info(
            "[Creator][refinement] stage=%s attempt=%d accepted=%s semantic_changed=%s acceptance_fact_count=%d candidate_digest=%s before_issue_codes=%s after_issue_codes=%s",
            stage, attempt, next_evaluation.accepted, semantic_changed,
            len(next_evaluation.acceptance_facts), digest,
            before_issue_codes, after_issue_codes,
        )
        if next_evaluation.accepted:
            return next_evaluation.candidate
        if attempt == max_attempts:
            raise BoundedRefinementFailed(
                candidate=candidate, evaluation=next_evaluation, attempt=attempt,
                semantic_changed=semantic_changed,
            )
        previous = candidate
        evaluation = next_evaluation
    raise AssertionError("unreachable")
