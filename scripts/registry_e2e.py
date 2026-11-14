"""Interactive v0.5 server registry: real 2000+ tickets and saved views."""
import os
from playwright.sync_api import sync_playwright, expect

ROOT = os.getenv("SMOKE_URL", "http://127.0.0.1:8000")
PASSWORD = os.getenv("DEMO_PASSWORD", "FieldPulseDemo!2026")


def main():
    with sync_playwright() as tool:
        browser = tool.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 850})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(ROOT, wait_until="domcontentloaded")
        expect(page.locator("#login")).to_be_visible(timeout=12000)
        page.locator('#login-form [name="email"]').fill("dispatcher@fieldpulse.local")
        page.locator('#login-form [name="password"]').fill(PASSWORD)
        page.locator('#login-form button[type="submit"]').click()
        expect(page.locator("#shell")).to_be_visible(timeout=20000)
        page.locator('.nav-item[data-page="tickets"]').click()
        expect(page.locator('#ticket-table tbody tr')).to_have_count(20, timeout=20000)
        assert page.evaluate("() => state.ticketData.total") > 500, (
            "Registry is still limited to 500 records"
        )
        total = page.evaluate("() => state.ticketData.total")
        # All requests after this point go through a server-side page endpoint.
        page.locator('button[data-ticket-page="2"]').click()
        expect(page.locator("#ticket-table .result-count").last).to_contain_text("2 /", timeout=15000)
        assert page.evaluate("() => state.ticketData.page") == 2
        page.locator('#ticket-priority-filter').select_option("P1")
        expect(page.locator("#ticket-table tbody tr").first).to_be_visible(timeout=15000)
        assert page.evaluate("() => state.ticketData.total") > 0
        page.locator('#ticket-status-filter').select_option("new")
        expect(page.locator("#ticket-table tbody tr").first).to_be_visible(timeout=15000)
        assert all(item["priority"] == "P1" and item["status"] == "new" for item in
                   page.evaluate("() => state.ticketData.items"))

        # Save an account-owned filter, leave the registry and restore it.
        page.locator("[data-save-ticket-view]").click()
        expect(page.locator("#ticket-view-form")).to_be_visible()
        page.locator('#ticket-view-form [name="name"]').fill("CI · новые P1")
        page.locator('#ticket-view-form button[type="submit"]').click()
        expect(page.locator(".saved-views [data-ticket-view]")).to_have_count(2, timeout=12000)
        saved_id = page.evaluate("() => state.ticketViews[0].id")
        assert saved_id
        page.locator('.nav-item[data-page="overview"]').click()
        expect(page.locator(".page-head h1")).to_be_visible()
        page.locator('.nav-item[data-page="tickets"]').click()
        page.locator('[data-ticket-view="%s"]' % saved_id).click()
        expect(page.locator('#ticket-status-filter')).to_have_value("new")
        expect(page.locator('#ticket-priority-filter')).to_have_value("P1")
        expect(page.locator("#ticket-table tbody tr").first).to_be_visible(timeout=15000)
        assert page.evaluate("() => state.ticketData.total") > 0

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator('[data-delete-ticket-view="%s"]' % saved_id).click()
        expect(page.locator('[data-ticket-view="%s"]' % saved_id)).to_have_count(0)
        assert page.evaluate("() => state.ticketViews.length") == 0
        assert not errors, errors
        context.close()
        browser.close()
        print("Registry v0.5: %d tickets, paging, search filters, personal view save/restore/delete — OK" % total)


if __name__ == "__main__":
    main()
