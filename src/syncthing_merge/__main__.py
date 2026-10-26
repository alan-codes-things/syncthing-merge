"""Allow ``python -m syncthing_merge``."""

from . import main

if __name__ == "__main__":
    raise SystemExit(main())
