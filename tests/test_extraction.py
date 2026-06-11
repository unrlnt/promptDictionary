"""Offline unit tests for the refinement parser/mapper. No network, no DB.

Feeds canned LLM JSON through the parser and asserts the right refinement rows,
that unknown kinds are dropped, and that code-fenced JSON is handled.
"""
from __future__ import annotations

from promptdict.extraction import build_extraction_prompt, refinement_rows


def test_forgotten_and_specified_upfront_mapping():
    text = """{
      "forgotten": [
        {"kind": "format", "turn": 2, "note": "make it a table"},
        {"kind": "audience", "turn": 3, "note": "specify the audience"}
      ],
      "specified_upfront": [
        {"kind": "length", "note": "ten items requested"}
      ]
    }"""
    rows = refinement_rows(text, cluster_id=None)

    # forgotten -> in_first_prompt False, turn_index = the LLM's 1-based turn
    assert ("format", 2, False, "make it a table", None) in rows
    assert ("audience", 3, False, "specify the audience", None) in rows
    # specified_upfront -> in_first_prompt True, turn_index = 1
    assert ("length", 1, True, "ten items requested", None) in rows
    assert len(rows) == 3


def test_unknown_kinds_dropped():
    # "vibes" isn't canonical; "citation" was an off-the-cuff example (canonical: sources).
    text = ('{"forgotten":[{"kind":"vibes","turn":2,"note":"x"},'
            '{"kind":"format","turn":2,"note":"y"}],'
            '"specified_upfront":[{"kind":"citation","note":"z"}]}')
    rows = refinement_rows(text, cluster_id=None)
    assert [r[0] for r in rows] == ["format"]


def test_code_fenced_json_handled():
    text = ('```json\n'
            '{"forgotten":[],"specified_upfront":[{"kind":"tone","note":"formal"}]}\n'
            '```')
    rows = refinement_rows(text, cluster_id="cid-1")
    assert rows == [("tone", 1, True, "formal", "cid-1")]


def test_cluster_id_propagates_and_malformed_turn_skipped():
    text = ('{"forgotten":[{"kind":"depth","turn":"oops","note":"go deeper"},'
            '{"kind":"scope","turn":4,"note":"focus on X"}],'
            '"specified_upfront":[]}')
    rows = refinement_rows(text, cluster_id="c9")
    # The non-integer turn is dropped; the valid one keeps the cluster_id.
    assert rows == [("scope", 4, False, "focus on X", "c9")]


def test_empty_arrays_yield_no_rows():
    assert refinement_rows('{"forgotten":[],"specified_upfront":[]}', None) == []


def test_prompt_numbers_turns_and_keeps_json_shape():
    prompt = build_extraction_prompt(["first request", "make it shorter"])
    assert "1. first request" in prompt
    assert "2. make it shorter" in prompt
    # The literal JSON shape in the instructions must survive rendering.
    assert '"forgotten"' in prompt and '"specified_upfront"' in prompt
