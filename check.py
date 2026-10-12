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
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text}).encode()
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
    except Exception as e:
        print("TG SEND FAILED:", e)


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
    page.wait_for_selector("text=Select from the following options", timeout=20000)
    page.click("a[aria-label='Select Password.']", force=True)
    pw_input = page.wait_for_selector("input[type=password]", timeout=20000)
    pw_input.fill(CB_PASSWORD)
    page.click("input[type=submit]", force=True)
    page.wait_for_load_state("networkidle", timeout=30000)
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

    if not looks_authenticated(content) and "Hello" not in text:
        do_login(page)
        page.goto("https://mysat.collegeboard.org/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        content, text = dbg(page, "mysat_after_login")

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
            log(f"DEBUG js-click opened new tab (attempt {attempt}):", page.url)
            break
        except Exception as e:
            log(f"DEBUG js-click attempt {attempt} did not open new tab:", type(e).__name__)
        time.sleep(3)
        if page.url != start_url:
            log(f"DEBUG same-page navigation happened (attempt {attempt}):", page.url)
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

    if "Own Your Future with the Digital SAT" in content:
        page, _ = click_maybe_newtab(context, page, ["button:has-text('Get Started')"])
        time.sleep(3)
        content, text = dbg(page, "after_step1_get_started")

    if "You're On Your Way!" in content and "test-center-country-city-input" not in content:
        page, _ = click_maybe_newtab(context, page, ["button:has-text('Get Started')"])
        time.sleep(3)
        content, text = dbg(page, "after_step2_get_started")

    prev_content = None
    for _ in range(14):
        if "test-center-country-city-input" in content:
            break
        if "School Information" in content and "Tell us about your school" in content:
            grade_sel = page.query_selector("#qc-id-personalinfo-list-grade")
            if grade_sel and grade_sel.input_value() != "6":
                grade_sel.select_option("6")
                log("DEBUG corrected grade to 11th Grade (value=6)")
                time.sleep(1)
            no_results = "No Results" in page.inner_text("body")
            btn = page.query_selector("[id*=button-continue]")
            still_disabled = btn.is_disabled() if btn else True
            if no_results and still_disabled:
                log("BLOCKED: school name is empty and required -- cannot safely guess a formal school name")
                asked_flag = FLAG_PATH + ".school_asked"
                if not os.path.exists(asked_flag):
                    tg("🟡 Нужна твоя помощь (разово): на шаге School Information поле 'Name of your high school' пустое и обязательное. "
                       "Зайди сам один раз на https://satreg.collegeboard.org/register/personalInformation , впиши название школы, "
                       "выбери её из списка и нажми Continue. После этого мониторинг дальше пойдёт automatически, больше писать не буду.")
                    with open(asked_flag, "w") as f:
                        f.write("asked")
                raise RuntimeError("NEEDS_SCHOOL_NAME_INPUT")
            if btn and not btn.is_disabled():
                btn.click(force=True)
                time.sleep(3)
                content, text = dbg(page, "after_school_info_fixed")
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
                () => Array.from(document.querySelectorAll('[id*=continue], [id*=Continue]')).map(e => e.id)
            """)
            log("DEBUG personalInformation continue-ish ids:", ids)
            clicked_id = None
            for cid in ids:
                if "session" not in cid.lower() and "timeout" not in cid.lower():
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
        if "Own Your Future with the Digital SAT" in content or "You're On Your Way!" in content:
            click_first(page, ["button:has-text('Get Started')"])
            time.sleep(3)
            content, text = dbg(page, "after_get_started")
            continue
        if "I have read and I accept" in content or "terms-acceptance-checkbox" in content:
            accept_terms_if_present(page)
            time.sleep(2)
            content, text = dbg(page, "after_terms")
            continue
        if f"qc-id-selectdatecenter-testdate-button-DEC-05" in content:
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
            if str(e) != "NEEDS_SCHOOL_NAME_INPUT":
                tg(f"🔴 SAT-мониторинг (облако): ошибка при проверке — {e}")
            context.storage_state(path=STATE_PATH)
            browser.close()
            sys.exit(1)

        body_text = page.inner_text("body")
        idx = body_text.find("Find a Test Center")
        snippet = body_text[idx: idx + 600] if idx >= 0 else body_text[:600]
        log("RESULT SNIPPET:", snippet.replace("\n", " ")[:300])

        has_center = ("Dushanbe" in snippet) or ("Qsi International" in snippet)
        no_seats = "No seats available" in snippet

        if not os.path.exists(FLAG_PATH):
            tg("🟡 SAT-мониторинг в облаке (GitHub Actions) запущен и работает. Проверка каждые ~5 минут, независимо от твоего Mac.")
            with open(FLAG_PATH, "w") as f:
                f.write("started")

        if has_center and not no_seats:
            log("SEAT AVAILABLE - attempting reserve")
            tg("🟢🟢🟢 МЕСТО В ДУШАНБЕ НА 5 ДЕКАБРЯ ПОЯВИЛОСЬ! (обнаружено облачным мониторингом) Пытаюсь забронировать...")
            sel_btn = page.query_selector("button:has-text('Select'), a:has-text('Select'), button:has-text('Reserve')")
            if sel_btn:
                sel_btn.click(force=True)
                time.sleep(3)
                tg("Бронь нажата. ИДИ К КОМПЬЮТЕРУ — 20 минут на фото и оплату $68, иначе бронь сгорит.")
            else:
                tg("Место видно, но кнопку бронирования найти не смог — заходи и бронируй вручную ПРЯМО СЕЙЧАС: https://satreg.collegeboard.org/register/testCenterSearch")
        else:
            log("no seats this run")

        context.storage_state(path=STATE_PATH)
        browser.close()


if __name__ == "__main__":
    main()
