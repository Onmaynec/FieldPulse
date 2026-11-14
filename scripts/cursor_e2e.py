"""Browser regression for live keyset navigation and complete dashboard totals."""
import os

from playwright.sync_api import expect, sync_playwright

ROOT = os.getenv("SMOKE_URL", "http://127.0.0.1:8000")
PASSWORD = os.getenv("DEMO_PASSWORD", "FieldPulseDemo!2026")


def sign_in(page, email):
    page.goto(ROOT, wait_until="domcontentloaded")
    expect(page.locator("#login")).to_be_visible(timeout=12000)
    page.locator('#login-form [name="email"]').fill(email)
    page.locator('#login-form [name="password"]').fill(PASSWORD)
    page.locator('#login-form button[type="submit"]').click()
    expect(page.locator("#shell")).to_be_visible(timeout=20000)
    expect(page.locator(".page-head h1")).to_be_visible(timeout=20000)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1320, "height": 860})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        sign_in(page, "dispatcher@fieldpulse.local")
        expect(page.locator(".stats .stat-card").first).to_be_visible()
        whole_total = page.evaluate("() => state.board.total")
        assert whole_total >= 2000, ("Dashboard is limited to 500", whole_total)
        assert page.evaluate("() => Object.values(state.board.by_status).reduce((a,b)=>a+b,0)") == whole_total
        page.locator('.nav-item[data-page="tickets"]').click()
        expect(page.locator("#ticket-table tbody tr")).to_have_count(20, timeout=18000)
        old_first = page.locator("#ticket-table tbody tr").first.inner_text()
        page.locator('[data-ticket-mode="seek"]').click()
        expect(page.locator('[data-ticket-mode="seek"]')).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#ticket-table tbody tr")).to_have_count(20, timeout=18000)
        assert page.evaluate("() => state.ticketMode") == "seek"
        expect(page.locator(".table-toolbar")).to_contain_text("без COUNT")
        page.locator('[data-seek-dir="next"]').click()
        expect(page.locator("#ticket-table .result-count").last).to_contain_text("Шаг 2", timeout=12000)
        assert page.evaluate("() => state.ticketCursors.length") == 1
        next_first = page.locator("#ticket-table tbody tr").first.inner_text()
        assert next_first != old_first
        page.locator('[data-seek-dir="prev"]').click()
        expect(page.locator("#ticket-table .result-count").last).to_contain_text("Шаг 1", timeout=12000)
        assert page.locator("#ticket-table tbody tr").first.inner_text() == old_first
        # Sorting and filters reset cursor history to avoid mixing datasets.
        page.locator('#ticket-sort').select_option("deadline")
        expect(page.locator("#ticket-table tbody tr").first).to_be_visible(timeout=12000)
        assert page.evaluate("() => state.ticketCursors.length") == 0
        page.locator('[data-seek-dir="next"]').click()
        expect(page.locator("#ticket-table .result-count").last).to_contain_text("Шаг 2", timeout=12000)
        page.locator('#ticket-priority-filter').select_option("P1")
        expect(page.locator("#ticket-table tbody tr").first).to_be_visible(timeout=12000)
        assert page.evaluate("() => state.ticketCursors.length") == 0
        assert all(t["priority"] == "P1" for t in page.evaluate("() => state.ticketData.items"))
        page.locator('[data-ticket-mode="offset"]').click()
        expect(page.locator(".table-toolbar")).to_contain_text("нумерованные страницы", timeout=18000)
        assert page.evaluate("() => state.ticketMode") == "offset"
        assert not errors, errors
        browser.close()
    print("Cursor E2E: server dashboard %s rows, keyset next/back, deadline, filter, mode switch — OK"
          % whole_total)


if __name__ == "__main__":
    main()
