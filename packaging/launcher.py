"""PyInstaller entry point for the steward one-file binary.

Imports steward.cli as a package (never as __main__) so relative imports
inside steward.* resolve inside the frozen app, then runs the CLI.
"""

import sys

from steward.cli import main

if __name__ == "__main__":
    sys.exit(main())
