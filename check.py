import os
import re
import sys
import threading
import time
import urllib.request
import urllib.parse

from playwright.sync_api import sync_playwright

CB_EMAIL = os.environ["CB_EMAIL"]
CB_PASSWORD = os.environ["CB_PASSWORD"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

STATE_PATH = os.environ.get("STATE_PATH", "state.json")
FLAG_PATH = os.environ.get("FLAG_PATH", "started.flag")
BLOCK_FLAG_PATH = FLAG_PATH + ".blocked_alerted"
# all state files share the FLAG_PATH prefix so watch.yml's `started.flag*`
# cache glob persists every one of them between runs without a per-file list
ERROR_ALERT_PATH = FLAG_PATH + ".error_alerted"
BLIND_COUNT_PATH = FLAG_PATH + ".blind_count"
BLIND_ALERT_PATH = FLAG_PATH + ".blind_alerted"
UNKNOWN_ALERT_PATH = FLAG_PATH + ".unknown_alerted"

TEST_DATE_BUTTON_ID = "qc-id-selectdatecenter-testdate-button-DEC-05"
# the results page repeats the selected date ("Saturday, December 5, 2026") --
# requiring it guards against reading results for a different date the site
# remembered from an earlier (e.g. manual) session
TEST_DATE_RE = re.compile(r"Dec(ember)?\.? 5,? 2026")

REALISTIC_USER_AGENT = (
    # must match the ACTUAL platform (this runs on Linux in GitHub Actions) --
    # a Windows/Mac UA string paired with navigator.platform/WebGL still
    # reporting Linux is a cross-signal mismatch that bot-detection heuristics
    # specifically look for, which would be worse than just using the default.
    # The version must match the real engine too (filled in from
    # browser.version at launch): a pinned "Chrome/130" next to the
    # sec-ch-ua client hint the browser itself sends (v=153 when this was
    # written, and rising with every unpinned `pip install playwright`) is the
    # same kind of mismatch.
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)

# Markers for common bot-protection vendors (Akamai, Cloudflare, PerimeterX/HUMAN,
# Imperva) -- none of these phrases appear anywhere in the legitimate flow, so
# matching here is a safe, low-false-positive signal that the site is actively
# challenging/blocking us rather than just rendering something unexpected.
BLOCK_MARKERS = [
    "Access Denied", "Pardon the Interruption", "Request unsuccessful",
    "Incapsula incident", "unusual traffic from your computer",
    "Checking your browser before accessing", "Attention Required! | Cloudflare",
    "Ray ID", "verify you are a human", "px-captcha",
]


def looks_blocked(text):
    low = text.lower()
    return any(m.lower() in low for m in BLOCK_MARKERS)


def tg(text):
    # the single most important message this whole system sends (seat found)
    # goes through here -- a bare one-shot call with no retry means one
    # transient network/API blip at exactly the wrong moment loses the alert
    # with nobody ever finding out, which is precisely what must never happen.
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text}).encode()
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
            return True
        except Exception as e:
            print(f"TG SEND FAILED (attempt {attempt + 1}/3):", e)
            if attempt < 2:
                time.sleep(3)
    return False


def read_ts(path):
    # timestamp stored INSIDE the file -- file mtimes aren't something to rely
    # on after an actions/cache round-trip
    try:
        with open(path) as f:
            return float(f.read().strip() or 0)
    except (OSError, ValueError):
        return None


def write_ts(path):
    with open(path, "w") as f:
        f.write(str(time.time()))


def remove_flag(path):
    if os.path.exists(path):
        os.remove(path)


def tg_throttled(flag_path, text, min_interval):
    # repeating the same non-seat alert every 5 minutes trains the owner to mute
    # the bot -- and a muted bot is how the one alert that matters (seat found)
    # gets missed. Send once, then at most every min_interval while it persists.
    last = read_ts(flag_path)
    if last is not None and time.time() - last < min_interval:
        log(f"alert throttled (last sent {int(time.time() - last)}s ago):", text[:100])
        return
    if tg(text):
        write_ts(flag_path)


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def force_render(page):
    try:
        page.evaluate("""
            () => {
                window.dispatchEvent(new Event('focus'));
                document.dispatchEvent(new Event('visibilitychange'));
                window.dispatchEvent(new Event('pageshow'));
            }
        """)
    except Exception:
        pass


def needs_login(page):
    url = page.url
    return "collegeboard.org/login" in url or "idp.collegeboard.org" in url


def do_login(page):
    log("logging in fresh")
    page.goto("https://account.collegeboard.org/login/login", wait_until="domcontentloaded")
    page.bring_to_front()
    time.sleep(1)
    try:
        page.wait_for_selector("#input28", timeout=8000)
    except Exception:
        log("no email field shown -- likely already authenticated, skipping login")
        return
    page.fill("#input28", CB_EMAIL)
    page.click("input[type=submit]", force=True)
    try:
        page.wait_for_selector("text=Select from the following options", timeout=10000)
        page.click("a[aria-label='Select Password.']", force=True)
    except Exception:
        log("DEBUG do_login: no method-picker shown, assuming password field appears directly")
    pw_input = page.wait_for_selector("input[type=password]", timeout=20000)
    pw_input.fill(CB_PASSWORD)
    page.click("input[type=submit]", force=True)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        # this site can keep background polling/websocket activity alive
        # indefinitely, which makes networkidle time out even on a successful
        # login -- the equivalent wait elsewhere in this file (full re-login
        # path) already tolerates this the same way; do_login() itself was
        # missing it, risking an uncaught exception that would report a
        # perfectly fine login as a FATAL error.
        log("DEBUG do_login: networkidle wait timed out, proceeding anyway")
    if "Unable to sign in" in page.content():
        raise RuntimeError("LOGIN_FAILED_BAD_PASSWORD")
    log("login ok")


def accept_terms_if_present(page):
    try:
        cb = page.query_selector("#terms-acceptance-checkbox")
        if cb and not cb.is_checked():
            box = page.query_selector("#qc-id-termsconditions-scrollbox-termsconditions")
            if box:
                page.evaluate("(el) => { el.scrollTop = el.scrollHeight; }", box)
                time.sleep(1)
            cb.click(force=True)
            page.click("#qc-id-termsconditions-button-continue", force=True)
            time.sleep(2)
    except Exception as e:
        log("terms step skipped/failed (may not be present):", e)


def looks_authenticated(content):
    markers = ["Get Started", "Select Date and Test Center", "Choose your test location",
               "test-center-country-city-input", "Find a Test Center"]
    return any(m in content for m in markers)


def dbg(page, label):
    page.bring_to_front()
    for attempt in range(3):
        try:
            time.sleep(0.5)
            c = page.content()
            t = page.inner_text("body")
            log(f"DEBUG[{label}] url={page.url} content_len={len(c)} text_len={len(t)} text_preview={t[:150].replace(chr(10),' ')}")
            return c, t
        except Exception as e:
            log(f"DEBUG[{label}] retry {attempt} after error: {e}")
            time.sleep(2)
    return "", ""


def click_first(page, selectors):
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            el.click(force=True, timeout=10000)
            return sel
    return None


def dismiss_cookie_banner(page):
    try:
        btn = page.query_selector("#onetrust-accept-btn-handler, .accept-recommended-btn-handler, button:has-text('Accept All')")
        if btn:
            btn.click(force=True, timeout=5000)
            time.sleep(1)
    except Exception as e:
        log("cookie dismiss skipped:", e)


def click_maybe_newtab(context, page, selectors):
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            try:
                with context.expect_page(timeout=4000) as new_page_info:
                    el.click(force=True, timeout=10000)
                new_page = new_page_info.value
                new_page.wait_for_load_state("domcontentloaded", timeout=20000)
                return new_page, sel
            except Exception:
                return page, sel
    return page, None


def click_el_maybe_newtab(context, page, el, label):
    # element-handle variant of click_maybe_newtab, for the reserve clicks
    try:
        with context.expect_page(timeout=4000) as new_page_info:
            el.click(force=True, timeout=10000)
        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=20000)
        log(f"DEBUG {label} click opened a new tab:", new_page.url)
        return new_page
    except Exception as e:
        # the usual case is simply "no new tab within 4s"; log anything else
        # (including the click itself timing out) -- this is the reserve path
        if 'waiting for event "page"' not in str(e):
            log(f"DEBUG {label} click raised:", repr(e)[:200])
        return page


# The page-level Continue for the reserve step: enabled, actually rendered, and
# not a stale/foreign one (session-timeout modal, earlier wizard steps). Falls back
# to a rendered, enabled button labelled exactly "Continue" in case the id has no
# "continue" in it.
FIND_CONTINUE_JS = r"""
() => {
  const usable = e => e.getClientRects().length > 0 && !e.disabled
                      && e.getAttribute('aria-disabled') !== 'true';
  const byId = Array.from(document.querySelectorAll('[id*=continue], [id*=Continue]')).find(e =>
    e.id && !/session|timeout|personalinfo|testdate|termsconditions/i.test(e.id) && usable(e));
  if (byId) return byId;
  return Array.from(document.querySelectorAll('button')).find(b =>
    (b.innerText || '').trim() === 'Continue' && !b.getAttribute('aria-describedby') && usable(b)) || null;
}
"""

# Text of the step after a successful reserve (photo upload, then payment). Only
# counts if NEW relative to the results page -- see the reserve block.
CONFIRM_STEPS = ["STEP 3 OF 4", "STEP 4 OF 4"]  # case-sensitive
CONFIRM_WORDS = ["photo", "payment", "upload"]  # case-insensitive


def ensure_on_test_center_search(context, page):
    page.goto("https://mysat.collegeboard.org/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(3)
    dismiss_cookie_banner(page)
    content, text = dbg(page, "mysat_initial")
    if looks_blocked(text):
        raise RuntimeError("SITE_BLOCKED_SUSPECTED")
    if len(text.strip()) < 20:
        # blank/unrendered page, not necessarily "not logged in" -- give it one
        # more chance before concluding a fresh login is needed. Misreading this
        # as logged-out previously triggered an unnecessary do_login() call on an
        # already-valid session, which then hit an unhandled edge case.
        log("DEBUG mysat_initial came back blank, retrying before assuming logged out")
        time.sleep(4)
        content, text = dbg(page, "mysat_initial_retry")

    if not looks_authenticated(content) and "Hello" not in text:
        do_login(page)
        page.goto("https://mysat.collegeboard.org/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        content, text = dbg(page, "mysat_after_login")
        # mysat.collegeboard.org sometimes shows its own simple "Sign In / Continue"
        # splash on top of an already-valid session rather than the dashboard --
        # a second click is needed to actually land on "Hello osim..."
        if "Sign In" in text and "Continue" in text and "Hello" not in text:
            log("DEBUG mysat splash screen shown after login, clicking its Continue")
            page.evaluate("""
                () => {
                    var b = Array.from(document.querySelectorAll('button')).find(x => x.innerText.trim() === 'Continue');
                    if (b) b.click();
                }
            """)
            time.sleep(4)
            content, text = dbg(page, "after_mysat_splash_continue")

    content, text = "", ""
    for entry_attempt in range(3):
        start_url = page.url
        for attempt in range(4):
            try:
                with context.expect_page(timeout=5000) as new_page_info:
                    page.evaluate("""
                        () => {
                            var btns = Array.from(document.querySelectorAll('button,a'));
                            var btn = btns.find(b => /register for the sat/i.test(b.innerText||''));
                            if (btn) btn.click();
                        }
                    """)
                page = new_page_info.value
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                log(f"DEBUG js-click opened new tab (entry {entry_attempt}, attempt {attempt}):", page.url)
                break
            except Exception as e:
                log(f"DEBUG js-click entry {entry_attempt} attempt {attempt} did not open new tab:", type(e).__name__)
            time.sleep(3)
            if page.url != start_url:
                log(f"DEBUG same-page navigation happened (entry {entry_attempt}, attempt {attempt}):", page.url)
                break
        time.sleep(3)
        content, text = dbg(page, "after_entry_click")
        if looks_blocked(text):
            raise RuntimeError("SITE_BLOCKED_SUSPECTED")

        if "Verify with your password" in text or ("Password" in text and "Forgot password" in text):
            log("DEBUG step-up password re-verification required")
            pw_input = page.wait_for_selector("input[type=password]", timeout=15000)
            pw_input.fill(CB_PASSWORD)
            page.click("input[type=submit]", force=True)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(5)
            content, text = dbg(page, "after_stepup_password")
        elif "Sign In" in text and "Email Address" in text:
            # the session fully expired (not just a step-up) -- a complete login
            # form is shown (email+password), distinct from the partial
            # password-only step-up handled above. Previously this silently fell
            # through, leaving `content` pointed at the raw login page, which made
            # the run quietly report "no seats" without ever reaching the real page.
            #
            # Do NOT navigate away to the generic login URL here (that's what
            # do_login() does) -- we're already mid-way through an OAuth redirect
            # that was specifically tied to the "Register for the SAT" click, with
            # its own client_id/redirect_uri/state. Abandoning it for a fresh
            # generic login broke the expected "choose verification method" step.
            # Instead, keep working on THIS page in place.
            log("DEBUG full re-login required (session fully expired) -- handling in place")
            email_el = page.query_selector("#input28")
            if email_el:
                email_el.fill(CB_EMAIL)
                page.click("input[type=submit]", force=True)
                try:
                    page.wait_for_selector("text=Select from the following options", timeout=10000)
                    page.click("a[aria-label='Select Password.']", force=True)
                except Exception:
                    log("DEBUG no method-picker shown, assuming password field appears directly")
                pw_input = page.wait_for_selector("input[type=password]", timeout=15000)
                pw_input.fill(CB_PASSWORD)
                page.click("input[type=submit]", force=True)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                time.sleep(5)
            content, text = dbg(page, "after_full_relogin")
            if len(text.strip()) < 20:
                # this exact transition (fresh landing on mysat.collegeboard.org
                # right after a full login) has repeatedly needed extra time + a
                # second render-nudge beyond what dbg() already retries internally
                log("DEBUG after_full_relogin came back empty, giving it more time")
                time.sleep(4)
                content, text = dbg(page, "after_full_relogin_retry")

        # the step-up challenge (or a flaky click) can bounce us back to the plain
        # dashboard instead of into the registration flow -- detect that and retry
        # the "Register for the SAT" click again instead of silently proceeding as
        # if we were on the right page (that previously caused a run to report
        # "no seats" while never actually reaching the search page at all).
        on_bare_dashboard = ("Hello" in text and "Register for the SAT" in content
                              and "STEP 1 OF 4" not in content and "STEP 2 OF 4" not in content)
        # likewise, still being stuck on an auth page of any kind must never be
        # mistaken for "we reached the search page and it's just empty"
        still_on_auth_page = ("Sign In" in text and "Email Address" in text) or "Verify with your password" in text
        # and an unrendered/blank page (the recurring visibility-timing issue on
        # this site) must never be treated as "we arrived, there's just nothing
        # here" -- that produced a false "no seats" with an empty RESULT SNIPPET
        looks_blank = len(text.strip()) < 20
        if not on_bare_dashboard and not still_on_auth_page and not looks_blank:
            break
        if looks_blank:
            log(f"DEBUG page came back blank after entry attempt {entry_attempt}, retrying")
        log(f"DEBUG still on dashboard/auth page after entry attempt {entry_attempt}, retrying")
    else:
        raise RuntimeError("COULD_NOT_ENTER_REGISTRATION_FLOW")

    if "Own Your Future with the Digital SAT" in content:
        page, _ = click_maybe_newtab(context, page, ["button:has-text('Get Started')"])
        time.sleep(3)
        content, text = dbg(page, "after_step1_get_started")

    if "On Your Way" in content and "test-center-country-city-input" not in content:
        page, _ = click_maybe_newtab(context, page, ["button:has-text('Get Started')"])
        time.sleep(3)
        content, text = dbg(page, "after_step2_get_started")

    prev_content = None
    for _ in range(14):
        # Use the rendered TEXT, not raw HTML presence or even element.is_visible()
        # -- this multi-step wizard keeps later steps mounted (often just
        # transform-offset, which is_visible() does not catch), so the
        # city-input id is already "visible" by that check while the "You're On
        # Your Way!" intro is still the only thing actually on screen. That made
        # the loop exit one step too early, before ever reaching the real form.
        if "Choose your test location" in text and "On Your Way" not in text:
            break
        if "School Information" in content and "Tell us about your school" in content:
            grade_sel = page.query_selector("#qc-id-personalinfo-list-grade")
            if grade_sel and grade_sel.input_value() != "6":
                grade_sel.select_option("6")
                log("DEBUG corrected grade to 11th Grade (value=6)")
                time.sleep(1)
            # target the school-info continue button specifically -- the generic
            # "[id*=button-continue]" selector can match a stale, already-enabled
            # button left over from an earlier sub-step (student/contact info),
            # which made still_disabled read False here even though this step's
            # own button was genuinely still disabled.
            btn = page.query_selector("#qc-id-personalinfo-schoolinfo-button-continue")
            still_disabled = btn.is_disabled() if btn else True
            log(f"DEBUG school-info check: btn_found={btn is not None} still_disabled={still_disabled}")
            # "No Results" text only appears AFTER a failed search attempt, not as
            # a pristine default -- the disabled continue button alone is the
            # correct signal that no school has been resolved/selected yet.
            if still_disabled:
                # the school genuinely isn't in College Board's database (confirmed:
                # Tajikistan only has 2 schools listed, neither is his). Rather than
                # fabricating a specific wrong school match, use the site's own
                # legitimate "my high school is not listed" path -- any real student
                # in this situation would select the same option.
                log("DEBUG school not in database -- using the site's own 'not listed' path")
                edit_btn = page.query_selector("#highschool-edit-button")
                if edit_btn:
                    edit_btn.click(force=True)
                    time.sleep(2)
                intl = page.query_selector("#qc-id-selectdatecenter-testlocation-radio-international")
                if intl:
                    intl.click(force=True)
                    time.sleep(2)
                country_sel = page.query_selector("#ai-search-country-dropdown")
                if country_sel:
                    page.select_option("#ai-search-country-dropdown", "TJ")
                    time.sleep(1)
                name_in = page.query_selector("#ai-search-name-input")
                city_in = page.query_selector("#ai-search-city-input")
                if name_in:
                    name_in.fill("Lyceum for Gifted Students")
                if city_in:
                    city_in.fill("Dushanbe")
                time.sleep(0.5)
                search_btn = page.query_selector("#ai-search-high-school-button")
                if search_btn:
                    search_btn.click(force=True)
                    time.sleep(4)
                toggle = page.query_selector("#ai-cant-find-school-toggle")
                if toggle:
                    toggle.click(force=True)
                    time.sleep(2)
                not_listed = page.query_selector("#ai-cant-find-school-radio-not-listed")
                if not_listed:
                    not_listed.check(force=True)
                    time.sleep(1)
                cant_find_submit = page.query_selector("#ai-cant-find-school-submit")
                if cant_find_submit:
                    cant_find_submit.click(force=True)
                    time.sleep(3)
                page.evaluate("""
                    () => {
                        var b = Array.from(document.querySelectorAll('button')).find(x => x.innerText.trim() === 'Yes, this is correct');
                        if (b) b.click();
                    }
                """)
                time.sleep(5)
                content, text = dbg(page, "after_school_not_listed")
                continue
            elif btn and not btn.is_disabled():
                btn.click(force=True)
                time.sleep(3)
                content, text = dbg(page, "after_school_info_fixed")
                continue
            else:
                # neither branch matched (e.g. the page hadn't finished rendering
                # the "No Results" placeholder or the continue button yet) --
                # re-check instead of falling through to the generic STEP 1
                # handler below, which could click the wrong (disabled) button
                log("DEBUG school-info state not yet resolved, waiting and retrying")
                time.sleep(2)
                content, text = dbg(page, "after_school_info_retry")
                continue

        if "Start by telling us a bit about yourself" in content or page.query_selector("#checkBox-sdqEthnicityChoices-5"):
            # optional demographic survey (race/ethnicity, parent education, military,
            # languages). We decline every question rather than guessing personal
            # identity details on his behalf -- every question here has an explicit
            # "I do not wish to respond" option, which is the honest, safe default.
            log("DEBUG on Student Profile demographic page -- declining all optional questions")
            for cb_id in ["checkBox-sdqEthnicityChoices-5", "checkBox-sdqRaceChoices-5", "checkBox-sdqMilitaryChoices-3"]:
                el = page.query_selector(f"#{cb_id}")
                if el:
                    el.check(force=True)
            for sel_id in ["parent1Education", "parent2Education"]:
                el = page.query_selector(f"#{sel_id}")
                if el:
                    page.select_option(f"#{sel_id}", "I do not wish to respond")
            for radio_id in ["firstLanguage03", "bestLanguage03"]:
                el = page.query_selector(f"#{radio_id}")
                if el:
                    el.check(force=True)
            time.sleep(1)
            clicked_continue = page.evaluate("""
                () => {
                    var btns = Array.from(document.querySelectorAll('button')).filter(
                        b => b.innerText.trim() === 'Continue' && !b.getAttribute('aria-describedby')
                    );
                    if (btns.length > 0) { btns[0].click(); return true; }
                    return false;
                }
            """)
            log("DEBUG student profile continue clicked:", clicked_continue)
            time.sleep(4)
            content, text = dbg(page, "after_student_profile")
            continue

        if content == prev_content:
            try:
                debug_info = page.evaluate("""
                    () => {
                        var allSelects = Array.from(document.querySelectorAll('select')).map(s => ({id:s.id, value:s.value, options: Array.from(s.options).map(o=>({v:o.value,t:o.text}))}));
                        // never dump password fields: getting stuck right after the
                        // step-up/full re-login fill would print CB_PASSWORD's value
                        var allInputs = Array.from(document.querySelectorAll('input')).filter(i=>i.type!=='hidden'&&i.type!=='checkbox'&&i.type!=='password').map(i => ({id:i.id, value:i.value, type:i.type}));
                        return {selects: allSelects, inputs: allInputs, bodyTextFull: document.body.innerText.slice(0, 1500)};
                    }
                """)
            except Exception as e:
                debug_info = {"eval_error": str(e)}
            log("DEBUG stuck - content did not change. debug:", debug_info)
            break
        prev_content = content
        if "STEP 1 OF 4" in content and "test-center-country-city-input" not in content and "Own Your Future with the Digital SAT" not in content:
            ids = page.evaluate("""
                () => Array.from(document.querySelectorAll('[id*=continue], [id*=Continue]')).map(e => ({id: e.id, disabled: !!e.disabled}))
            """)
            log("DEBUG personalInformation continue-ish ids:", ids)
            clicked_id = None
            for item in ids:
                cid = item["id"]
                low = cid.lower()
                # exclude disabled buttons (the real current-substep button may be
                # disabled pending required fields) and stale ids from steps this
                # wizard keeps mounted after they're done -- schoolinfo has its own
                # dedicated handler earlier in the loop, so by the time we're here
                # it's always a leftover, same bug class already fixed for the
                # school-info and reserve-confirmation buttons.
                if item["disabled"] or "session" in low or "timeout" in low or "schoolinfo" in low:
                    continue
                clicked_id = cid
                break
            if clicked_id:
                page.click(f"#{clicked_id}", force=True)
            else:
                page.evaluate("""
                    () => {
                        var btns = Array.from(document.querySelectorAll('button'));
                        var btn = btns.find(b => b.innerText.trim() === 'Continue' && !b.getAttribute('aria-describedby'));
                        if (btn) btn.click();
                    }
                """)
            time.sleep(3)
            content, text = dbg(page, "after_personal_info")
            continue
        if "Own Your Future with the Digital SAT" in content or "On Your Way" in content:
            # the pre-loop handlers for these same two intro screens use
            # click_maybe_newtab because "Get Started" can open a new tab --
            # this in-loop catch-all used plain click_first instead, which
            # would silently keep operating on the old, now-abandoned tab if
            # that ever happened here. Match the established pattern.
            page, _ = click_maybe_newtab(context, page, ["button:has-text('Get Started')"])
            time.sleep(3)
            content, text = dbg(page, "after_get_started")
            continue
        if "I have read and I accept" in content or "terms-acceptance-checkbox" in content:
            accept_terms_if_present(page)
            time.sleep(2)
            content, text = dbg(page, "after_terms")
            continue
        if TEST_DATE_BUTTON_ID in content:
            date_btn = page.query_selector(f"#{TEST_DATE_BUTTON_ID}")
            if date_btn:
                date_btn.click(force=True)
                time.sleep(1)
            cont = page.query_selector("#testdate-continue-button")
            if cont:
                cont.click(force=True)
                time.sleep(3)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            content, text = dbg(page, "after_date_select")
            continue
        break

    if "test-center-country-city-input" in content:
        city = page.query_selector("#test-center-country-city-input")
        if city:
            city.fill("Dushanbe")
        country = page.query_selector("#test-center-country-select")
        if country:
            country.select_option("TJ")
        find_btn = page.query_selector("#test-center-country-find-button-enabled")
        log("DEBUG found find btn=", bool(find_btn))
        if find_btn:
            find_btn.click(force=True)
            time.sleep(4)
            # the fixed 4s alone raced a slow results render: the page was read
            # before any result row existed, which is indistinguishable from
            # "no seats" -- give a slow response up to 15s more to show the row
            try:
                page.wait_for_function(
                    "() => /No seats available|Qsi International/i.test(document.body.innerText)",
                    timeout=15000, polling=500)
            except Exception:
                log("WARNING: no result row rendered within ~19s of the Find click")
            dbg(page, "after_find_click")

    return page


# Decides seat state from the result card itself. Starts from the deepest
# rendered elements that name the center and walks UP to the first ancestor that
# holds either "No seats available" or an enabled Select/Reserve control -- i.e.
# the center's own card -- so neither another center's row, a hidden wizard step,
# nor a <script>/autocomplete mention of "Dushanbe" can decide it.
# "No seats available" is checked before the button at each level: an ancestor
# wide enough to hold another center's Select button also holds this center's
# "No seats available", and must not be read as a seat.
# The first signal found must also still be inside ONE card: when this center's
# own card shows neither (e.g. its seat is a radio/"Choose" control), the walk
# used to climb into the result list and take the NEXT center's "No seats
# available" as this center's -- a silent false "no seats" -- or another center's
# Select as a seat to auto-reserve. Every live card holds exactly one date line
# (and a Google Maps link), so an ancestor with two spans several cards.
# datePattern is TEST_DATE_RE's pattern, passed in so the two cannot drift.
SEAT_PROBE_JS = r"""
(datePattern) => {
  const QSI_RE = /Qsi International/i, CITY_RE = /Dushanbe/i;
  const NO_SEATS_RE = /No seats available/i;
  const BTN_RE = /^\s*(select|reserve)\b/i;
  const DATE_RE = new RegExp(datePattern, 'gi');
  const MAPS_RE = /google maps|google\.[a-z.]+\/maps|maps\.google\.|maps\.app\.goo\.gl|goo\.gl\/maps/i;
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'OPTION', 'SELECT', 'HEAD', 'TITLE']);
  const rendered = el => el.getClientRects().length > 0;
  const cardMarkers = el => ({
    maps: Array.from(el.querySelectorAll('a')).filter(a => rendered(a)
            && MAPS_RE.test((a.getAttribute('href') || '') + ' ' + (a.innerText || ''))).length,
    dates: ((el.innerText || '').match(DATE_RE) || []).length,
  });
  const labelOf = el => (((el.innerText || el.value || '') + '').trim()
                         || (el.getAttribute('aria-label') || '').trim());
  const buttons = Array.from(document.querySelectorAll(
      'button, a, [role=button], input[type=button], input[type=submit]'))
    .filter(b => rendered(b) && BTN_RE.test(labelOf(b))
                 && !b.disabled && b.getAttribute('aria-disabled') !== 'true');
  const deepestMatching = re => {
    const hits = new Set(Array.from(document.body.querySelectorAll('*')).filter(e =>
      !SKIP.has(e.tagName) && re.test(e.textContent || '') && rendered(e) && re.test(e.innerText || '')));
    return Array.from(hits).filter(e => !Array.from(e.children).some(c => hits.has(c)));
  };
  // the exact school name is authoritative when present; the city alone is only
  // a fallback in case the name's wording changes
  let starts = deepestMatching(QSI_RE);
  if (starts.length === 0) starts = deepestMatching(CITY_RE);
  let verdict = starts.length ? 'unknown' : 'absent', btn = null, card = null;
  let markers = null, foreign = null;
  for (const start of starts) {
    let passedOwnCard = false;
    for (let el = start; el && el !== document.body; el = el.parentElement) {
      const t = el.innerText || '';
      if (t.length > 3000) break;
      const m = cardMarkers(el);
      // the date line is the card marker (the live card's full text is in the
      // run log: exactly one); Maps links only stand in if cards drop the date
      const cards = m.dates || m.maps;
      const noSeats = NO_SEATS_RE.test(t);
      const b = noSeats ? null : buttons.find(x => el.contains(x));
      if (!noSeats && !b) {
        passedOwnCard = passedOwnCard || cards === 1;
        continue;
      }
      if (cards > 1) {
        // the first signal is in an ancestor spanning several cards. Climbed
        // there from inside a card: that card shows neither, so it is
        // unresolved. From outside any card (a heading above the list): a
        // stray mention, which decides nothing.
        if (passedOwnCard) foreign = foreign || {markers: m, preview: t.slice(0, 120).replace(/\s+/g, ' ')};
        break;
      }
      if (noSeats) {
        if (!card) { card = el; markers = m; }
        if (verdict === 'unknown') verdict = 'no_seats';
        break;
      }
      verdict = 'button'; btn = b; card = el; markers = m; break;
    }
    if (verdict === 'button') break;
  }
  // a center card that could not be resolved outranks another card's "No
  // seats available" -- it is the one card that may be showing a seat
  if (foreign && verdict === 'no_seats') { verdict = 'unknown'; card = null; markers = null; }
  const cardText = card ? (card.innerText || '') : '';
  return {
    btn: btn,
    info: {
      verdict: verdict,
      starts: starts.length,
      page_select_buttons: buttons.length,
      card_tag: card ? card.tagName + (card.id ? '#' + card.id : '') : null,
      card_len: cardText.length,
      card_markers: markers,
      card_preview: cardText.slice(0, 200).replace(/\s+/g, ' '),
      btn_label: btn ? labelOf(btn).slice(0, 80) : null,
      foreign_signal: foreign,
    },
  };
}
"""


def probe_seat_state(page):
    handle = page.evaluate_handle(SEAT_PROBE_JS, TEST_DATE_RE.pattern)
    info = handle.get_property("info").json_value()
    btn = handle.get_property("btn").as_element()
    return info, btn


def note_blind_run(reason):
    # this run never actually saw the Dushanbe/Dec 5 result row. It used to fall
    # through to "no seats this run" -- a monitor that silently stopped seeing
    # results looked exactly like a healthy one finding nothing, indefinitely.
    log("WARNING: BLIND RUN -- seats were NOT actually checked this run:", reason)
    n = int(read_ts(BLIND_COUNT_PATH) or 0) + 1
    with open(BLIND_COUNT_PATH, "w") as f:
        f.write(str(n))
    # one miss can be a transient render hiccup; two in a row is a real outage
    if n >= 2:
        tg_throttled(BLIND_ALERT_PATH,
                     f"🟠 SAT-мониторинг: {n} проверки подряд НЕ дошли до результатов поиска ({reason[:150]}). "
                     "Места сейчас фактически НЕ проверяются — зайди проверь сам: "
                     "https://satreg.collegeboard.org/register/testCenterSearch", 3600)


def clear_blind_state():
    if os.path.exists(BLIND_ALERT_PATH):
        tg("✅ SAT-мониторинг снова видит результаты поиска по Душанбе — проверки идут нормально.")
    remove_flag(BLIND_ALERT_PATH)
    remove_flag(BLIND_COUNT_PATH)


def main():
    with sync_playwright() as p:
        # --disable-blink-features=AutomationControlled hides the otherwise
        # very easy navigator.webdriver=true bot signal that Chromium sets by
        # default under Playwright/Puppeteer -- cheap, standard mitigation
        # against this specific automation fingerprint.
        #
        # channel="chromium" runs the full Chromium build in new headless mode
        # instead of the default chromium-headless-shell (both are already
        # installed by `playwright install chromium`). The headless shell
        # brands itself "HeadlessChrome" in the sec-ch-ua header it sends with
        # EVERY request -- a user_agent override does not touch client hints,
        # so each request said both "Chrome/130" (UA) and "HeadlessChrome;v=153"
        # (sec-ch-ua). Full Chromium sends a plain "Chromium" brand.
        browser = p.chromium.launch(channel="chromium", headless=True,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx_kwargs = {
            "user_agent": REALISTIC_USER_AGENT.format(major=browser.version.split(".")[0]),
            "viewport": {"width": 1366, "height": 768},
            "locale": "en-US",
        }
        if os.path.exists(STATE_PATH) and os.path.getsize(STATE_PATH) > 2:
            ctx_kwargs["storage_state"] = STATE_PATH
        context = browser.new_context(**ctx_kwargs)
        context.add_init_script("""
            Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
            Object.defineProperty(document, 'hidden', {get: () => false});
            window.addEventListener('load', () => {
                document.dispatchEvent(new Event('visibilitychange'));
                window.dispatchEvent(new Event('focus'));
            });
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
        fingerprint_logged = []

        def log_fingerprint_once(req):
            # what the site actually receives -- so a UA/client-hint mismatch
            # shows up in the run log instead of having to be guessed at
            if not fingerprint_logged and "collegeboard.org" in req.url:
                fingerprint_logged.append(True)
                log("DEBUG fingerprint headers:", {k: v for k, v in req.headers.items()
                                                    if k == "user-agent" or k.startswith("sec-ch-ua")})
        context.on("request", log_fingerprint_once)
        page = context.new_page()

        try:
            page = ensure_on_test_center_search(context, page)
            # a successful run means we're past any block -- clear the
            # one-time block-alert flag so a FUTURE block episode (not just
            # the current one) gets its own fresh alert instead of being
            # silenced forever by a flag left over from a past, now-resolved
            # block.
            remove_flag(BLOCK_FLAG_PATH)
            # same for the error alert throttle: the next failure episode should
            # alert immediately, not wait out an old episode's hour
            remove_flag(ERROR_ALERT_PATH)
        except Exception as e:
            log("FATAL during flow:", e)
            expected_pause = str(e) == "NEEDS_SCHOOL_NAME_INPUT"
            site_blocked = str(e) == "SITE_BLOCKED_SUSPECTED"
            if site_blocked:
                # a bot-protection challenge page was detected -- not on every
                # 5-min run while the block persists (spam, not actionable), but
                # not just ONCE either: a block lasting days used to mean one
                # message and then silence while seats went unchecked. Reminder
                # every 6h; tg_throttled also only marks it sent if it went out.
                # This run still exits 0 so GitHub doesn't flag it as a workflow
                # failure; the next scheduled run tries again on its own.
                tg_throttled(BLOCK_FLAG_PATH,
                             "🔴 Сайт College Board похоже временно блокирует/проверяет автоматические запросы. "
                             "Мониторинг продолжит пробовать сам, но пока это так — места НЕ проверяются; "
                             "если это не проходит долго — возможно, нужно подождать или проверить вручную.",
                             6 * 3600)
            elif not expected_pause:
                # was sent on EVERY failing run (every 5 min while e.g. login is
                # broken) -- enough noise to get the bot muted, which would also
                # mute the seat alert. Once per episode, then hourly reminders.
                tg_throttled(ERROR_ALERT_PATH, f"🔴 SAT-мониторинг (облако): ошибка при проверке — {e}", 3600)
            try:
                context.storage_state(path=STATE_PATH)
                browser.close()
            except Exception as close_err:
                log("could not save state/close browser after failure:", close_err)
            # expected pause (waiting on the owner to fill the school field) must NOT
            # be reported as a workflow failure, or GitHub emails on every 5-min run
            sys.exit(0 if (expected_pause or site_blocked) else 1)

        exit_code = 0
        seat_stage = False
        seat_alert_ok = result_alert_ok = False
        try:
            body_text = page.inner_text("body")
            idx = body_text.find("Find a Test Center")
            snippet = body_text[idx: idx + 600] if idx >= 0 else body_text[:600]
            log("RESULT SNIPPET:", snippet.replace("\n", " ")[:300])

            # check the FULL page text, not the 600-char snippet above (that's only
            # for the log line) -- if the site ever renders extra filter/UI text
            # before the result row, "Dushanbe"/"Qsi International" or "No seats
            # available" could fall outside that narrow window, which would make
            # has_center silently read False and cause a real seat to be missed
            # entirely (seat_available requires has_center AND a real button).
            has_center = ("Dushanbe" in body_text) or ("Qsi International" in body_text)
            date_ok = bool(TEST_DATE_RE.search(body_text))

            # the card-scoped probe decides (its verdict was checked in shadow mode
            # against the real page first: 'no_seats' on the QSI card itself). It
            # replaced "first element in document order whose text mentions
            # Dushanbe and is < 2000 chars" + has-text('Select'): on any shorter page
            # that element is a top-level container, so an unrelated control like
            # a "Select Date and Test Center" stepper link counted as a seat
            # (reproduced offline) -- a false seat alert plus a reserve click on
            # the wrong element.
            probe_info, sel_btn = probe_seat_state(page)
            verdict = probe_info["verdict"]
            log("DEBUG seat probe:", probe_info)
            # dry run of what the (never live-tested) reserve path would see when it
            # looks for its Continue button -- reveals stale ids from earlier steps
            try:
                cont_candidates = page.evaluate("""
                    () => Array.from(document.querySelectorAll('[id*=continue], [id*=Continue]')).map(e => ({
                        id: e.id, disabled: !!e.disabled, rendered: e.getClientRects().length > 0,
                        text: (e.innerText || '').trim().slice(0, 30)}))
                """)
                log("DEBUG continue-ish ids on results page:", cont_candidates)
            except Exception as ce:
                log("DEBUG continue-ish ids lookup failed:", ce)

            if not os.path.exists(FLAG_PATH):
                tg("🟡 SAT-мониторинг в облаке (GitHub Actions) запущен и работает. Проверка каждые ~5 минут, независимо от твоего Mac.")
                with open(FLAG_PATH, "w") as f:
                    f.write("started")

            # the PRIMARY signal is a real, enabled Select/Reserve control in the
            # center's own card (the probe already excludes disabled/aria-disabled
            # ones), not the absence of a specific phrase -- that phrase could change
            # wording and break this check forever.
            seat_available = verdict == "button"
            results_seen = date_ok and verdict != "absent"

            if results_seen:
                clear_blind_state()
            else:
                note_blind_run(f"verdict={verdict} has_center={has_center} date_ok={date_ok} url={page.url}")
                exit_code = 1

            if seat_available and not date_ok:
                # a Select control next to Dushanbe, but the page doesn't show Dec 5 --
                # auto-reserving could book the wrong date, and staying quiet could
                # waste a real seat: hand it straight to the owner
                log("WARNING: seat-like button found but test date not verified as Dec 5 -- NOT auto-reserving")
                tg_throttled(UNKNOWN_ALERT_PATH,
                             "🟢❓ Похоже, в Душанбе есть место, но я не смог подтвердить, что это 5 декабря — "
                             "сам НЕ бронирую. Проверь СЕЙЧАС вручную: https://satreg.collegeboard.org/register/testCenterSearch",
                             1800)
                seat_available = False
            elif results_seen and verdict == "unknown":
                # the Dushanbe card is there but no longer says "No seats available",
                # yet no Select/Reserve control was recognised -- e.g. the seat
                # control renders as a radio or uses other wording. This used to be
                # log-only, i.e. the exact moment a seat most likely appeared was
                # the one case that never reached the owner.
                log("WARNING: unrecognised result-row state (neither 'no seats' text nor a button found) -- site text may have changed:", snippet[:300])
                tg_throttled(UNKNOWN_ALERT_PATH,
                             "🟢❓ В строке Душанбе (5 декабря) больше НЕТ надписи «No seats available», но кнопку "
                             "брони я не распознал — ВОЗМОЖНО, МЕСТО ЕСТЬ. Проверь СЕЙЧАС вручную: "
                             "https://satreg.collegeboard.org/register/testCenterSearch", 1800)
            elif results_seen and verdict == "no_seats":
                # back to the plain, recognised "no seats" state: re-arm the alert
                remove_flag(UNKNOWN_ALERT_PATH)

            if seat_available:
                seat_stage = True
                log("SEAT AVAILABLE - attempting reserve")
                # sent alongside the reserve clicks, not before them: tg() retries
                # for up to ~50s when Telegram is slow, and a released seat can be
                # gone by then. Non-daemon, so it still completes if this block throws.
                seat_alert = {}
                seat_alert_thread = threading.Thread(target=lambda: seat_alert.update(ok=tg(
                    "🟢🟢🟢 МЕСТО В ДУШАНБЕ НА 5 ДЕКАБРЯ ПОЯВИЛОСЬ! Пытаюсь забронировать прямо сейчас — "
                    "жди второе сообщение с реальным статусом.")))
                seat_alert_thread.start()

                url_before = page.url
                text_before = page.inner_text("body")

                # sel_btn is guaranteed non-None here: seat_available already required it.
                # This site has repeatedly opened new tabs for key navigation clicks
                # ("Register for the SAT", "Get Started") -- the exact same pattern
                # is very plausible here too, and this reserve click has never been
                # exercised against a real seat to rule it out. If it DID open a new
                # tab and this kept operating on the old, abandoned page, every check
                # below (continue button, text_after) would silently read the wrong
                # page at the single highest-stakes moment in the whole script.
                page = click_el_maybe_newtab(context, page, sel_btn, "reserve Select")
                # this site requires a page-level Continue after Select on every other
                # step (date, school info, terms) -- almost certainly true here too.
                # Live, the results page's only continue-ish element is
                # "test-location-continue-disabled" (disabled until a center is
                # picked), so poll for an enabled one instead of a blind 3s sleep
                # that a slow re-render would simply miss.
                cont_el = None
                for _ in range(10):
                    try:
                        cont_el = page.evaluate_handle(FIND_CONTINUE_JS).as_element()
                    except Exception as pe:
                        # e.g. "Execution context was destroyed" while the Select
                        # click is still navigating: one failed look used to abort
                        # the whole reserve -- the next poll reads the new page
                        log("DEBUG reserve Continue lookup failed, retrying:", repr(pe)[:150])
                    if cont_el:
                        break
                    time.sleep(1)
                if cont_el:
                    try:
                        log("DEBUG reserve Continue:", cont_el.get_attribute("id"), (cont_el.inner_text() or "")[:30])
                    except Exception:
                        pass
                    page = click_el_maybe_newtab(context, page, cont_el, "reserve Continue")
                    try:
                        # wait only for step text the results page did NOT already
                        # show: a pre-existing "photo ID" note made this return at
                        # once, so a step 3 slowed by a seat-release rush was read
                        # too early and reported as "not sure it worked"
                        page.wait_for_function(
                            """([steps, words]) => {
                                const t = (document.body && document.body.innerText) || '';
                                const low = t.toLowerCase();
                                return steps.some(k => t.includes(k)) || words.some(k => low.includes(k));
                            }""",
                            arg=[[k for k in CONFIRM_STEPS if k not in text_before],
                                 [k for k in CONFIRM_WORDS if k not in text_before.lower()]],
                            timeout=30000, polling=500)
                    except Exception:
                        log("DEBUG no new photo/payment step text within 30s of Continue")
                else:
                    log("WARNING: no enabled Continue appeared within 10s of the Select click")
                page.bring_to_front()
                time.sleep(1)
                url_after = page.url
                text_after = page.inner_text("body")
                # require a SPECIFIC positive signal (the next wizard step: photo /
                # payment), not just "something changed" -- a bare URL change could
                # just as easily mean the session timed out or an error page loaded.
                # And it must be NEW relative to the results page: a word like
                # "photo" already present there (e.g. a "bring photo ID" note) would
                # otherwise "confirm" a reservation even if both clicks did nothing.
                def newly(k, case_insensitive=False):
                    if case_insensitive:
                        return k.lower() in text_after.lower() and k.lower() not in text_before.lower()
                    return k in text_after and k not in text_before
                looks_confirmed = any(newly(k) for k in CONFIRM_STEPS) or \
                    any(newly(k, True) for k in CONFIRM_WORDS)
                looks_like_error_or_login = ("Sign In" in text_after and "Email Address" in text_after) or newly("error", True)
                reserved_ok = looks_confirmed and not looks_like_error_or_login
                detail = f"url_before={url_before} url_after={url_after} text_after_preview={text_after[:300]}"
                log("RESERVE ATTEMPT RESULT:", detail)

                if reserved_ok:
                    result_alert_ok = tg("✅ Бронь похожа на успешную (страница сдвинулась дальше). ИДИ К КОМПЬЮТЕРУ СЕЙЧАС — "
                                         "20 минут на фото и оплату $68, иначе бронь сгорит: https://satreg.collegeboard.org/register/testCenterSearch")
                else:
                    result_alert_ok = tg(f"⚠️ НЕ УВЕРЕН, что бронь прошла — проверить не смог. Заходи СЕЙЧАС САМ и бронируй вручную: "
                                         f"https://satreg.collegeboard.org/register/testCenterSearch (детали: {detail[:200]})")
                seat_alert_thread.join(60)
                seat_alert_ok = seat_alert.get("ok", False)
                if not (seat_alert_ok or result_alert_ok):
                    # Telegram unreachable at the one moment it matters: fail the run
                    # so GitHub's failed-workflow email is a last-resort channel
                    log("SEAT ALERT NOT DELIVERED -- failing the run so GitHub emails the owner")
                    exit_code = 1
            elif results_seen and verdict == "no_seats":
                log("no seats this run")
            else:
                log("seats NOT confirmed absent this run (see WARNING above)")
        except Exception as e:
            log("EXCEPTION during seat-detection/reservation block:", repr(e))
            exit_code = 1
            if seat_stage:
                # this block runs at the single most important moment (a real seat found) --
                # it must NEVER crash silently. If anything throws here, tell the owner
                # immediately to go check by hand rather than losing the moment to a traceback.
                if not tg(f"🔴🔴 ОШИБКА во время попытки бронирования — проверить автоматически не смог. "
                          f"СРОЧНО зайди сам: https://satreg.collegeboard.org/register/testCenterSearch (ошибка: {str(e)[:200]})"):
                    log("SEAT ALERT NOT DELIVERED")
            else:
                # failed before any seat was seen: we simply didn't get to look
                note_blind_run(f"exception reading results: {str(e)[:120]}")

        try:
            context.storage_state(path=STATE_PATH)
            browser.close()
        except Exception as close_err:
            log("could not save state/close browser:", close_err)
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
