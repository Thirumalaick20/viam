"""Terminal colour, off unless a human is watching.

The course tests run course-tools with its output captured and match on
literal substrings, so escape codes must never reach a pipe. Colour is on
only when stdout is a terminal and NO_COLOR is unset.
"""

import os
import sys

_ON = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _wrap(code):
    def paint(text):
        return f"\033[{code}m{text}\033[0m" if _ON else str(text)
    return paint


bold = _wrap("1")
dim = _wrap("2")
red = _wrap("31")
green = _wrap("32")
yellow = _wrap("33")
cyan = _wrap("36")


def pass_(text=" PASS "):
    return _wrap("30;42")(text) if _ON else text.strip()


def fail(text=" FAIL "):
    return _wrap("30;41")(text) if _ON else text.strip()
