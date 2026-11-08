import asyncio
import sys

from cqb.pico import compactions
from cqb.pico.compactions import count_observed


def test_counts_only_compressions_and_summaries_of_the_mapped_session():
    lines = "\n".join((
        "Runtime event: agent.turn.start event_kind=agent.turn.start agent_id=main chat_id=pico:cqb-a session_key=sk-a",
        "Runtime event: agent.turn.start event_kind=agent.turn.start agent_id=main chat_id=pico:cqb-b session_key=sk-b",
        "Runtime event: agent.context.compress event_kind=agent.context.compress session_key=sk-a",
        "Runtime event: agent.session.summarize event_kind=agent.session.summarize session_key=sk-a",
        "Runtime event: agent.context.compress event_kind=agent.context.compress session_key=sk-b",
    ))
    assert count_observed(lines, "cqb-a") == 2
    assert count_observed(lines, "cqb-b") == 1
    assert count_observed(lines, "cqb-missing") is None


def test_cancellation_reaps_journal_process(monkeypatch):
    async def main():
        started = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*args, **kwargs):
            process = await real_spawn(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            processes.append(process)
            started.set()
            return process

        monkeypatch.setattr(compactions.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(compactions.observed_compactions("cqb-a"))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert processes[0].returncode is not None

    asyncio.run(main())
