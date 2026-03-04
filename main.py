import logging
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


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
