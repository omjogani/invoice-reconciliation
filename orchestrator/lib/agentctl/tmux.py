"""Thin tmux CLI wrapper for the claude-code harness.

All failures surface as :class:`TmuxError` carrying the failing subcommand and
tmux's stderr — callers convert to their envelope's ``Result.error``. Every
function accepts an optional ``socket`` (tmux ``-L`` named socket) so tests can
run against a disposable server; production callers omit it and share the
user's default server.
"""
from __future__ import annotations

import shlex
import subprocess
import time

_TIMEOUT_S = 10


class TmuxError(RuntimeError):
    pass


def _argv(args: list[str], socket: str | None) -> list[str]:
    return ["tmux"] + (["-L", socket] if socket else []) + args


def _run(args: list[str], socket: str | None = None) -> str:
    try:
        proc = subprocess.run(
            _argv(args, socket), capture_output=True, text=True, timeout=_TIMEOUT_S
        )
    except FileNotFoundError:
        raise TmuxError(
            "tmux is not installed (brew install tmux / apt install tmux)"
        )
    except subprocess.SubprocessError as exc:
        raise TmuxError(f"tmux {args[0]} failed: {exc}")
    if proc.returncode != 0:
        raise TmuxError(f"tmux {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def has_session(name: str, socket: str | None = None) -> bool:
    """True iff a session named exactly ``name`` exists.

    The ``=`` prefix anchors the match — bare ``-t name`` would prefix-match
    (`abc` satisfies a probe for `ab`), which breaks session-reuse decisions.
    Returns False on any error rather than raising: callers use this as a
    cheap existence probe, and a dead/absent server means "no session".
    """
    try:
        proc = subprocess.run(
            _argv(["has-session", "-t", f"={name}"], socket),
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def has_window(window_id: str, socket: str | None = None) -> bool:
    """True iff window ``window_id`` (a ``@N`` id) still exists on the server.

    This is the authoritative death signal for a spawned worker: no factory
    code sets ``remain-on-exit``, so tmux closes the window as soon as its
    command exits. A PID check alone cannot be trusted for that — a dead
    worker's pid number can be recycled by an unrelated process, which would
    read as "still alive" forever (see team/probe.gather).

    Returns False on any error rather than raising: an absent or dead server
    means the window is gone, which is exactly what callers need to know.
    """
    try:
        proc = subprocess.run(
            _argv(["display-message", "-p", "-t", window_id, "#{window_id}"], socket),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == window_id


def ensure_session(name: str, socket: str | None = None) -> str | None:
    """Create a detached session ``name`` iff it does not already exist.

    Returns the placeholder window id (``@N``) when this call created the
    session, else None. ``new-session`` unavoidably creates window 0 running
    the default shell; the caller must kill it once a real window exists,
    otherwise the session never auto-destroys (the placeholder shell keeps it
    alive forever on standard shell configs).
    """
    if has_session(name, socket):
        return None
    out = _run(
        ["new-session", "-d", "-s", name, "-P", "-F", "#{window_id}"], socket
    )
    return out.strip()


def new_window(
    session: str, name: str, command: str, socket: str | None = None
) -> str:
    """Create a window in ``session`` running ``command``.

    Returns the stable window id (``@N``) — unlike the name, it survives
    renames and is the only safe handle for later kill/send targeting.
    """
    out = _run(
        ["new-window", "-t", f"={session}", "-n", name, "-P", "-F", "#{window_id}",
         command],
        socket,
    )
    return out.strip()


def configure_factory_session(name: str, socket: str | None = None) -> None:
    """Session policy applied to every factory-created session.

    - ``client-detached`` hook: closing (or force-closing) the viewer
      Terminal kills the session AND its workers. Factory work is
      recoverable by respawning the phase, and the user prefers dead-on-
      close over leftover sessions accreting on the server. Note this also
      makes a deliberate `Ctrl-b d` detach lethal — factory sessions cannot
      be backgrounded. Never-attached sessions (headless, pre-attach gap)
      are unaffected: the hook only fires on a real detach.
    - ``status-left-length 40``: tmux's default of 10 truncates session
      names to ``DSCO-1915-`` in the status bar, hiding the ticket id.
    - ``mouse on``: tmux consumes mouse events itself (scrollback via
      copy-mode) instead of forwarding raw SGR mouse-report sequences to the
      pane. Without it, a TUI that enables mouse reporting (opencode) receives
      those sequences THROUGH tmux in a form it fails to parse, and every
      mouse move over an attached viewer Terminal lands as literal
      ``^[[<35;78;19M`` junk in the agent's input box (observed 2026-08-03,
      opencode dry run).
    """
    # NB: unlike has-session/new-window/kill-session, the -t of set-hook and
    # set-option REJECTS the `=` exact-match prefix (tmux 3.6b: "no such
    # session"). Bare name is safe here — the exact-named session was just
    # created, and exact matches win target resolution. The hook COMMAND's
    # kill-session does accept (and keeps) the `=`.
    _run(
        ["set-hook", "-t", name, "client-detached",
         f'kill-session -t "={name}"'],
        socket,
    )
    _run(["set-option", "-t", name, "status-left-length", "40"], socket)
    _run(["set-option", "-t", name, "mouse", "on"], socket)


def kill_window(window_id: str, socket: str | None = None) -> None:
    _run(["kill-window", "-t", window_id], socket)


def kill_session(name: str, socket: str | None = None) -> None:
    _run(["kill-session", "-t", f"={name}"], socket)


def rename_window(window_id: str, name: str, socket: str | None = None) -> None:
    _run(["rename-window", "-t", window_id, name], socket)


def lock_window_name(window_id: str, socket: str | None = None) -> None:
    """Stop tmux from overriding the window's `-n` name.

    Without this, `automatic-rename` replaces the name with the pane's
    running command (`bash /long/path/spawn.command`, then `exit` after the
    worker dies), and `allow-rename` lets the program's own OSC title escapes
    do the same.
    """
    _run(["set-option", "-w", "-t", window_id, "automatic-rename", "off"], socket)
    _run(["set-option", "-w", "-t", window_id, "allow-rename", "off"], socket)


def pipe_pane(window_id: str, log_path: str, socket: str | None = None) -> None:
    """Append all pane output to ``log_path`` (tmux runs the snippet via sh)."""
    _run(
        ["pipe-pane", "-o", "-t", window_id, f"cat >> {shlex.quote(log_path)}"],
        socket,
    )


_ENTER_ATTEMPTS = 3
_ENTER_RETRY_DELAY_S = 0.4


def send_text(window_id: str, text: str, socket: str | None = None) -> None:
    """Clear the pane's input line (Ctrl-U), type ``text`` literally, then
    press Enter.

    The Ctrl-U first: an agent's own input box is legitimately empty at all
    times — only nudges (this function) and terminal noise ever put anything
    there — so clearing is always safe, and it stops accumulated junk being
    submitted along with the nudge. The observed junk source: a mouse-
    reporting TUI under an attached viewer collects raw SGR sequences per
    mouse move (see ``configure_factory_session``'s ``mouse on``, the
    at-source fix; this is the defence in depth). Raw-mode TUIs interpret
    the byte as kill-line; canonical-mode panes have the tty line discipline
    do the same — either way pending input is discarded and nothing reaches
    the app as content.

    ``-l`` disables key-name lookup so the text arrives byte-for-byte (no
    shell expansion either — the pane's foreground process receives it as
    typed input, not a shell command line).

    The typing steps are retried ASYMMETRICALLY, which is deliberate:

    * The text step is **not** retried. A failure there is ambiguous — a
      ``send-keys`` that times out may still have delivered — so retrying
      risks typing the line twice. For the teams nudge (this function's main
      caller) a duplicated pointer is worse than a missed one, because the
      message itself is already durable on disk and the recipient polls
      regardless.
    * The Enter step **is** retried, because losing it is the damaging
      outcome: the text sits in the recipient's input box unsubmitted, so the
      nudge silently never happens and a lead quietly degrades to poll-only.
      A redundant Enter is verifiably harmless — on an empty prompt the TUI
      ignores it.

    Both failures observed in the 2026-08-03 dry run were transient rather
    than wrong-key: one ``send-keys`` timed out against a busy tmux server,
    and one reported ``not in a mode`` from a momentary pane state. Both ways
    of submitting (the ``Enter`` key name and a literal CR) were verified to
    work against a live claude TUI, so the key was never the problem.
    """
    _run(["send-keys", "-t", window_id, "C-u"], socket)
    _run(["send-keys", "-t", window_id, "-l", text], socket)
    last: TmuxError | None = None
    for attempt in range(_ENTER_ATTEMPTS):
        try:
            _run(["send-keys", "-t", window_id, "Enter"], socket)
            return
        except TmuxError as exc:
            last = exc
            if attempt + 1 < _ENTER_ATTEMPTS:
                time.sleep(_ENTER_RETRY_DELAY_S)
    raise TmuxError(
        f"typed the text but could not submit it after {_ENTER_ATTEMPTS} "
        f"attempts ({last}) — the recipient's input box holds an unsent line"
    )


def send_escape(window_id: str, socket: str | None = None) -> None:
    _run(["send-keys", "-t", window_id, "Escape"], socket)
