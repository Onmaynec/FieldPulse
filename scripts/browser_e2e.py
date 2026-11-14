"""Browser end-to-end: offline PWA, two photos, reconnect and conflict.

Runs against a fully seeded DEMO stack. Creates a real ticket and attachments.
Requires Python Playwright and Chromium installed on the test runner.
"""
import json
import os
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from smoke import Browser

ROOT = os.getenv("SMOKE_URL", "http://127.0.0.1:8000")
PASSWORD = os.getenv("DEMO_PASSWORD", "FieldPulseDemo!2026")


def prepare():
    client, dispatcher = Browser(), Browser()
    client.login("client-a@fieldpulse.local")
    dispatcher.login("dispatcher@fieldpulse.local")
    site = client.request("GET", "/sites")[0]
    asset = client.request("GET", "/assets?site_id=" + site["id"])[0]
    ticket = client.request("POST", "/tickets", {
        "site_id": site["id"], "asset_id": asset["id"], "priority": "P2",
        "kind": "inspection", "description": "Browser offline scenario: inspect field equipment"
    }, headers={"Idempotency-Key": uuid.uuid4().hex}, expected=201)
    suggestions = dispatcher.request("GET", "/engineers/suggestions?ticket_id="+ticket["id"])
    available = next((x for x in suggestions if x["eligible"]), None)
    assert available, "No eligible engineer in seed"
    ticket = dispatcher.request("POST", "/tickets/"+ticket["id"]+"/assign", {
        "version": ticket["version"], "engineer_id": available["id"]
    })
    engineer = Browser()
    email = "engineer%d@fieldpulse.local" % int(available["name"].split()[-1])
    engineer.login(email)
    ticket = engineer.request("POST", "/tickets/"+ticket["id"]+"/accept",
                              {"version": ticket["version"]})
    ticket = engineer.request("POST", "/tickets/"+ticket["id"]+"/start",
                              {"version": ticket["version"]})
    return client, dispatcher, engineer, ticket, email


def assert_queue(page, expected):
    count = page.evaluate("async () => (await dbAll('queue')).length")
    assert count == expected, "Expected %d queued operations, found %s" % (expected, count)


def save_work(page, ticket_id, summary, with_photos=False):
    page.locator('button[data-work-ticket="%s"]' % ticket_id).click()
    page.locator('#work-form [name="summary"]').fill(summary)
    page.locator('#work-form input[name="check"]').evaluate_all(
        "(inputs) => inputs.forEach(box => box.checked = true)"
    )
    if with_photos:
        # Valid JPEG bytes, no filesystem fixture needed.
        from PIL import Image
        import io
        image = Image.new("RGB", (30, 30), (150, 68, 37))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        content = buffer.getvalue()
        page.locator('#work-form [name="photos"]').set_input_files([
            {"name": "before.jpg", "mimeType": "image/jpeg", "buffer": content},
            {"name": "after.jpg", "mimeType": "image/jpeg", "buffer": content},
        ])
    page.locator('#work-form button[type="submit"]').click()
    expect(page.locator("#modal-root")).to_be_empty(timeout=10000)


def main():
    client, dispatcher, engineer, ticket, email = prepare()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="allow")
        page = context.new_page()
        page.goto(ROOT, wait_until="networkidle", timeout=40000)
        expect(page.locator("#login")).to_be_visible()
        page.locator('#login-form [name="email"]').fill(email)
        page.locator('#login-form [name="password"]').fill(PASSWORD)
        page.locator('#login-form button[type="submit"]').click()
        expect(page.locator('button[data-work-ticket="%s"]' % ticket["id"])).to_be_visible(timeout=25000)
        page.evaluate("async () => { await navigator.serviceWorker.ready; return true; }")
        if not page.evaluate("() => Boolean(navigator.serviceWorker.controller)"):
            page.reload(wait_until="domcontentloaded")
        expect(page.locator('button[data-work-ticket="%s"]' % ticket["id"])).to_be_visible(timeout=25000)
        context.set_offline(True)
        save_work(page, ticket["id"], "Offline work with two images", with_photos=True)
        assert_queue(page, 1)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator('button[data-work-ticket="%s"]' % ticket["id"])).to_be_visible(timeout=15000)
        assert_queue(page, 1)
        context.set_offline(False)
        page.evaluate("async () => await sync()")
        page.wait_for_function("async () => (await dbAll('queue')).length === 0", timeout=40000)
        assert len(engineer.request("GET", "/tickets/"+ticket["id"]+"/attachments")) == 2
        logs = engineer.request("GET", "/tickets/"+ticket["id"]+"/worklogs")
        assert len(logs) == 1 and logs[0]["summary"] == "Offline work with two images"
        # A dispatcher changes status while the engineer is offline. No silent overwrite.
        context.set_offline(True)
        save_work(page, ticket["id"], "Offline note after cancellation")
        assert_queue(page, 1)
        current = dispatcher.request("GET", "/tickets/"+ticket["id"])
        dispatcher.request("POST", "/tickets/"+ticket["id"]+"/cancel",
                           {"version": current["version"]})
        context.set_offline(False)
        page.evaluate("async () => await sync()")
        page.wait_for_function(
            "async () => (await dbAll('queue')).some(x => x.conflict === 'VERSION_OR_STATUS')",
            timeout=30000
        )
        assert_queue(page, 1)
        browser.close()
    print("Browser offline PWA: two photos, persistent queue, reconnect and safe conflict — OK")


if __name__ == "__main__":
    main()
