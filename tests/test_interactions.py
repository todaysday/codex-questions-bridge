import json

from cqb.codex.interactions import read_interactions


def test_reads_only_matching_collaboration_inputs_within_codex_home(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "thread.jsonl"
    call = {"type": "response_item", "payload": {"type": "function_call", "namespace": "collaboration",
            "call_id": "c1", "name": "followup_task", "arguments": json.dumps({"target": "/root/ind008", "message": "hello"})}}
    output = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
              "output": "never show"}}
    path.write_text("\n".join(map(json.dumps, [call, output])) + "\n")
    assert read_interactions(str(path), str(tmp_path), {"c1"}) == ({"c1": ("followup_task", "/root/ind008")}, [])
    assert read_interactions(str(path), str(tmp_path / "other"), {"c1"}) == ({}, [])
    assert read_interactions(str(path), str(tmp_path), {"different"}) == ({}, [])


def test_incoming_agent_messages_include_only_sender_recipient_and_time(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "thread.jsonl"
    row = {"timestamp": "2026-11-09T16:43:25.896Z", "type": "response_item", "payload": {
        "type": "agent_message", "author": "/root/ind008", "recipient": "/root",
        "content": [{"type": "input_text", "text": "SECRET_TEXT"}],
    }}
    path.write_text(json.dumps(row) + "\n")
    calls, incoming = read_interactions(str(path), str(tmp_path), set())
    assert calls == {}
    assert incoming == [(1794242605896, "/root/ind008", "/root")]
    assert "SECRET_TEXT" not in repr(incoming)
