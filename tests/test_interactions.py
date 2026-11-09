import json

from cqb.codex.interactions import read_interactions


def test_reads_only_matching_collaboration_inputs_within_codex_home(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "thread.jsonl"
    call = {"type": "response_item", "payload": {"type": "function_call", "namespace": "collaboration",
            "call_id": "c1", "arguments": json.dumps({"message": "hello"})}}
    output = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
              "output": "never show"}}
    path.write_text("\n".join(map(json.dumps, [call, output])) + "\n")
    assert read_interactions(str(path), str(tmp_path), {"c1"}) == {"c1": {"message": "hello"}}
    assert read_interactions(str(path), str(tmp_path / "other"), {"c1"}) == {}
    assert read_interactions(str(path), str(tmp_path), {"different"}) == {}
