"""Entry point: python -m pricewatch

Runs a single process. The feed, price store and event bus live in memory, so do
not run several copies behind a load balancer (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import logging
import signal
import threading

from werkzeug.serving import make_server

from .app import create_app
from .config import Config
from .services import Services


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log = logging.getLogger("pricewatch")
    cfg = Config.from_env()
    if cfg.uses_default_secret:
        log.warning(
            "PRICEWATCH_JWT_SECRET is not set: using an insecure development secret. "
            "Set it before exposing this app to anyone."
        )
    if cfg.allow_private_webhooks:
        log.warning("PRICEWATCH_ALLOW_PRIVATE_WEBHOOKS is on: SSRF protection is disabled.")

    services = Services(cfg)
    app = create_app(services)
    services.start()
    server = make_server(cfg.host, cfg.port, app, threaded=True)

    def request_shutdown(signum, _frame):
        log.info("received %s, shutting down", signal.Signals(signum).name)
        # shutdown() blocks until serve_forever() returns, and serve_forever() runs on
        # this very thread, so it has to be called from another one.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    log.info(
        "PriceWatch listening on http://%s:%d (feed=%s, symbols=%s)",
        cfg.host, cfg.port, services.provider.name, ",".join(cfg.symbols),
    )  # fmt: skip
    try:
        server.serve_forever()
    finally:
        server.server_close()
        services.stop()
        log.info("shutdown complete")


if __name__ == "__main__":
    main()
