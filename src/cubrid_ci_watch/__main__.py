"""Module entry point for `python -m cubrid_ci_watch`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
