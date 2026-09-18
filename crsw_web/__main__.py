"""Local development entry point: `python -m crsw_web`.

Loads .env from the current directory (without overriding real env
vars), then serves on 127.0.0.1:8000 with reload. Containers do not
use this; they run uvicorn directly with env from compose."""
import sys

import uvicorn

from .config import ConfigError, Settings, load_dotenv


def main() -> int:
    load_dotenv()
    try:
        Settings.from_env()   # fail here, with the names, not inside uvicorn
    except ConfigError as e:
        print("config error: %s" % e, file=sys.stderr)
        return 2
    uvicorn.run("crsw_web.app:create_app", factory=True,
                host="127.0.0.1", port=8000, reload=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
