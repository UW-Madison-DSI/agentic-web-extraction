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

import sys
import threading
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


def emit(message: str) -> None:
    """Emit one progress/diagnostic line to stderr and, if set, to the log file."""
    print(message, file=sys.stderr, flush=True)
    with _lock:
        path = _log_file_path
        if path is None:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {message}\n")
        except OSError:
            # Never let a logging failure (unwritable path, full disk) break a
            # crawl -- the stderr line already went out above.
            pass
