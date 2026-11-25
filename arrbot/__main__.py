import logging
import os

from .bot import build_app
from .config import load_config


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # hides the bot token in URLs
    build_app(load_config()).run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
