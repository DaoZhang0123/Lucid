"""PyInstaller entry shim.

`python/otterscope/__main__.py` uses package-relative imports (`from .config import ...`),
which PyInstaller can't satisfy if it's invoked directly as the entry script.
This wrapper imports the package first so the relative imports resolve.
"""
import sys

# Force UTF-8 so emoji / non-GBK output from rich / sidecar JSON works on
# legacy Windows consoles (default cp936 in zh-CN locale).
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from otterscope.__main__ import main


if __name__ == "__main__":
    sys.exit(main())
