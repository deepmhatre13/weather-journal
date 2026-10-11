"""Entrypoint for running weather_journal as a module."""

import sys

from weather_journal.cli import main

if __name__ == "__main__":
    sys.exit(main())
