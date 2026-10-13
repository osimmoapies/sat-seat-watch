import os
import sys
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

TEST_DATE_BUTTON_ID = "qc-id-selectdatecenter-testdate-button-DEC-05"


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
            return
        except Exception as e:
            print(f"TG SEND FAILED (attempt {attempt + 1}/3):", e)
            if attempt < 2:
                time.sleep(3)


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


def ensure_on_test_center_search(context, page):
    page.goto("https://mysat.collegeboard.org/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(3)
    dismiss_cookie_banner(page)
    content, text = dbg(page, "mysat_initial")
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
                        var allInputs = Array.from(document.querySelectorAll('input')).filter(i=>i.type!=='hidden'&&i.type!=='checkbox').map(i => ({id:i.id, value:i.value, type:i.type}));
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
            dbg(page, "after_find_click")

    return page


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = {}
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
        """)
        page = context.new_page()

        try:
            page = ensure_on_test_center_search(context, page)
        except Exception as e:
            log("FATAL during flow:", e)
            expected_pause = str(e) == "NEEDS_SCHOOL_NAME_INPUT"
            if not expected_pause:
                tg(f"🔴 SAT-мониторинг (облако): ошибка при проверке — {e}")
            context.storage_state(path=STATE_PATH)
            browser.close()
            # expected pause (waiting on the owner to fill the school field) must NOT
            # be reported as a workflow failure, or GitHub emails on every 5-min run
            sys.exit(0 if expected_pause else 1)

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
            no_seats_text = "No seats available" in body_text

            if not os.path.exists(FLAG_PATH):
                tg("🟡 SAT-мониторинг в облаке (GitHub Actions) запущен и работает. Проверка каждые ~5 минут, независимо от твоего Mac.")
                with open(FLAG_PATH, "w") as f:
                    f.write("started")

            # scope the click to the actual result row, not the whole page -- avoids
            # matching an unrelated "Select"/"Reserve" text elsewhere. Match on
            # Dushanbe too (not just the exact school name), consistent with the
            # has_center check above, in case the row text ever varies slightly.
            row_handle = page.evaluate_handle("""
                () => {
                    var all = Array.from(document.querySelectorAll('*'));
                    return all.find(e => /Qsi International|Dushanbe/i.test(e.innerText||'') && e.innerText.length < 2000) || null;
                }
            """)
            row_el = row_handle.as_element()
            sel_btn = None
            if row_el:
                sel_btn = row_el.query_selector("button:has-text('Select'), a:has-text('Select'), button:has-text('Reserve')")
            if sel_btn and sel_btn.is_disabled():
                # a disabled button present in the row is not an actionable seat --
                # without this check a greyed-out button would still flip
                # seat_available to True below and fire a false "место появилось"
                # alert for something that can't actually be clicked through.
                sel_btn = None

            # the PRIMARY signal is a real clickable button in the row, not the absence of a
            # specific phrase -- that phrase could change wording and silently break this
            # check forever, causing a false "seat available" alert on every single run.
            seat_available = has_center and sel_btn is not None

            if has_center and not seat_available and not no_seats_text:
                # text changed in a way we don't recognise: no "No seats available" phrase,
                # but also no button found -- log loudly so this gets noticed, but do NOT
                # alert the owner on every run for something we can't act on anyway.
                log("WARNING: unrecognised result-row state (neither 'no seats' text nor a button found) -- site text may have changed:", snippet[:300])

            if seat_available:
                log("SEAT AVAILABLE - attempting reserve")
                tg("🟢🟢🟢 МЕСТО В ДУШАНБЕ НА 5 ДЕКАБРЯ ПОЯВИЛОСЬ! Пытаюсь забронировать прямо сейчас — жди второе сообщение с реальным статусом.")

                url_before = page.url
                text_before = page.inner_text("body")

                # sel_btn is guaranteed non-None here: seat_available already required it.
                # This site has repeatedly opened new tabs for key navigation clicks
                # ("Register for the SAT", "Get Started") -- the exact same pattern
                # is very plausible here too, and this reserve click has never been
                # exercised against a real seat to rule it out. If it DID open a new
                # tab and this kept operating on the old, abandoned page, every check
                # below (cont_id, text_after) would silently read the wrong page at
                # the single highest-stakes moment in the whole script.
                try:
                    with context.expect_page(timeout=4000) as new_page_info:
                        sel_btn.click(force=True)
                    page = new_page_info.value
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    log("DEBUG reserve Select click opened a new tab:", page.url)
                except Exception:
                    pass
                time.sleep(3)
                # this site requires a page-level Continue after Select on every other
                # step (date, school info, terms) -- almost certainly true here too.
                # Find a continue-ish id, excluding the session/timeout modal's AND any
                # stale Step-1 ("personalinfo") button this wizard keeps mounted from
                # earlier steps -- the exact kind of stale-button mismatch already
                # found and fixed once for the school-info step, now guarded against
                # here too since this is the single highest-stakes click in the script.
                cont_id = page.evaluate("""
                    () => {
                        var els = Array.from(document.querySelectorAll('[id*=continue], [id*=Continue]'));
                        var el = els.find(e => e.id && !/session|timeout|personalinfo/i.test(e.id) && !e.disabled);
                        return el ? el.id : null;
                    }
                """)
                if cont_id:
                    btn = page.query_selector(f"#{cont_id}")
                    if btn and not btn.is_disabled():
                        btn.click(force=True)
                        time.sleep(4)
                page.bring_to_front()
                time.sleep(1)
                url_after = page.url
                text_after = page.inner_text("body")
                # require a SPECIFIC positive signal (photo/payment step), not just
                # "something changed" -- a bare URL change could just as easily mean
                # the session timed out or an error page loaded, which would have
                # falsely confirmed a reservation that never actually happened at
                # the single moment this matters most.
                looks_confirmed = any(k in text_after for k in ["photo", "Photo", "payment", "Payment", "upload", "Upload"])
                looks_like_error_or_login = ("Sign In" in text_after and "Email Address" in text_after) or "error" in text_after.lower()
                reserved_ok = looks_confirmed and not looks_like_error_or_login
                detail = f"url_before={url_before} url_after={url_after} text_after_preview={text_after[:300]}"
                log("RESERVE ATTEMPT RESULT:", detail)

                if reserved_ok:
                    tg("✅ Бронь похожа на успешную (страница сдвинулась дальше). ИДИ К КОМПЬЮТЕРУ СЕЙЧАС — "
                       "20 минут на фото и оплату $68, иначе бронь сгорит: https://satreg.collegeboard.org/register/testCenterSearch")
                else:
                    tg(f"⚠️ НЕ УВЕРЕН, что бронь прошла — проверить не смог. Заходи СЕЙЧАС САМ и бронируй вручную: "
                       f"https://satreg.collegeboard.org/register/testCenterSearch (детали: {detail[:200]})")
            else:
                log("no seats this run")
        except Exception as e:
            # this block runs at the single most important moment (a real seat found) --
            # it must NEVER crash silently. If anything throws here, tell the owner
            # immediately to go check by hand rather than losing the moment to a traceback.
            log("EXCEPTION during seat-detection/reservation block:", repr(e))
            tg(f"🔴🔴 ОШИБКА во время попытки бронирования — проверить автоматически не смог. "
               f"СРОЧНО зайди сам: https://satreg.collegeboard.org/register/testCenterSearch (ошибка: {str(e)[:200]})")

        context.storage_state(path=STATE_PATH)
        browser.close()


if __name__ == "__main__":
    main()
