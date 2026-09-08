"""Make the research package and the auto_trader repo root importable in tests.

The research venv (3.13) does not install auto_trader; the pure scalping signal
now lives in this package (``scalping_signal.py``, stdlib-only), and the repo
root stays importable for the research modules that still read app-side pure
contracts.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # .../auto_trader.rob-316

for _p in (str(_HERE), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
