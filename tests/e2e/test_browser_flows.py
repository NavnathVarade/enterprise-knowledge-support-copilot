"""Browser end-to-end tests: a real Chromium drives the real UI through the real gateway to the
real API and database. See harness.py for exactly which parts are stand-ins."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect
from scripts.smoke_test import run as run_smoke
from tests.e2e.conftest import Stack

pytestmark = pytest.mark.e2e

POLICY = (
    b"# Refund Policy\n\n## Eligibility\n\n"
    b"Customers may request a refund within 30 days of purchase."
)
QUESTION = "What is the refund policy for customers?"


@pytest.fixture
def page(browser: Any, stack: Stack) -> Iterator[Page]:
    context = browser.new_context(base_url=stack.base, viewport={"width": 1100, "height": 800})
    p = context.new_page()
    p.csp_violations = []  # type: ignore[attr-defined]
    p.page_errors = []  # type: ignore[attr-defined]
    p.on(
        "console",
        lambda m: (
            p.csp_violations.append(m.text)  # type: ignore[attr-defined]
            if m.type == "error" and "Content Security Policy" in m.text
            else None
        ),
    )
    p.on("pageerror", lambda e: p.page_errors.append(str(e)))  # type: ignore[attr-defined]
    p.set_default_timeout(15000)
    yield p
    assert p.csp_violations == [], p.csp_violations  # type: ignore[attr-defined]
    assert p.page_errors == [], p.page_errors  # type: ignore[attr-defined]
    context.close()


def sign_in(page: Page, stack: Stack, tenant: str, who: str) -> None:
    t = stack.users["tenants"][tenant]
    u = t["users"][who]
    page.goto("/login")
    page.get_by_label("Organization", exact=True).fill(t["slug"])
    page.get_by_label("Email", exact=True).fill(u["email"])
    page.get_by_label("Password", exact=True).fill(u["password"])
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Ask a question")).to_be_visible()


def upload_policy(page: Page, name: str = "policy.md") -> None:
    page.get_by_role("link", name="Documents").click()
    page.set_input_files(
        'input[type="file"]', {"name": name, "mimeType": "text/markdown", "buffer": POLICY}
    )
    row = page.get_by_role("row", name=re.compile(re.escape(name)))
    expect(row).to_be_visible()
    # the UI polls every 5 s while the document is being processed
    expect(row.get_by_text("Ready")).to_be_visible(timeout=45000)


def ask(page: Page, question: str = QUESTION) -> None:
    page.get_by_role("link", name="Ask").click()
    page.get_by_label("Your question").fill(question)
    page.get_by_role("button", name="Ask", exact=True).click()


# ---------------------------------------------------------------- the main user journey


def test_upload_wait_for_ingestion_then_ask_and_get_a_cited_answer(
    page: Page, stack: Stack
) -> None:
    sign_in(page, stack, "a", "analyst")
    upload_policy(page)
    ask(page)
    answer = page.locator("article.answer")
    expect(answer).to_contain_text("30 days", timeout=30000)
    expect(answer.get_by_text("answered")).to_be_visible()
    sources = answer.locator("ol li")
    expect(sources).to_have_count(1)
    expect(sources.first).to_contain_text("policy.md")
    expect(sources.first).to_contain_text("version 1")


def test_another_tenant_cannot_see_or_retrieve_those_documents(page: Page, stack: Stack) -> None:
    sign_in(page, stack, "a", "analyst")
    upload_policy(page, "tenant-a-only.md")
    page.get_by_role("button", name="Sign out").click()

    sign_in(page, stack, "b", "analyst")
    page.get_by_role("link", name="Documents").click()
    expect(page.get_by_text("No documents yet.")).to_be_visible()
    ask(page)
    expect(page.locator("article.answer")).to_be_visible(timeout=30000)
    body = page.locator("body").inner_text()
    assert "tenant-a-only" not in body and "30 days" not in body
    expect(page.locator("article.answer .badge")).to_have_text(
        re.compile("abstained|not supported")
    )


# ---------------------------------------------------------------- authentication


def test_wrong_password_shows_a_safe_error_and_stores_nothing(page: Page, stack: Stack) -> None:
    t = stack.users["tenants"]["a"]
    page.goto("/login")
    page.get_by_label("Organization", exact=True).fill(t["slug"])
    page.get_by_label("Email", exact=True).fill(t["users"]["analyst"]["email"])
    page.get_by_label("Password", exact=True).fill("definitely-wrong")
    page.get_by_role("button", name="Sign in").click()
    alert = page.get_by_role("alert")
    expect(alert).to_contain_text("Invalid tenant, email, or password.")
    expect(alert).to_contain_text("Reference:")  # the request id support can look up
    expect(page.get_by_label("Password", exact=True)).to_have_value("")
    assert page.evaluate("sessionStorage.getItem('ekc.session')") is None
    assert page.evaluate("localStorage.length") == 0


def test_unknown_tenant_and_unknown_user_get_the_identical_message(
    page: Page, stack: Stack
) -> None:
    t = stack.users["tenants"]["a"]
    messages = []
    for slug, email in (("no-such-org", "x@example.com"), (t["slug"], "nobody@example.com")):
        page.goto("/login")
        page.get_by_label("Organization", exact=True).fill(slug)
        page.get_by_label("Email", exact=True).fill(email)
        page.get_by_label("Password", exact=True).fill("whatever-123")
        page.get_by_role("button", name="Sign in").click()
        expect(page.get_by_role("alert")).to_be_visible()
        messages.append(page.get_by_role("alert").locator("p").first.inner_text())
    assert messages[0] == messages[1]  # no account-enumeration oracle in the UI either


def test_anonymous_deep_link_goes_to_login_and_returns_after_sign_in(
    page: Page, stack: Stack
) -> None:
    page.goto("/documents")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    t = stack.users["tenants"]["a"]
    page.get_by_label("Organization", exact=True).fill(t["slug"])
    page.get_by_label("Email", exact=True).fill(t["users"]["analyst"]["email"])
    page.get_by_label("Password", exact=True).fill(t["users"]["analyst"]["password"])
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Documents")).to_be_visible()


def test_sign_out_clears_the_session(page: Page, stack: Stack) -> None:
    sign_in(page, stack, "a", "analyst")
    assert page.evaluate("sessionStorage.getItem('ekc.session')") is not None
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert page.evaluate("sessionStorage.getItem('ekc.session')") is None
    page.goto("/documents")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()


def test_expired_session_is_dropped_on_reload(page: Page, stack: Stack) -> None:
    sign_in(page, stack, "a", "analyst")
    page.evaluate(
        "const s = JSON.parse(sessionStorage.getItem('ekc.session'));"
        "s.expiresAt = Date.now() - 1000; sessionStorage.setItem('ekc.session', JSON.stringify(s));"
    )
    page.reload()
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()


def test_a_token_the_server_rejects_ends_the_session_with_a_notice(
    page: Page, stack: Stack
) -> None:
    sign_in(page, stack, "a", "analyst")
    page.evaluate(
        "const s = JSON.parse(sessionStorage.getItem('ekc.session'));"
        "s.token = s.token.slice(0, -4) + 'AAAA'; sessionStorage.setItem('ekc.session', JSON.stringify(s));"
    )
    page.reload()  # still looks structurally valid to the client; the signature is now wrong
    page.get_by_role("link", name="Documents").click()
    expect(page.get_by_text("Your session ended. Please sign in again.")).to_be_visible()


# ---------------------------------------------------------------- authorization


def test_ordinary_user_has_no_admin_navigation_and_the_server_refuses_too(
    page: Page, stack: Stack
) -> None:
    sign_in(page, stack, "a", "analyst")
    nav = page.get_by_role("navigation", name="Main")
    expect(nav.get_by_role("link", name="Usage")).to_have_count(0)
    expect(nav.get_by_role("link", name="Audit")).to_have_count(0)
    page.goto("/audit")  # a full reload: the session survives in sessionStorage
    expect(page.get_by_text("Your account does not have access to this page.")).to_be_visible()
    # the UI check is cosmetic: prove the API itself refuses the same token
    status = page.evaluate(
        """async () => {
            const t = JSON.parse(sessionStorage.getItem('ekc.session')).token;
            const r = await fetch('/api/v1/audit', {headers: {Authorization: 'Bearer ' + t}});
            return r.status;
        }"""
    )
    assert status == 403


def test_auditor_sees_audit_but_not_usage(page: Page, stack: Stack) -> None:
    sign_in(page, stack, "a", "auditor")
    nav = page.get_by_role("navigation", name="Main")
    expect(nav.get_by_role("link", name="Audit")).to_be_visible()
    expect(nav.get_by_role("link", name="Usage")).to_have_count(0)


def test_admin_audit_trail_records_activity_and_the_chain_verifies(
    page: Page, stack: Stack
) -> None:
    sign_in(page, stack, "a", "analyst")  # generate at least one audited login
    page.get_by_role("button", name="Sign out").click()
    sign_in(page, stack, "a", "admin")
    page.get_by_role("link", name="Audit").click()
    expect(page.get_by_role("cell", name="auth.login").first).to_be_visible()
    page.get_by_role("button", name="Verify integrity").click()
    expect(page.get_by_text(re.compile(r"Chain intact: \d+ events verified"))).to_be_visible()


def test_admin_can_view_usage_and_save_a_budget(page: Page, stack: Stack) -> None:
    sign_in(page, stack, "a", "admin")
    page.get_by_role("link", name="Usage").click()
    expect(page.get_by_role("heading", name="Usage and budget")).to_be_visible()
    page.get_by_label("Tokens per day (empty = unlimited)").fill("500000")
    page.get_by_role("button", name="Save limits").click()
    expect(page.get_by_text("Limits saved.")).to_be_visible()
    page.reload()
    expect(page.get_by_label("Tokens per day (empty = unlimited)")).to_have_value("500000")
    page.get_by_label("Tokens per day (empty = unlimited)").fill("")  # restore: unlimited
    page.get_by_role("button", name="Save limits").click()
    # removing every limit is guarded by a confirmation
    page.get_by_role("dialog").get_by_role("button", name="Remove limits").click()
    expect(page.get_by_text("Limits saved.")).to_be_visible()


def test_invalid_budget_input_is_rejected_in_the_browser(page: Page, stack: Stack) -> None:
    sign_in(page, stack, "a", "admin")
    page.get_by_role("link", name="Usage").click()
    page.get_by_label("Tokens per day (empty = unlimited)").fill("-5")
    page.get_by_role("button", name="Save limits").click()
    expect(page.get_by_role("alert")).to_contain_text("Limits must be positive")


# ---------------------------------------------------------------- uploads


def test_unsupported_file_is_rejected_before_any_upload(page: Page, stack: Stack) -> None:
    sign_in(page, stack, "a", "analyst")
    page.get_by_role("link", name="Documents").click()
    uploads: list[str] = []
    page.on("request", lambda r: uploads.append(r.url) if r.method == "POST" else None)
    page.set_input_files(
        'input[type="file"]',
        {"name": "run.exe", "mimeType": "application/octet-stream", "buffer": b"MZ"},
    )
    expect(page.get_by_role("alert")).to_contain_text("Unsupported file type")
    assert uploads == []


def test_content_that_does_not_match_its_claimed_type_is_refused_by_the_server(
    page: Page, stack: Stack
) -> None:
    sign_in(page, stack, "a", "analyst")
    page.get_by_role("link", name="Documents").click()
    page.set_input_files(
        'input[type="file"]',
        {"name": "fake.pdf", "mimeType": "application/pdf", "buffer": b"this is not a pdf at all"},
    )
    # passes the client's extension check, so the SERVER's content sniffing must stop it
    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.get_by_role("row", name=re.compile("fake.pdf"))).to_have_count(0)


# ---------------------------------------------------------------- the edge, seen from a browser


def test_security_headers_and_routing_as_a_browser_sees_them(page: Page, stack: Stack) -> None:
    resp = page.goto("/")
    assert resp is not None
    csp = resp.headers["content-security-policy"]
    assert (
        "script-src 'self'" in csp
        and "unsafe-inline" not in csp
        and "frame-ancestors 'none'" in csp
    )
    assert httpx.get(f"{stack.base}/metrics").status_code == 404
    assert httpx.get(f"{stack.base}/api/v1/health/live").json() == {"status": "alive"}
    assert httpx.get(f"{stack.base}/assets/nonexistent.js").status_code == 404
    # inline script injection is blocked by the CSP: the page must NOT execute it
    page.evaluate(
        "() => { const s = document.createElement('script'); s.textContent = 'window.__pwned = 1';"
        " document.body.appendChild(s); }"
    )
    assert page.evaluate("window.__pwned") is None
    page.csp_violations.clear()  # type: ignore[attr-defined]  # the violation above was intentional


def test_post_deploy_smoke_test_passes_against_the_full_stack(stack: Stack) -> None:
    results = run_smoke(stack.base, production=False)
    failed = [r for r in results if not r.ok]
    assert not failed, failed


# ---------------------------------------------------------------- signup and shell


def test_a_new_organization_can_sign_up_then_sign_in_and_is_isolated(
    page: Page, stack: Stack
) -> None:
    import uuid

    slug = f"e2e-{uuid.uuid4().hex[:8]}"
    page.goto("/signup")
    page.get_by_label("Organization name").fill("E2E Signup Org")
    expect(page.get_by_label("Organization identifier")).to_have_value("e2e-signup-org")
    page.get_by_label("Organization identifier").fill(slug)
    page.get_by_label("Your name").fill("Sam Signup")
    page.get_by_label("Email", exact=True).fill("sam@example.com")
    page.get_by_label("Password", exact=True).fill("correct-horse-battery-9")
    page.get_by_label("Confirm password").fill("correct-horse-battery-9")
    page.get_by_role("button", name="Create organization").click()
    expect(page.get_by_role("heading", name="Ask a question")).to_be_visible()
    # the creator is an administrator of the NEW organization and of nothing else
    nav = page.get_by_role("navigation", name="Main")
    expect(nav.get_by_role("link", name="Usage")).to_be_visible()
    expect(nav.get_by_role("link", name="Audit")).to_be_visible()
    page.get_by_role("link", name="Documents").click()
    expect(page.get_by_text("No documents yet.")).to_be_visible()

    # the identifier + password work through the normal sign-in page
    page.get_by_role("button", name="Sign out").click()
    page.get_by_label("Organization", exact=True).fill(slug)
    page.get_by_label("Email", exact=True).fill("sam@example.com")
    page.get_by_label("Password", exact=True).fill("correct-horse-battery-9")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Ask a question")).to_be_visible()


def test_signup_rejects_a_taken_identifier_and_weak_passwords(page: Page, stack: Stack) -> None:
    taken = stack.users["tenants"]["a"]["slug"]
    page.goto("/signup")
    page.get_by_label("Organization name").fill("Copycat")
    page.get_by_label("Organization identifier").fill(taken)
    page.get_by_label("Your name").fill("Mallory")
    page.get_by_label("Email", exact=True).fill("mallory@example.com")
    page.get_by_label("Password", exact=True).fill("short")
    page.get_by_label("Confirm password").fill("short")
    page.get_by_role("button", name="Create organization").click()
    expect(
        page.get_by_text("Use at least 10 characters.")
    ).to_be_visible()  # client-side, no request
    page.get_by_label("Password", exact=True).fill("correct-horse-battery-9")
    page.get_by_label("Confirm password").fill("correct-horse-battery-9")
    page.get_by_role("button", name="Create organization").click()
    expect(page.get_by_text("already in use")).to_be_visible()  # server-side, on the field
    expect(page.get_by_role("heading", name="Create your organization")).to_be_visible()


def test_mobile_navigation_dark_theme_and_system_status(browser: Any, stack: Stack) -> None:
    context = browser.new_context(base_url=stack.base, viewport={"width": 390, "height": 800})
    p = context.new_page()
    p.set_default_timeout(15000)
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    try:
        sign_in(p, stack, "a", "analyst")
        # no horizontal page scroll at phone width
        assert p.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        expect(p.get_by_role("navigation", name="Main")).not_to_be_in_viewport()
        p.get_by_role("button", name="Open navigation menu").click()
        expect(p.get_by_role("navigation", name="Main")).to_be_in_viewport()
        p.get_by_role("link", name="System status").click()
        expect(p.get_by_role("heading", name="System status")).to_be_visible()
        expect(p.get_by_text("Running")).to_be_visible()
        expect(p.get_by_role("cell", name="database", exact=True)).to_be_visible()
        # the drawer closes after navigating
        expect(p.get_by_role("navigation", name="Main")).not_to_be_in_viewport()
        p.get_by_role("button", name="Open navigation menu").click()
        p.get_by_role("button", name="Dark theme").click()
        assert p.evaluate("document.documentElement.dataset.theme") == "dark"
        p.reload()
        assert p.evaluate("document.documentElement.dataset.theme") == "dark"  # remembered
        # the session token is tab-scoped sessionStorage only
        assert "eyJ" not in p.evaluate("JSON.stringify({...localStorage})")
    finally:
        context.close()
    assert errors == [], errors
