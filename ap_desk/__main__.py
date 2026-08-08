"""Package entry point so `python -m ap_desk ...` works from a clone."""

from ap_desk.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
