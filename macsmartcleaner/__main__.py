import sys

from .cli import main

# guard: scan worker processes re-import this module and must not start the CLI
if __name__ == "__main__":
    sys.exit(main())
