"""Logging configuration — defined once, applied per-script.

Every entry-point script used to repeat the same ``logging.basicConfig(...)``
block at import time. That duplication drifted (some set a datefmt, some did
not) and ran on *import*, which is a side effect a library must never impose.
``setup_logging()`` centralises the format and is called explicitly from each
script's ``main()`` (or just before it), never at import time inside ``lib``.
"""

import logging

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging with the project's standard format."""
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
