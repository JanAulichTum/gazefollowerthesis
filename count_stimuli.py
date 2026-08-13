# -*- coding: utf-8 -*-
"""How many real stimuli will this session present? One number, nothing else.

WHY THIS IS A FILE AND NOT A ONE-LINER
--------------------------------------
The launcher needs this count before a participant sits down, because
``SESSION_STIMULUS_MODE=all`` against an empty folder is a session that
records nothing and you find out only once they are seated.

It was originally written inline::

    for /f %%n in ('python -c "import config;print(len(config.discover_stimuli()))"') do ...

which cmd.exe cannot parse. Inside ``for /f ('...')`` the command is
re-parsed by a second shell, and the parentheses of ``len(...)`` and
``discover_stimuli()`` are metacharacters at that point. The failure is
not a Python error and does not name the line: cmd reports

    "." kann syntaktisch an dieser Stelle nicht verarbeitet werden.

and then terminates the console — taking the menu that called it down as
well. A separate file has no quoting or parenthesis exposure at all.

Prints a single integer to stdout so the caller can read it directly.
"""

from __future__ import annotations

import sys

try:
    import config

    print(len(config.discover_stimuli()))
except Exception:  # noqa: BLE001
    # The launcher treats an empty result as "unknown" and says so,
    # rather than a batch script showing a Python traceback to whoever
    # is about to run a participant.
    print("")
    sys.exit(1)
