import logging

NOISY_LOGGERS = ("httpx", "httpx2", "apscheduler.executors.default")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
