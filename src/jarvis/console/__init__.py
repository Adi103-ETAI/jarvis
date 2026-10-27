"""jarvisd console package.

app.py -- core loop (wiring, ask + model failover, wait/cancel, dispatch).
commands.py -- the command surface (mixin). turns.py -- scan helpers.
Imports here keep the historical `jarvis.console` path stable for tests
and for `python -m jarvis`.
"""

from jarvis.console.app import (DEFAULT_MODEL, DEFAULT_WORKSPACE,
                                JarvisConsole, main)
from jarvis.console.commands import HELP, parse_model_spec
from jarvis.console.turns import (QUOTA_STATUSES, first_token_seconds,
                                  turn_failed)

__all__ = ["JarvisConsole", "main", "parse_model_spec", "HELP",
           "first_token_seconds", "turn_failed", "QUOTA_STATUSES",
           "DEFAULT_MODEL", "DEFAULT_WORKSPACE"]
