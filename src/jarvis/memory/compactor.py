"""Compactor -- the compaction plugin: nothing durable dies with the window.

Snapshot -> kernel compacts (the model summarizes in-kernel, where
thinking belongs) -> durable facts re-injected as background.
"""

from __future__ import annotations

from jarvis.memory.archive import Archive
from jarvis.memory.facts import FactStore
from jarvis.memory.prompt import facts_prompt_block


class Compactor:
    """The compaction plugin: nothing durable dies with the context window.

    Sequence per run: snapshot the message count -> kernel compacts the
    session (the model summarizes in-kernel, where thinking belongs) ->
    durable facts are re-injected as background. After this, identity and
    preferences survive even though old messages are gone.
    """

    def __init__(self, kernel, facts: FactStore, archive: Archive, journal):
        self._kernel = kernel
        self._facts = facts
        self._archive = archive
        self._journal = journal

    def run(self, session_id: str) -> dict:
        messages = self._kernel.messages(session_id) or []
        before = len(messages)
        self._archive.append_compaction(session_id=session_id, messages_before=before)
        self._journal.add("compact-started", session_id=session_id,
                          messages_before=before)
        self._kernel.compact(session_id)
        try:
            self._kernel.wait(session_id, timeout=60.0)
        except Exception:                     # noqa: BLE001 — compaction is async; proceed
            pass
        block = facts_prompt_block(self._facts.facts())
        injected = 0
        if block:
            self._kernel.inject_context(session_id, block,
                                        description="durable memory re-injection")
            injected = block.count("\n- [")
        self._journal.add("compact-finished", session_id=session_id,
                          messages_before=before, facts_injected=injected)
        return {"messages_before": before, "facts_injected": injected}
