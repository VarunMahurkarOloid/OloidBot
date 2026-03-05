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


def _start_keep_alive():
    """Self-ping every 10 min to prevent Render free tier from sleeping."""
    import threading
    import urllib.request

    def ping():
        while True:
            try:
                port = int(os.environ.get("PORT", 8080))
                urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5)
            except Exception:
                pass
            import time as _t
            _t.sleep(600)  # 10 minutes

    if os.environ.get("RENDER"):
        t = threading.Thread(target=ping, daemon=True)
        t.start()
        logger.info("Keep-alive ping started (every 10 min)")


def main():
    from src.config import settings
    from src.oauth_server import start_oauth_server
    from src.scheduler import start_scheduler
    from src.slack_bot import start_bot

    logger.info("OloidBot starting...")
    logger.info("Data dir: %s", settings.effective_data_dir)
    logger.info("Port: %s", settings.effective_port)
    logger.info("Encryption: %s", "FERNET_KEY env var" if settings.fernet_key else "auto-generated key file")

    # Start OAuth callback server (background thread)
    oauth_server = start_oauth_server()

    # Keep-alive self-ping (prevents Render free tier from sleeping)
    _start_keep_alive()

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
