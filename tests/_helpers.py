"""Shared helper for the test_*.py files.

Each module already carries its own plain-assert self-check under
``if __name__ == "__main__"`` (garmin-coach's test_coach.py precedent,
just inlined per-module here since these modules are small enough
that a separate mirror file would only duplicate the same asserts).
These test_*.py files run that self-check as an isolated subprocess
so ``python tests/test_training.py`` etc. is still the entrypoint a
reader expects, without maintaining two copies of the same assertions.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def run_module_selfcheck(module_path: str) -> str:
    """Run a module's own ``__main__`` self-check in a subprocess.

    Parameters:
        module_path (str): Path to the module, relative to the repo
            root (e.g. ``"training.py"`` or
            ``"ingest/parse_health_connect.py"``).

    Returns:
        str: Combined stdout+stderr, for the caller to print/inspect.

    Raises:
        AssertionError: The self-check exited nonzero.
    """
    result = subprocess.run(
        [sys.executable, module_path], cwd=ROOT,
        capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return output.strip()
