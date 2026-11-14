"""FieldPulse v0.4.0 browser visual and interaction regression.

Runs on a seeded demo Compose stack. Captures snapshots of real, role-gated
pages at desktop and mobile widths. No mocked server data or external assets.
"""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = os.getenv("SMOKE_URL", "http://127.0.0.1:8000")
PASSWORD = os.getenv("DEMO_PASSWORD", "FieldPulseDemo!2026")
OUTPUT = Path(os.getenv("VISUAL_OUTPUT", "visual-artifacts"))
OUTPUT.mkdir(parents=True, exist_ok=True)


def assert_no_horizontal_overflow(page, label):
    dimensions = page.evaluate("""() => ({
        viewport: window.innerWidth,
        document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth
    })""")
    assert dimensions["document"] <= dimensions["viewport"] + 2, (
        label, "Document overflow", dimensions
    )


def capture(page, name):
    # Capture the header and primary content, not a previously scrolled table.
    page.evaluate("() => window.scrollTo({top: 0, behavior: 'instant'})")
    page.screenshot(path=str(OUTPUT / (name + ".png")), full_page=False,
                    animations="disabled", caret="hide")


def sign_in(page, email):
    page.goto(ROOT, wait_until="networkidle", timeout=45000)
    expect(page.locator("#login")).to_be_visible(timeout=12000)
    page.locator('#login-form [name="email"]').fill(email)
    page.locator('#login-form [name="password"]').fill(PASSWORD)
    page.locator('#login-form button[type="submit"]').click()
    expect(page.locator("#shell")).to_be_visible(timeout=25000)
    expect(page.locator(".page-head h1")).to_be_visible(timeout=25000)


def open_page(page, name):
    page.locator('.nav-item[data-page="%s"]' % name).click()
    expect(page.locator('#navigation [data-page="%s"][aria-current="page"]' % name)).to_be_visible()
    expect(page.locator(".page-head h1")).to_be_visible()


def test_role(browser, role, email):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        sign_in(page, email)
        assert_no_horizontal_overflow(page, role + " overview")
        capture(page, "desktop-" + role + "-overview")
        nav = page.locator("#navigation .nav-item").all()
        assert len(nav) >= 4
        if role == "client":
            assert page.locator('[data-page="admin"]').count() == 0
            assert page.locator('[data-page="reports"]').count() == 0
        if role != "admin":
            assert page.locator('[data-page="admin"]').count() == 0

        if role == "admin":
            open_page(page, "admin")
            expect(page.locator('[data-edit-policy]')).to_be_visible()
            capture(page, "desktop-admin")
            page.locator('[data-edit-policy]').click()
            expect(page.locator("#admin-policy-form")).to_be_visible()
            expect(page.locator('[name="intervals-0"]')).to_be_visible()
            page.locator(".modal-close").click()

        if role in ("dispatcher", "manager", "admin"):
            open_page(page, "reports")
            expect(page.locator(".responsive-chart")).to_be_visible()
            capture(page, "desktop-" + role + "-reports")

        open_page(page, "tickets")
        expect(page.locator("#ticket-search")).to_be_visible()
        page.locator("#ticket-search").fill("zzzz-an-entry-that-does-not-exist")
        expect(page.locator(".empty")).to_be_visible()
        page.locator("#ticket-search").fill("")
        expect(page.locator("#ticket-table .table")).to_be_visible()
        page.locator('[data-density="compact"]').click()
        assert page.locator('[data-density="compact"]').get_attribute("aria-pressed") == "true"
        page.locator('button[data-theme="dark"]').last.click()
        assert page.locator("html").get_attribute("data-theme") == "dark"
        capture(page, "desktop-" + role + "-tickets-dark")
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#shell")).to_be_visible(timeout=20000)
        expect(page.locator(".page-head h1")).to_be_visible(timeout=20000)
        assert page.locator("html").get_attribute("data-theme") == "dark", "Theme not persisted"
        # New tab starts from an overview, user remains logged in via cookie.
        page.locator('button[data-theme="light"]').last.click()
        assert page.locator("html").get_attribute("data-theme") == "light"

        page.set_viewport_size({"width": 390, "height": 844})
        assert_no_horizontal_overflow(page, role + " mobile")
        expect(page.locator(".page-head h1")).to_be_visible()
        page.locator("#menu-toggle").click()
        expect(page.locator("#sidebar")).to_have_class("sidebar open")
        assert page.locator("#menu-toggle").get_attribute("aria-expanded") == "true"
        page.locator("#sidebar-backdrop").click(position={"x": 350, "y": 110})
        assert page.locator("#menu-toggle").get_attribute("aria-expanded") == "false"
        capture(page, "mobile-" + role + "-overview")
        if role == "engineer":
            # Engineer's PWA is also covered by scripts/browser_e2e.py with offline mutations.
            page.locator("#menu-toggle").click()
            page.locator('.nav-item[data-page="mobile"]').click()
            expect(page.locator("#menu-toggle")).to_have_attribute("aria-expanded", "false", timeout=20000)
            expect(page.locator(".mobile-grid")).to_be_visible(timeout=20000)
            assert_no_horizontal_overflow(page, "engineer mobile tasks")
            capture(page, "mobile-engineer-field")
        if role == "dispatcher":
            # Boundary widths and tablet: document must never grow beyond viewport.
            for width in (320, 768, 1024, 2560):
                page.set_viewport_size({"width": width, "height": 900})
                assert_no_horizontal_overflow(page, "dispatcher width " + str(width))
                expect(page.locator(".page-head h1")).to_be_visible()
                if width in (320, 2560):
                    capture(page, "responsive-dispatcher-" + str(width))
        assert not errors, (role, "Browser JavaScript exceptions", errors[:10])
    except BaseException:
        capture(page, "error-" + role)
        print(role, "JavaScript errors:", errors[:10])
        print(role, "page:", page.locator("#content").inner_text()[:450])
        raise
    finally:
        context.close()


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        # Unauthenticated login is checked separately at desktop and phone widths.
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(ROOT, wait_until="networkidle")
        expect(page.locator("#login")).to_be_visible()
        capture(page, "desktop-login")
        page.set_viewport_size({"width": 390, "height": 844})
        assert_no_horizontal_overflow(page, "mobile login")
        capture(page, "mobile-login")
        context.close()

        for role, email in [
            ("dispatcher", "dispatcher@fieldpulse.local"),
            ("admin", "admin@fieldpulse.local"),
            ("client", "client-a@fieldpulse.local"),
            ("manager", "manager@fieldpulse.local"),
            ("engineer", "engineer1@fieldpulse.local"),
        ]:
            test_role(browser, role, email)
        browser.close()
    print("FieldPulse visual E2E: 5 roles, light/dark, filters, density, 390/1440px and screenshots — OK")


if __name__ == "__main__":
    main()
