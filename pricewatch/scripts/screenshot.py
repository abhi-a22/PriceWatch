"""Start a real PriceWatch process, create demo data through the API, and screenshot the dashboard.

Usage: python scripts/screenshot.py [output.png]
Takes about 80 seconds because the 1-minute change column needs a minute of history.
"""

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/dashboard.png")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def main() -> None:
    hook_port, port = free_port(), free_port()
    hook = HTTPServer(("127.0.0.1", hook_port), Receiver)
    threading.Thread(target=hook.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp()
    env = {
        **os.environ,
        "PRICEWATCH_PORT": str(port),
        "PRICEWATCH_DB": f"{tmp}/demo.db",
        "PRICEWATCH_SEED": "11",
        "PRICEWATCH_JWT_SECRET": "screenshot-demo-secret-" + "x" * 30,
        "PRICEWATCH_ALLOW_PRIVATE_WEBHOOKS": "1",  # the demo receiver is on localhost
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "pricewatch"], env=env, stdout=subprocess.DEVNULL
    )  # noqa: S603
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                requests.get(base + "/api/health", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.1)
        creds = {"email": "demo@example.com", "password": "demo-password-1"}
        token = requests.post(base + "/api/auth/register", json=creds, timeout=10).json()["token"]
        headers = {"Authorization": "Bearer " + token}
        hook_url = f"http://127.0.0.1:{hook_port}/hook"
        demo_alerts = [
            {"symbol": "NVDA", "rule_type": "pct_rise", "threshold": 0.15, "window_seconds": 10,
             "repeating": True, "cooldown_seconds": 20, "webhook_url": hook_url},
            {"symbol": "BTC", "rule_type": "pct_drop", "threshold": 0.15, "window_seconds": 10,
             "repeating": True, "cooldown_seconds": 20},
            {"symbol": "TSLA", "rule_type": "pct_rise", "threshold": 0.2, "window_seconds": 30,
             "repeating": True, "cooldown_seconds": 30},
            {"symbol": "AAPL", "rule_type": "above", "threshold": 400},
            {"symbol": "MSFT", "rule_type": "below", "threshold": 100,
             "repeating": True, "cooldown_seconds": 300},
        ]  # fmt: skip
        for body in demo_alerts:
            r = requests.post(base + "/api/alerts", json=body, headers=headers, timeout=5)
            assert r.status_code == 201, r.text

        print("waiting 75s for a minute of price history and some alerts to fire...")
        time.sleep(75)

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception:  # noqa: BLE001
                browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(
                viewport={"width": 1180, "height": 900}, device_scale_factor=2, color_scheme="light"
            )
            page.goto(base)
            page.fill("#auth-email", creds["email"])
            page.fill("#auth-password", creds["password"])
            page.click("#auth-submit")
            expect(page.locator("#alerts-list li")).to_have_count(len(demo_alerts))
            expect(page.locator("#events-list li").first).to_be_visible(timeout=20_000)
            page.wait_for_timeout(1500)
            OUT.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUT), full_page=True)
            browser.close()
        print("saved", OUT)
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        hook.shutdown()


if __name__ == "__main__":
    main()
