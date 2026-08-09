import pytest


@pytest.mark.asyncio
async def test_unchanged_first_candidate_reaches_second_attempt():
    from backend.services.creator.bounded_refinement import (
        BoundedRefinementFailed,
        CandidateEvaluation,
        bounded_refine_candidate,
    )

    feedback = []

    async def propose(previous, envelope):
        feedback.append(envelope)
        return previous

    async def evaluate(candidate):
        return CandidateEvaluation(False, candidate, [{"message": "still incomplete"}])

    with pytest.raises(BoundedRefinementFailed) as raised:
        await bounded_refine_candidate(
            stage="test", initial_candidate={"value": 1},
            initial_evaluation=CandidateEvaluation(False, {"value": 1}, [{"message": "missing"}]),
            propose=propose, evaluate=evaluate,
            semantic_signature=lambda candidate: candidate, max_attempts=2,
        )
    assert len(feedback) == 2
    assert feedback[1] == {
        "acceptance_facts": [{"message": "still incomplete"}],
        "progress": {"semantic_changed": False},
        "attempt": 2,
        "max_attempts": 2,
    }
    assert raised.value.attempt == 2
    assert raised.value.semantic_changed is False
