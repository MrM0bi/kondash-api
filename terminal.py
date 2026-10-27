"""Small stdlib-only helpers for readable setup output (colors only on a terminal, NO_COLOR respected)."""
import os
from pathlib import Path
import sys


def _styled(stream, code, text):
    if stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb":
        return f"\033[{code}m{text}\033[0m"
    return text


def _symbol(stream, fancy, plain):
    try:
        fancy.encode(stream.encoding or "ascii")
        return fancy
    except (UnicodeEncodeError, LookupError):
        return plain


def _line(stream, code, fancy, plain, text, indent="  "):
    if stream is not sys.stdout:
        sys.stdout.flush()  # keep stdout/stderr in order when both go to the same pipe
    print(indent + _styled(stream, code, _symbol(stream, fancy, plain)) + " " + text, file=stream)


def _fix(stream, fix, indent):
    arrow = _symbol(stream, "→", "->")
    for number, line in enumerate((fix or "").splitlines()):
        lead = _styled(stream, "33", arrow) if number == 0 else " " * len(arrow)
        print(indent + "    " + lead + " " + line, file=stream)


def step(text):
    print(file=sys.stdout)
    print(_styled(sys.stdout, "1;36", "==>") + " " + _styled(sys.stdout, "1", text), file=sys.stdout)


def ok(text, indent="  "):
    _line(sys.stdout, "32", "✓", "+", text, indent)


def skip(text, indent="  "):
    _line(sys.stdout, "2", "·", "-", text, indent)


def bad(text, indent="  "):
    """A failed result line on stdout (unlike fail(), which reports an error on stderr)."""
    _line(sys.stdout, "1;31", "✗", "x", text, indent)


def warn(text, fix=None):
    _line(sys.stderr, "33", "!", "!", text)
    _fix(sys.stderr, fix, "  ")


def fail(text, fix=None, stream=None, indent=""):
    stream = stream or sys.stderr
    _line(stream, "1;31", "✗", "x", text, indent=indent)
    _fix(stream, fix, indent)


def problem(text, fix):
    """A failed preflight check, printed in place with what to do about it."""
    fail(text, fix, sys.stdout, "  ")


def headline(text):
    print(file=sys.stdout)
    print(_styled(sys.stdout, "1;32", text), file=sys.stdout)


def table(rows):
    """Print aligned "label  value" rows; a None row prints a blank separator line."""
    width = max((len(row[0]) for row in rows if row), default=0)
    for row in rows:
        if row is None:
            print(file=sys.stdout)
        else:
            print("  " + _styled(sys.stdout, "1", row[0].ljust(width)) + "  " + row[1], file=sys.stdout)


def tidy(path):
    """Show paths under the home directory as ~/... for shorter output."""
    path, home = Path(path), Path.home()
    return "~/" + str(path.relative_to(home)) if path == home or home in path.parents else str(path)
