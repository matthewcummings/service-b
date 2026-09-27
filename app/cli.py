"""Entry point: `python -m app <serve|migrate|seed|copy-db>` (default: serve).

One image, several commands: ECS runs each as its own container, in order (see README).
"""

import argparse
import logging
import sys

import uvicorn

from app.api import create_app
from app.config import AppSettings
from app.copy_db import copy_db
from app.errors import CommandFailed
from app.migrate import migrate
from app.seed import seed


def serve() -> None:
    settings = AppSettings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)


COMMANDS = {"serve": serve, "migrate": migrate, "seed": seed, "copy-db": copy_db}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("command", nargs="?", default="serve", choices=COMMANDS)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        COMMANDS[args.command]()
    except CommandFailed as exc:
        logging.getLogger("app").error("%s failed: %s", args.command, exc)
        sys.exit(1)
