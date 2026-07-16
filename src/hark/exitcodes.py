"""CLI exit codes (docs/SPEC.md §4)."""

OK = 0
ERROR = 1
USAGE = 2
HERDR = 3
PROVIDER = 4
AUDIO = 5
TIMEOUT = 6
ABORT = 7  # stale / policy / user cancel


def normalize_failure_exit(code: object, *, fallback: int = ERROR) -> int:
    """Return a valid, nonzero process exit for a reported failure.

    Python accepts arbitrary objects at exception boundaries and the shell only
    preserves integer exit statuses from 0 through 255.  Keep legitimate custom
    failure codes, but never let a false/invalid value turn a failure into
    success (or into platform-dependent truncation).
    """
    if type(fallback) is not int or not 1 <= fallback <= 255:
        raise ValueError("fallback must be an integer from 1 through 255")
    if type(code) is int and 1 <= code <= 255:
        return code
    return fallback
