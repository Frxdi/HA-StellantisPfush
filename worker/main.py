import time
import asyncio
import os
import json
from urllib.parse import urlsplit, parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
import uuid

app = FastAPI()

browser_process_id = None
process_start = None
browser_start = None
context_start = None
force_debug = False
ok_count = 0
ko_count = 0

playwright = None
browser = None
browser_lock = asyncio.Lock()

DEBUG_DIR = "/tmp/oauth_debug"

# --- Selectors -------------------------------------------------------------
# Login form (Gigya). Visible fields are preferred first; the rest are
# fallbacks seen on some brand/country variants.
LOGIN_EMAIL_SELECTORS = [
    '#gigya-login-form input[name="username"]',
    'form input[name="username"]',
    'input[type="email"]',
]
LOGIN_PASSWORD_SELECTORS = [
    '#gigya-login-form input[name="password"]',
    'form input[name="password"]',
    'input[type="password"]',
]
LOGIN_SUBMIT_SELECTORS = [
    '#gigya-login-form input[type="submit"]',
    '#gigya-login-form button[type="submit"]',
    'form input[type="submit"]',
    'form button[type="submit"]',
]

# Consent / authorize page. Stellantis has used several markups here
# (#cvs_from, #cvs_form, #consentbutton, plain forms...).
CONSENT_SELECTORS = [
    '#consentbutton',
    '#cvs_from input[type="submit"]',
    '#cvs_from button[type="submit"]',
    '#cvs_form input[type="submit"]',
    '#cvs_form button[type="submit"]',
    'form input[type="submit"]',
    'form button[type="submit"]',
]
CONSENT_FORM_SELECTORS = [
    '#cvs_from',
    '#cvs_form',
    'form[action*="/oauth2/authorize"]',
]


def log_process(message, process_id, force=force_debug):
    if force:
        print(f"[{process_id}] {message}")


def log_start_process(process_id):
    global process_start
    process_start = time.perf_counter()
    log_process("Process start", process_id, True)


def log_end_process(process_id):
    if process_start:
        log_process(f"Process end: {time.perf_counter() - process_start:.2f}s", process_id, True)
        log_process(f"Totals OK: {ok_count}", process_id, True)
        log_process(f"Totals KO: {ko_count}", process_id, True)


def log_start_browser():
    global browser_process_id, browser_start
    browser_process_id = uuid.uuid4().hex[:8]
    browser_start = time.perf_counter()
    log_process("Browser start", browser_process_id, True)


def log_end_browser():
    global browser_process_id
    if browser_start:
        log_process(f"Browser end: {time.perf_counter() - browser_start:.2f}s", browser_process_id, True)


def log_start_context(process_id):
    global context_start
    context_start = time.perf_counter()
    log_process("Context start", process_id)


def log_end_context(process_id):
    if context_start:
        log_process(f"Context end: {time.perf_counter() - context_start:.2f}s", process_id)


async def start_browser():
    global playwright, browser
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--disable-notifications",
            "--disable-default-apps",
            "--mute-audio",
            "--no-first-run",
            "--no-zygote"
        ],
    )
    log_start_browser()


@app.on_event("startup")
async def startup():
    async with browser_lock:
        await start_browser()


@app.on_event("shutdown")
async def shutdown():
    global playwright, browser
    if browser:
        await browser.close()
    if playwright:
        await playwright.stop()
    log_end_browser()


def http_response(message, process_id, status=400):
    global ok_count, ko_count
    if status == 200:
        ok_count += 1
        body = {"code": message}
    else:
        ko_count += 1
        body = {"message": f"{message} [{process_id}]", "code": status}
    log_process(f"Response: {message}", process_id)
    log_end_process(process_id)
    return JSONResponse(
        status_code=status,
        content=body,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    )


# --- Helpers added by the fix ----------------------------------------------

def extract_code_from_redirect(url: str):
    """
    Only accept the OAuth code from the final, non-HTTP redirect
    (custom app scheme, target 'oauth2redirect'). Intermediate HTTPS
    redirects (e.g. idpcvs.<brand>.com/.../OAuthProxy.jsp?...code=...)
    can also contain a 'code' parameter, but using that one results in
    'invalid_grant' from the token endpoint, so it must be ignored.
    """
    if url.startswith("http://") or url.startswith("https://"):
        return None
    if "oauth2redirect" not in url:
        return None
    try:
        query = url.split("?", 1)[1] if "?" in url else ""
        params = parse_qs(query)
        code_values = params.get("code")
        if code_values:
            return code_values[0]
    except Exception:
        return None
    return None


async def click_first_visible(page, selectors, timeout_input, process_id, label):
    """
    Try selectors one by one and only interact with the first one that is
    actually visible, instead of joining them with a comma (which can match
    a hidden Gigya template field first and silently fail/timeout).
    """
    last_error = None
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout_input)
            return locator, selector
        except Exception as e:
            last_error = e
            continue
    log_process(f"No visible selector found for {label}: {selectors}", process_id, True)
    raise last_error or Exception(f"No visible selector found for {label}: {selectors}")


async def submit_consent_form(page, process_id, timeout_input):
    """
    Click a visible consent/authorize button if we can find one. If Playwright
    can't locate a visible submit control (some brand/country variants render
    it oddly), fall back to calling requestSubmit() on the known form
    selectors via JS.
    """
    try:
        locator, selector = await click_first_visible(
            page, CONSENT_SELECTORS, timeout_input, process_id, "consent button"
        )
        await locator.click()
        log_process(f"Clicked consent selector: {selector}", process_id)
        return
    except Exception as e:
        log_process(f"Visible consent button not found, trying requestSubmit() fallback: {e}", process_id, True)

    for form_selector in CONSENT_FORM_SELECTORS:
        try:
            clicked = await page.evaluate(
                """(sel) => {
                    const form = document.querySelector(sel);
                    if (form && typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                        return true;
                    }
                    if (form) {
                        form.submit();
                        return true;
                    }
                    return false;
                }""",
                form_selector,
            )
            if clicked:
                log_process(f"Submitted consent form via JS fallback: {form_selector}", process_id)
                return
        except Exception as e:
            log_process(f"requestSubmit() fallback failed for {form_selector}: {e}", process_id)
            continue

    raise Exception("Could not find or submit the consent form")


async def save_debug_artifacts(page, process_id):
    """
    Save redacted debug info on failure: current URL, a short body snippet,
    a screenshot and the page HTML. No credentials or OAuth tokens are
    logged; the URL's query string is stripped before saving.
    """
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        current_url = page.url
        safe_url = current_url.split("?", 1)[0]

        html = await page.content()
        body_snippet = html[:2000]

        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{process_id}.png"), full_page=True)
        with open(os.path.join(DEBUG_DIR, f"{process_id}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        with open(os.path.join(DEBUG_DIR, f"{process_id}.json"), "w", encoding="utf-8") as f:
            json.dump({"url": safe_url, "body_snippet": body_snippet}, f, indent=2)

        log_process(f"Saved debug artifacts to {DEBUG_DIR}/{process_id}.*", process_id, True)
    except Exception as e:
        log_process(f"Failed to save debug artifacts: {e}", process_id, True)


@app.post("/")
async def fetch(request: Request):
    global force_debug
    process_id = uuid.uuid4().hex[:8]
    log_start_process(process_id)

    context = None
    captured_code = None
    page = None

    try:
        payload = await request.json()
        url = payload.get("url")
        email = payload.get("email")
        password = payload.get("password")
        timeout_page = payload.get("timeout_page", 50000)
        timeout_input = payload.get("timeout_input", 50000)
        force_debug = payload.get("debug", False)

        if not url or not email or not password:
            return http_response("Missing required params", process_id)

        async with browser_lock:
            log_start_context(process_id)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
                bypass_csp=True,
                ignore_https_errors=True,
            )
            page = await context.new_page()

            def on_request_failed(req):
                nonlocal captured_code
                if captured_code:
                    return
                code = extract_code_from_redirect(req.url)
                if code:
                    captured_code = code
                    log_process("Code captured!", process_id, True)

            page.on("requestfailed", on_request_failed)

            log_process(f"Navigating to login: {url}", process_id)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_page)

            log_process("Waiting for login form...", process_id)
            email_locator, email_selector = await click_first_visible(
                page, LOGIN_EMAIL_SELECTORS, timeout_input, process_id, "email field"
            )
            password_locator, password_selector = await click_first_visible(
                page, LOGIN_PASSWORD_SELECTORS, timeout_input, process_id, "password field"
            )

            log_process("Filling credentials...", process_id)
            await email_locator.fill("")
            await email_locator.type(email, delay=50)
            await password_locator.fill("")
            await password_locator.type(password, delay=50)

            log_process("Submitting login form...", process_id)
            submit_locator, submit_selector = await click_first_visible(
                page, LOGIN_SUBMIT_SELECTORS, timeout_input, process_id, "login submit button"
            )
            await submit_locator.click()

            log_process("Waiting for redirects...", process_id)
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_page)

            log_process("Waiting for consent page...", process_id)
            await submit_consent_form(page, process_id, timeout_input)

            log_process("Polling for code capture...", process_id)
            deadline = time.perf_counter() + (timeout_page / 1000)
            while not captured_code and time.perf_counter() < deadline:
                await asyncio.sleep(0.25)

            await context.close()
            log_end_context(process_id)

            if captured_code:
                return http_response(captured_code, process_id, 200)
            return http_response("Code not found", process_id)

    except Exception as e:
        log_process(f"Error: {e}", process_id, True)
        if page and not page.is_closed():
            await save_debug_artifacts(page, process_id)
        if context:
            await context.close()
            log_end_context(process_id)
        if captured_code:
            return http_response(captured_code, process_id, 200)
        return http_response(str(e), process_id)


@app.get("/health")
async def healthcheck():
    global playwright, browser
    process_id = uuid.uuid4().hex[:8]
    log_start_process(process_id)
    async with browser_lock:
        log_process("Check browser", process_id, True)
        try:
            context = await asyncio.wait_for(
                browser.new_context(),
                timeout=10000
            )
            page = await context.new_page()
            await page.goto("about:blank", timeout=10000)
            await context.close()
        except Exception as e:
            log_process(f"Restarting browser: {e}", process_id, True)
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                if playwright:
                    await playwright.stop()
            except Exception:
                pass
            await start_browser()
    log_end_process(process_id)
    return {
        "status": "ok"
    }
