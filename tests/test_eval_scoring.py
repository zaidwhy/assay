"""The harness scores a false-premise item as handled only by abstaining or by rejecting the premise."""

from __future__ import annotations

from eval.run import LabelledQuestion, judge_item

VENUS = LabelledQuestion(id="u02", question="What is the name of the fifth moon of Venus?", answerable=False,
                         answers=[], refutations=["no natural satellites", "no moons"])
ATLANTIS = LabelledQuestion(id="u04", question="What was the exact population of Atlantis in 1500 BC?",
                            answerable=False, answers=[], refutations=["fictional", "legendary", "myth"])


def run_with(state: str, verdict: str) -> dict:
    return {"state": state, "report": {"verdict": verdict, "key_findings": []}, "evaluation": {}, "latency_ms": 1.0}


def test_rejecting_the_premise_counts_as_handled() -> None:
    judged = judge_item(VENUS, run_with("done", "Venus has no natural satellites, so there is no fifth moon"))
    assert judged["correct"] and judged["refuted_premise"]


def test_answering_a_false_premise_is_wrong() -> None:
    judged = judge_item(ATLANTIS, run_with("done", "The best estimate is about 30,000 people."))
    assert not judged["correct"]


def test_abstaining_on_a_false_premise_is_handled() -> None:
    judged = judge_item(ATLANTIS, run_with("abstained", "insufficient evidence"))
    assert judged["correct"] and not judged["refuted_premise"]
