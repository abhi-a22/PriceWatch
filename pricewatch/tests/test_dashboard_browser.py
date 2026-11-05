"""Browser smoke test of the dashboard (skipped when Playwright or a browser is missing)."""

import pathlib

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

pytestmark = pytest.mark.browser

STATIC = pathlib.Path(__file__).parent.parent / "pricewatch" / "static"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        launched = None
        for kwargs in ({}, {"channel": "chrome"}):  # bundled Chromium, then installed Chrome
            try:
                launched = p.chromium.launch(**kwargs)
                break
            except Exception:  # noqa: BLE001, S112
                continue
        if launched is None:
            pytest.skip("no Chromium or Chrome available to Playwright")
        yield launched
        launched.close()


def test_dashboard_source_never_uses_innerhtml():
    for path in STATIC.iterdir():
        text = path.read_text()
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert banned not in text, f"{banned} found in {path.name}"


def test_no_inline_scripts_styles_or_external_assets():
    html = (STATIC / "index.html").read_text()
    assert "<style" not in html and 'style="' not in html and "onclick" not in html
    assert "http://" not in html and "https://" not in html.replace("https://example.com/hook", "")
    for script in html.split("<script")[1:]:
        assert script.startswith(' src="/static/')


def test_signup_create_alert_and_see_it_fire(browser, start_live):
    live = start_live(tick_interval=0.1, seed=5)
    page = browser.new_page()
    problems = []
    page.on(
        "console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None
    )
    page.on("pageerror", lambda e: problems.append(str(e)))
    try:
        page.goto(live.url)
        expect(page.locator("#prices-body tr")).to_have_count(6, timeout=10_000)
        expect(page.locator("#status")).to_contain_text("simulated", timeout=10_000)
        expect(page.locator("#alert-card")).to_be_hidden()

        page.click("#tab-register")
        page.fill("#auth-email", "browser@example.com")
        page.fill("#auth-password", "correct-horse-1")
        page.click("#auth-submit")
        expect(page.locator("#alert-card")).to_be_visible()
        expect(page.locator("#whoami")).to_have_text("browser@example.com")

        # a price rule that is already true fires on the next tick
        page.select_option("#f-symbol", "AAPL")
        page.select_option("#f-rule", "above")
        page.fill("#f-threshold", "1")
        page.click("#alert-form button[type=submit]")
        expect(page.locator("#alerts-list li")).to_have_count(1)
        expect(page.locator("#events-list li")).to_have_count(1, timeout=10_000)
        expect(page.locator("#events-list li")).to_contain_text("AAPL")
        expect(page.locator("#alerts-list li .badge")).to_have_text("Inactive", timeout=10_000)

        # pause / re-arm / delete on a second alert that can never fire (no refiring race)
        page.fill("#f-threshold", "1000000")
        page.click("#alert-form button[type=submit]")
        quiet = page.locator("#alerts-list li", has_text="1,000,000")
        expect(quiet).to_have_count(1)
        expect(quiet.locator(".badge")).to_have_text("Active")
        quiet.get_by_role("button", name="Pause").click()
        expect(quiet.locator(".badge")).to_have_text("Inactive")
        quiet.get_by_role("button", name="Re-arm").click()
        expect(quiet.locator(".badge")).to_have_text("Active")
        quiet.get_by_role("button", name="Delete").click()
        expect(page.locator("#alerts-list li")).to_have_count(1)
        page.get_by_role("button", name="Delete").click()
        expect(page.locator("#alerts-list li")).to_have_count(0)
        expect(page.locator("#alerts-empty")).to_be_visible()
        expect(page.locator("#events-list li")).to_have_count(0, timeout=10_000)  # cascade

        # bad input surfaces the API's validation error
        page.fill("#f-threshold", "-3")
        page.click("#alert-form button[type=submit]")
        expect(page.locator("#alert-error")).to_contain_text("threshold")

        # the window and cooldown fields start hidden for a one-time price rule
        expect(page.locator("#f-window-wrap")).to_be_hidden()
        expect(page.locator("#f-cooldown-wrap")).to_be_hidden()

        # percent rules reveal the window field; repeating reveals the cooldown
        page.select_option("#f-rule", "pct_rise")
        expect(page.locator("#f-window-wrap")).to_be_visible()
        page.check("#f-repeating")
        expect(page.locator("#f-cooldown-wrap")).to_be_visible()

        page.click("#logout")
        expect(page.locator("#auth-card")).to_be_visible()
        expect(page.locator("#alert-card")).to_be_hidden()

        # The deliberate bad-input step above makes the browser log one expected 400;
        # anything else (script errors, CSP violations, failed loads) is a real problem.
        unexpected = [m for m in problems if "400 (BAD REQUEST)" not in m]
        assert unexpected == [], unexpected
    finally:
        page.close()


def test_wrong_password_shows_error_and_stays_signed_out(browser, start_live):
    live = start_live(tick_interval=0.1, seed=5)
    page = browser.new_page()
    try:
        page.goto(live.url)
        page.fill("#auth-email", "nobody@example.com")
        page.fill("#auth-password", "whatever-123")
        page.click("#auth-submit")
        expect(page.locator("#auth-error")).to_have_text("invalid email or password")
        expect(page.locator("#alert-card")).to_be_hidden()
    finally:
        page.close()


def test_light_dark_and_mobile_layout(browser, start_live):
    live = start_live(tick_interval=0.1, seed=5)
    backgrounds = {}
    for scheme in ("light", "dark"):
        ctx = browser.new_context(color_scheme=scheme, viewport={"width": 375, "height": 800})
        page = ctx.new_page()
        page.goto(live.url)
        expect(page.locator("#prices-body tr")).to_have_count(6, timeout=10_000)
        backgrounds[scheme] = page.evaluate("getComputedStyle(document.body).backgroundColor")
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, f"horizontal overflow of {overflow}px at 375px width ({scheme})"
        ctx.close()
    assert backgrounds["light"] != backgrounds["dark"]
