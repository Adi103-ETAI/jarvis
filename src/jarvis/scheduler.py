"""T1 Scheduler — the first component that acts without a person present.

Phase 4 of the build order: minimal proactivity. One clock trigger (T1),
one user-approved routine (the 08:00 briefing), and the cycle guards that
make repetition safe:

    approval    a routine exists because the human wrote it in
                config/routines.toml. Runtime may only TIGHTEN (disable);
                it can never author, widen, or re-enable a file-disabled
                routine. Standing authority is minted by policy files.
    isolation   routine turns run on their OWN dedicated session
                ("jarvis-routines"), never mixed with console turns.
                The session is registry-owned, so Gate B's doorkeeper
                watches it like any other, with trust="scheduled" —
                and policy.toml keeps scheduled.mutating = "confirm".
    dedupe      one routine turn at a time (busy until the kernel reports
                idle on the routine session) + one fire per calendar day,
                remembered via the journal, so restarts cannot re-fire.
    budget      max_run_minutes watchdog interrupts a runaway turn (the
                routine session is JARVIS-owned, so the subject rule
                allows it); cooldown_minutes between launches.
    no loops    consecutive failures auto-disable the routine and surface
                a human-review notice. Failed routines never retry by
                themselves; the human re-enables by editing the file (or
                'routine NAME on' for a runtime-off, file-enabled one).
    DND         the quiet window suppresses ROUTINE NOTIFICATIONS only.
                Confirmation codes are never suppressed — safety outlouds
                DND, always.

The scheduler is a thread inside the console (jarvisd v0): when the
console exits, schedules do not fire. Daemon/service lifecycle is a later
phase; single-machine first (D3).
"""

from __future__ import annotations

import datetime as _dt
import threading
import time

try:  # stdlib tomllib (3.11+); the repo pins modern CPython
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

from jarvis.journal import Journal
from jarvis.kernel.adapter import KernelError

ROUTINE_SESSION_TITLE = "jarvis-routines"


def parse_daily_hhmm(spec: str) -> _dt.time | None:
    """Parse the v0 schedule format: 'daily HH:MM'. Anything else -> None."""
    parts = (spec or "").strip().split()
    if len(parts) != 2 or parts[0].lower() != "daily":
        return None
    try:
        hh, mm = parts[1].split(":")
        return _dt.time(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def in_dnd(now: _dt.time, window: str | None) -> bool:
    """True if `now` falls inside a DND window like '23:00-07:00'.

    A window may cross midnight. Empty/None/invalid -> never DND. DND only
    ever silences notifications; it never blocks a launch decision.
    """
    if not window or "-" not in window:
        return False
    try:
        a_s, b_s = window.split("-", 1)
        a = _dt.time.fromisoformat(a_s.strip())
        b = _dt.time.fromisoformat(b_s.strip())
    except ValueError:
        return False
    if a == b:
        return False
    if a < b:                       # same-day window, e.g. 13:00-15:00
        return a <= now < b
    return now >= a or now < b      # crosses midnight, e.g. 23:00-07:00


class Routine:
    """One standing job as the human wrote it. Immutable at runtime."""

    def __init__(self, name: str, schedule: str, task: str, *, enabled: bool = True,
                 max_run_minutes: float = 10.0, cooldown_minutes: float = 60.0,
                 max_consecutive_failures: int = 3, notify: bool = True):
        self.name = name
        self.schedule = schedule
        self.task = task
        self.enabled = enabled                  # file state: the approval
        self.max_run_minutes = max_run_minutes
        self.cooldown_minutes = cooldown_minutes
        self.max_consecutive_failures = max_consecutive_failures
        self.notify = notify
        # runtime-only state (may tighten, never widen)
        self.runtime_off = False
        self.consecutive_failures = 0
        self.last_started: float | None = None
        self.busy_since: float | None = None    # watchdog clock for the live turn
        self.failed_during_turn = False

    @property
    def active(self) -> bool:
        return self.enabled and not self.runtime_off

    @property
    def state(self) -> str:
        if not self.enabled:
            return "disabled-by-file"
        if self.runtime_off:
            return "off (runtime)"
        if self.busy_since is not None:
            return "running"
        if self.consecutive_failures >= self.max_consecutive_failures:
            return "auto-disabled (failures)"
        return "armed"


def load_routines(path) -> list[Routine]:
    """Read config/routines.toml. The file is the approval; nothing else is."""
    try:
        raw = tomllib.loads(open(path, "rb").read().decode("utf-8")
                            if isinstance(path, str) else path.read_text())
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 -- a broken policy file arms nothing
        return []

    d = raw.get("defaults", {}) or {}
    routines: list[Routine] = []
    for item in raw.get("routine", []):
        if not item.get("name") or not item.get("task"):
            continue
        routines.append(Routine(
            name=str(item["name"]),
            schedule=str(item.get("schedule", "")),
            task=str(item["task"]),
            enabled=bool(item.get("enabled", True)),
            max_run_minutes=float(item.get("max_run_minutes",
                                           d.get("max_run_minutes", 10.0))),
            cooldown_minutes=float(item.get("cooldown_minutes",
                                            d.get("cooldown_minutes", 60.0))),
            max_consecutive_failures=int(item.get("max_consecutive_failures",
                                                  d.get("max_consecutive_failures", 3))),
            notify=bool(item.get("notify", True)),
        ))
    return routines


class Scheduler:
    """Background thread: watches the clock, launches approved routines."""

    def __init__(self, console, *, routines_path=None, journal: Journal | None = None,
                 poll_seconds: float = 15.0, dnd: str | None = None, clock=None):
        self.console = console
        self.routines_path = routines_path or "config/routines.toml"
        self.journal = journal or console.journal
        self.poll_seconds = poll_seconds
        self.dnd = dnd if dnd is not None else self._dnd_from_file()
        self.clock = clock or _dt.datetime.now   # injectable for tests
        self.routines: list[Routine] = load_routines(self.routines_path)

        self.session_id: str | None = None  # dedicated routine session
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fired_dates: dict[str, str] = {}   # routine -> 'YYYY-MM-DD'

    def _dnd_from_file(self):
        """[defaults] dnd from the routines file; None when absent/broken."""
        if tomllib is None:
            return None
        try:
            with open(self.routines_path, "rb") as fh:
                data = tomllib.load(fh)
            return (data.get("defaults") or {}).get("dnd")
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self, join_seconds: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — the scheduler must never die silently
                try:
                    self.journal.add("scheduler", event="tick-error")
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(self.poll_seconds)

    # ------------------------------------------------------------ tick logic

    def _tick(self) -> None:
        self._ensure_session()
        self._watchdog()
        for routine in self.routines:
            if self._due(routine):
                self._launch(routine, reason="scheduled")

    def _ensure_session(self) -> None:
        if self.session_id is not None and self.console.registry.owns(self.session_id):
            return
        try:
            session = self.console.kernel.create_session(
                directory=self.console.workspace,
                model=dict(self.console.model),
                title=ROUTINE_SESSION_TITLE)
        except KernelError:
            return                          # kernel down: try again next tick
        self.session_id = session["id"]
        self.console.registry.add(self.session_id, title=ROUTINE_SESSION_TITLE,
                                  trusted=True)
        self.console.worker.set_trust(self.session_id, "scheduled")
        self.journal.add("routine-session-opened", session_id=self.session_id)

    def _due(self, routine: Routine) -> bool:
        if not routine.active:
            return False
        fire_at = parse_daily_hhmm(routine.schedule)
        if fire_at is None:
            return False
        now = self.clock()
        if (now.time() < fire_at) or routine.busy_since is not None:
            return False
        today = now.strftime("%Y-%m-%d")
        # one fire per calendar day, remembered across restarts via journal
        if self._fired_dates.get(routine.name) == today:
            return False
        if self._fired_in_journal(routine.name, today):
            self._fired_dates[routine.name] = today
            return False
        if routine.last_started is not None and \
                (time.time() - routine.last_started) < routine.cooldown_minutes * 60:
            return False
        if routine.consecutive_failures >= routine.max_consecutive_failures:
            return False
        return True

    def _fired_in_journal(self, name: str, today: str) -> bool:
        for entry in self.journal.tail(500, kind="routine-started"):
            if entry.get("routine") == name and entry.get("date") == today:
                return True
        return False

    def _launch(self, routine: Routine, *, reason: str) -> None:
        self._ensure_session()
        if self.session_id is None:
            return   # kernel unreachable: nothing marked fired; next tick retries
        today = self.clock().strftime("%Y-%m-%d")
        self._fired_dates[routine.name] = today
        routine.last_started = time.time()
        routine.busy_since = time.time()
        routine.failed_during_turn = False
        self.journal.add("routine-started", routine=routine.name, date=today,
                         reason=reason, session_id=self.session_id,
                         task=routine.task[:280])
        self.console.ask(routine.task, lane="bg", source="scheduled",
                         session_id=self.session_id)
        self._notify(routine, f"[jarvis] routine '{routine.name}' launched ({reason})")

    def _watchdog(self) -> None:
        """max_run_minutes budget: interrupt runaway routine turns (own session)."""
        for routine in self.routines:
            if routine.busy_since is None:
                continue
            elapsed = time.time() - routine.busy_since
            if elapsed > routine.max_run_minutes * 60:
                self.journal.add("routine-timeout", routine=routine.name,
                                 ran_s=round(elapsed, 1))
                try:
                    self.console.kernel.interrupt(self.session_id)
                except KernelError:
                    pass
                self._finish(routine, ok=False, note="max_run_minutes exceeded")

    # ------------------------------------------------------- event pipe hook

    def note_event(self, event: dict) -> None:
        """EventPipe subscriber: routine-session turn completion / failure."""
        if self.session_id is None:
            return
        data = event.get("data") or event.get("properties") or {}
        if str(data.get("sessionID", "")) != self.session_id:
            return
        etype = str(event.get("type", ""))
        if etype == "session.execution.failed":
            for r in self.routines:
                if r.busy_since is not None:
                    r.failed_during_turn = True
        elif etype == "session.idle" or etype.endswith(".idle"):
            for r in self.routines:
                if r.busy_since is not None:
                    self._finish(r, ok=not r.failed_during_turn,
                                 note="kernel reported idle")

    def _finish(self, routine: Routine, *, ok: bool, note: str) -> None:
        routine.busy_since = None
        if ok:
            routine.consecutive_failures = 0
            self.journal.add("routine-finished", routine=routine.name, note=note)
            self._notify(routine, f"[jarvis] routine '{routine.name}' finished.")
            return
        routine.consecutive_failures += 1
        self.journal.add("routine-failed", routine=routine.name, note=note,
                         consecutive_failures=routine.consecutive_failures)
        if routine.consecutive_failures >= routine.max_consecutive_failures:
            routine.runtime_off = True
            self._notify(routine, f"[jarvis] routine '{routine.name}' DISABLED after "
                                  f"{routine.consecutive_failures} failures — human review needed.")
        else:
            self._notify(routine, f"[jarvis] routine '{routine.name}' failed "
                                  f"({routine.consecutive_failures}/"
                                  f"{routine.max_consecutive_failures}).")

    def _notify(self, routine: Routine, line: str) -> None:
        """DND suppresses routine notifications ONLY — never confirmations."""
        now = self.clock().time()
        if routine.notify and not in_dnd(now, self.dnd):
            self.console.announcer.nudge(line)

    # ---------------------------------------------------------- human control

    def run_now(self, name: str) -> str:
        """Manual immediate launch. Counts as today's fire: the journal
        dedupe means the scheduled clock trigger will not run it again
        today — one briefing per day, however it was started."""
        for r in self.routines:
            if r.name == name:
                if not r.active:
                    return f"{name}: not active ({r.state})."
                if r.busy_since is not None:
                    return f"{name}: already running."
                self._ensure_session()
                if self.session_id is None:
                    return "kernel unreachable — routine not launched; try again."
                self._launch(r, reason="manual")
                return f"{name}: launched (manual; today's scheduled fire is satisfied)."
        return f"no routine named '{name}'."

    def list_lines(self) -> list[str]:
        now = self.clock()
        lines = []
        for r in self.routines:
            fire = parse_daily_hhmm(r.schedule)
            fire_s = fire.strftime("%H:%M") if fire else "?"
            lines.append(f"{r.name:20s} daily {fire_s}  {r.state:22s} "
                         f"fails {r.consecutive_failures}/{r.max_consecutive_failures}")
        return lines or ["no routines defined (config/routines.toml)"]

    def runtime_toggle(self, name: str, on: bool) -> str:
        """Runtime may only TIGHTEN: 'off' always works; 'on' only resets a
        runtime-off, file-enabled routine. File-disabled stays disabled."""
        for r in self.routines:
            if r.name == name:
                if not on:
                    r.runtime_off = True
                    self.journal.add("routine-toggled", routine=name, state="off")
                    return f"{name}: off (runtime; file unchanged)."
                if not r.enabled:
                    return f"{name}: refused — disabled by file; edit config/routines.toml."
                if r.consecutive_failures >= r.max_consecutive_failures:
                    return (f"{name}: refused — auto-disabled after failures; "
                            "clear it by editing the file (human review).")
                r.runtime_off = False
                self.journal.add("routine-toggled", routine=name, state="on")
                return f"{name}: on."
        return f"no routine named '{name}'."
