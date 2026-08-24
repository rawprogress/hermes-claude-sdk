"""``python -m hermes_claude_runner`` — used by the daemon to spawn workers."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
