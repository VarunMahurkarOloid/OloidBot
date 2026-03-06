import logging
import os
import signal
import sys

# Always use INFO for our code so we see errors and startup info.
# Only quiet noisy third-party loggers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Quiet noisy third-party loggers
for _noisy in ("urllib3", "google", "googleapiclient", "oauth2client",
               "apscheduler.scheduler", "apscheduler.executors",
               "slack_sdk.web.slack_response", "slack_bolt",
               "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _start_keep_alive(base_url: str):
    """Ping the external URL every 5 min to prevent Render free tier from sleeping.

    Render only counts EXTERNAL requests as activity. Localhost pings don't work.
    """
    import threading
    import urllib.request

    health_url = f"{base_url}/health"

    def ping():
        import time as _t
        while True:
            _t.sleep(300)  # 5 minutes
            try:
                urllib.request.urlopen(health_url, timeout=10)
                logger.debug("Keep-alive ping OK: %s", health_url)
            except Exception as e:
                logger.warning("Keep-alive ping failed: %s", e)

    t = threading.Thread(target=ping, daemon=True)
    t.start()
    logger.info("Keep-alive started — pinging %s every 5 min", health_url)


def main():
    from src.config import settings
    from src.oauth_server import start_oauth_server
    from src.scheduler import start_scheduler
    from src.slack_bot import start_bot

    logger.info("OloidBot starting...")
    logger.info("Base URL: %s", settings.effective_base_url)
    logger.info("Data dir: %s", settings.effective_data_dir)
    logger.info("Port: %s", settings.effective_port)
    logger.info("Encryption: %s", "FERNET_KEY env var" if settings.fernet_key else "auto-generated key file")

    # Start OAuth callback server (background thread)
    oauth_server = start_oauth_server()

    # Keep-alive ping (prevents Render free tier from sleeping)
    if os.environ.get("RENDER"):
        _start_keep_alive(settings.effective_base_url)

    # Start email poller (background thread)
    scheduler = start_scheduler()

    # On Render: ignore SIGTERM so the service stays alive.
    # Render sends SIGTERM when it *thinks* the service is idle.
    # If we ignore it, Render will keep the process running as long as
    # the health check passes (which our keep-alive ensures).
    # On local dev: handle SIGTERM/SIGINT for clean shutdown.
    if os.environ.get("RENDER"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
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
