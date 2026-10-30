"""Captures screenshots of the real UI on the real stack (set EKC_SCREENSHOT_DIR; skipped otherwise).

Not assertion-heavy: behaviour is covered in test_browser_flows.py. This exists so a reviewer can SEE
what the stack serves (docs/screenshots/). The same stand-ins apply (extractive answerer, in-memory
object store, deterministic embeddings): answers shown are extractive excerpts, not a real LLM."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from tests.e2e.conftest import Stack
from tests.e2e.test_browser_flows import ask, sign_in, upload_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("EKC_SCREENSHOT_DIR"), reason="EKC_SCREENSHOT_DIR not set"
    ),
]

DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}


def _shot(pg: Page, name: str, *, full: bool = True) -> None:
    out = Path(os.environ["EKC_SCREENSHOT_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    pg.wait_for_timeout(350)
    pg.screenshot(path=str(out / f"{name}.png"), full_page=full)


@pytest.fixture
def make_page(browser: Any, stack: Stack) -> Iterator[Any]:
    contexts: list[Any] = []

    def make(viewport: dict[str, int], scheme: str = "light") -> Page:
        ctx = browser.new_context(base_url=stack.base, viewport=viewport, color_scheme=scheme)
        contexts.append(ctx)
        p = ctx.new_page()
        p.set_default_timeout(20000)
        return p

    yield make
    for c in contexts:
        c.close()


def test_capture_desktop_light(make_page: Any, stack: Stack) -> None:
    page: Page = make_page(DESKTOP)
    t = stack.users["tenants"]["a"]
    page.goto("/login")
    _shot(page, "01-login")
    page.get_by_label("Organization", exact=True).fill(t["slug"])
    page.get_by_label("Email", exact=True).fill(t["users"]["analyst"]["email"])
    page.get_by_label("Password", exact=True).fill("wrong-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_be_visible()
    _shot(page, "02-login-error")

    page.goto("/signup")
    _shot(page, "03-signup")
    page.get_by_label("Organization name").fill("Northwind Traders")
    page.get_by_label("Your name").fill("Nia Okafor")
    page.get_by_label("Email", exact=True).fill("nia@example.com")
    page.get_by_label("Password", exact=True).fill("short")
    page.get_by_label("Confirm password").fill("short")
    page.get_by_role("button", name="Create organization").click()
    expect(page.get_by_text("Use at least 10 characters.")).to_be_visible()
    _shot(page, "04-signup-validation")

    slug = f"northwind-{uuid.uuid4().hex[:6]}"
    page.get_by_label("Organization identifier").fill(slug)
    page.get_by_label("Password", exact=True).fill("correct-horse-battery-9")
    page.get_by_label("Confirm password").fill("correct-horse-battery-9")
    page.get_by_role("button", name="Create organization").click()
    expect(page.get_by_role("heading", name="Ask a question")).to_be_visible()
    _shot(page, "05-signup-success-ask-empty")
    page.get_by_role("button", name="Sign out").click()

    sign_in(page, stack, "a", "analyst")
    page.get_by_role("link", name="Documents").click()
    expect(page.get_by_text("No documents yet.")).to_be_visible()
    _shot(page, "06-documents-empty")
    upload_policy(page)
    _shot(page, "07-documents-ready")
    ask(page)
    expect(page.locator("article.answer")).to_contain_text("30 days", timeout=30000)
    page.get_by_text("How this was routed").click()
    _shot(page, "08-ask-answer-with-citation")
    page.get_by_role("link", name="System status").click()
    expect(page.get_by_text("Running")).to_be_visible()
    _shot(page, "09-system-status")
    page.get_by_role("button", name="Sign out").click()

    sign_in(page, stack, "b", "analyst")
    ask(page)
    expect(page.locator("article.answer")).to_be_visible(timeout=30000)
    _shot(page, "10-ask-abstains-for-other-tenant")
    page.get_by_role("button", name="Sign out").click()

    sign_in(page, stack, "a", "admin")
    page.get_by_role("link", name="Usage").click()
    expect(page.get_by_text("Tokens used today")).to_be_visible()
    page.get_by_label("Tokens per day (empty = unlimited)").fill("500000")
    page.get_by_role("button", name="Save limits").click()
    expect(page.get_by_text("Limits saved.")).to_be_visible()
    _shot(page, "11-usage-and-budget")
    page.get_by_role("link", name="Audit").click()
    expect(page.get_by_role("cell", name="auth.login").first).to_be_visible()
    page.get_by_role("button", name="Verify integrity").click()
    expect(page.get_by_text(re.compile(r"Chain intact: \d+ events verified"))).to_be_visible()
    page.get_by_role("button", name=re.compile("Inspect event")).first.click()
    _shot(page, "12-audit-verified-with-detail")
    page.get_by_role("button", name="Collapse sidebar").click()
    _shot(page, "13-sidebar-collapsed")


def test_capture_dark_and_mobile(make_page: Any, stack: Stack) -> None:
    dark: Page = make_page(DESKTOP, "dark")
    dark.goto("/login")
    _shot(dark, "20-dark-login")
    sign_in(dark, stack, "a", "admin")
    dark.get_by_role("link", name="Documents").click()
    expect(dark.get_by_role("heading", name="Documents")).to_be_visible()
    _shot(dark, "21-dark-documents")
    dark.get_by_role("link", name="Audit").click()
    expect(dark.get_by_role("cell", name="auth.login").first).to_be_visible()
    _shot(dark, "22-dark-audit")

    phone: Page = make_page(PHONE)
    phone.goto("/login")
    _shot(phone, "30-mobile-login")
    sign_in(phone, stack, "a", "analyst")
    _shot(phone, "31-mobile-ask")
    phone.get_by_role("button", name="Open navigation menu").click()
    _shot(phone, "32-mobile-menu-open", full=False)
