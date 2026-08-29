"""Shared progress/diagnostic sink.

Every stage (frontier loop, provider LLM calls) emits its progress lines
through :func:`emit` so there is one place that decides where they go. Lines
always print to stderr (never stdout: the CLI writes result JSON to stdout, so
a stray progress line there would corrupt it for a consumer piping the output).

On top of that, giving a log file path (via :func:`configure`, the Extractor,
or ``AWE_LOG_FILE``) also appends each emitted line -- prefixed with a
timestamp -- to that file. A single knob: an empty path means no file logging
(the default), a non-empty path turns it on. A durable, timestamped record is
useful to a host codebase consuming this library.
"""

import re
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Module-global sink config. `None` means file logging is off (the default until
# a non-empty path is configured). Guarded by a lock so concurrent emits don't
# interleave a partial line in the file.
_lock = threading.Lock()
_log_file_path: Path | None = None


def configure(*, log_file: str = "") -> None:
    """Set the log file path (empty string turns file logging off).

    The path is resolved relative to the current working directory, so a host
    codebase gets the log wherever it runs the crawl from.
    """
    global _log_file_path
    with _lock:
        _log_file_path = Path(log_file) if log_file else None


@dataclass(frozen=True)
class Event:
    """One emitted line, in a shape a subscriber can branch on.

    ``kind`` is the bracketed tag the line already carries by convention --
    ``[fetch]``, ``[blocked]``, ``[robots]``, ``[summarize]`` -- so a subscriber
    can filter without parsing prose. It is best-effort: a line with no tag has
    ``kind == ""``, and the tag is not a stable API the way the result object is.
    ``message`` is the exact text that went to stderr, indentation included.
    """

    kind: str
    message: str


Subscriber = Callable[[Event], None]

# Subscribers are registered for the duration of a crawl (see `subscribed`) and
# are module-global like the log path above, so with two Extractors running
# concurrently in one process both callbacks see both crawls' lines. That is the
# same caveat the process-wide http clients carry, and for the same reason: the
# alternative is threading a sink through every module that emits.
_subscribers: list[Subscriber] = []

# The library's own log convention: an optionally indented `[tag]` prefix.
_KIND = re.compile(r"^\s*\[([a-z0-9_:-]+)\]", re.IGNORECASE)


@contextmanager
def subscribed(callback: Subscriber | None) -> Iterator[None]:
    """Deliver every :func:`emit` to `callback` for the life of the block.

    ``None`` is a no-op, so a caller that did not ask for events pays nothing.
    """
    if callback is None:
        yield
        return
    with _lock:
        _subscribers.append(callback)
    try:
        yield
    finally:
        with _lock:
            try:
                _subscribers.remove(callback)
            except ValueError:
                pass


def emit(message: str, *, kind: str | None = None) -> None:
    """Emit one progress/diagnostic line to stderr, the log file, and subscribers.

    `kind` overrides the tag derived from the message; pass it for a line that
    doesn't carry the usual `[tag]` prefix.
    """
    print(message, file=sys.stderr, flush=True)
    # The file write stays under the lock -- concurrent emits would otherwise
    # interleave half-lines -- but subscribers are notified *outside* it. `_lock`
    # is not reentrant, so a subscriber that logs (a plausible thing for a
    # subscriber to do) would deadlock the crawl if it were called while held.
    with _lock:
        path = _log_file_path
        subscribers = list(_subscribers)
        if path is not None:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{stamp} {message}\n")
            except OSError:
                # Never let a logging failure (unwritable path, full disk) break
                # a crawl -- the stderr line already went out above.
                pass
    if not subscribers:
        return
    if kind is None:
        found = _KIND.match(message)
        kind = found.group(1).lower() if found else ""
    event = Event(kind=kind, message=message)
    for subscriber in subscribers:
        try:
            subscriber(event)
        except Exception:  # noqa: BLE001 - a subscriber must not break a crawl
            pass
