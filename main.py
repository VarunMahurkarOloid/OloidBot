import logging
import os
import signal
import sys

# Production: WARNING only; Dev: INFO
_log_level = logging.WARNING if os.environ.get("RENDER") else logging.INFO

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Quiet noisy third-party loggers even in dev
for _noisy in ("urllib3", "google", "googleapiclient", "oauth2client",
               "apscheduler", "slack_sdk", "slack_bolt", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
# Always show our startup messages
logger.setLevel(logging.INFO)


def main():
    from src.config import settings
    from src.oauth_server import start_oauth_server
    from src.scheduler import start_scheduler
    from src.slack_bot import start_bot

    logger.info("OloidBot starting...")

    # Start OAuth callback server (background thread)
    oauth_server = start_oauth_server()

    # Start email poller (background thread)
    scheduler = start_scheduler()

    def shutdown(signum, frame):
        logger.info("Shutting down...")
        oauth_server.shutdown()
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start Slack bot (blocking)
    start_bot()


if __name__ == "__main__":
    main()
