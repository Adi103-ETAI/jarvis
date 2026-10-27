"""Memory -- the Phase 5 layer: facts + transcript + recall + compaction.

Architecture build order #4: "what did I say about X" must be answerable,
and state must survive compaction. Design follows decision D5 (borrowed
shapes, not code) and decision D6 (this phase):

    Letta split    core memory (always-in-context) vs archival memory
                   (searched on demand). Here: FactStore = core, Archive =
                   archival. Long-term identity lives in facts tagged
                   identity/preference and rides into every relevant turn.
    Hermes shape   memory that later phases will turn into skills and
                   procedural memory. v0 stores; it does not yet learn.
    The law        the active layer never runs a model to decide what to
                   remember. Facts are written because the SUBJECT typed
                   them ("remember ...") -- capture over extraction. The
                   archive is verbatim transcript. Recall is deterministic
                   search, not generation. Nothing here can execute code;
                   memory widens context, never authority.

Storage is the house style: append-only JSONL, replayed into state, zero
external dependencies. Forgetting appends a tombstone; history is never
rewritten. A truncated final line (crash mid-write) is ignored on replay.

Package layout: facts.py (FactStore, core), archive.py (Archive,
archival), recall.py (Recall + scoring), prompt.py (facts_prompt_block),
archiver.py (TurnArchiver), compactor.py (Compactor), common.py (shared
constants/helpers). This __init__ keeps `jarvis.memory` stable.
"""

from jarvis.memory.archive import Archive
from jarvis.memory.common import CORE_CATEGORIES, FACT_SOURCES, tokenize
from jarvis.memory.archiver import TurnArchiver
from jarvis.memory.compactor import Compactor
from jarvis.memory.facts import FactStore
from jarvis.memory.prompt import facts_prompt_block
from jarvis.memory.recall import Recall

__all__ = ["Archive", "Compactor", "FactStore", "Recall", "TurnArchiver",
           "facts_prompt_block", "CORE_CATEGORIES", "FACT_SOURCES", "tokenize"]
