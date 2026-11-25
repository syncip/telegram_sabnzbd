import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bot import build_app
from .config import load_config

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # stdout: what "docker logs" and the Unraid log windows show.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    # Additionally a file in the data folder (on Unraid e.g. /mnt/user/appdata/.../data/arrbot.log).
    try:
        path = Path(os.environ.get("DATA_DIR", "/data")) / "arrbot.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError as e:
        print(f"Log-Datei nicht möglich: {e}", flush=True)
    logging.basicConfig(level=level, format=FORMAT, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # hides the bot token in URLs


def main() -> None:
    setup_logging()
    log = logging.getLogger("arrbot")
    log.info("arrbot startet …")
    try:
        cfg = load_config()
    except SystemExit as e:
        log.error("Konfigurationsfehler: %s", e)
        raise
    build_app(cfg).run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
