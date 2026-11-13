"""Offline test of do_login() against fake College Board sign-in pages (served by
the browser's request routing -- nothing leaves the machine).

12-13 Nov 2026: after a saved session expired, do_login() logged "login ok" 0.04 s
after the password click and went straight to mysat, which cut off the sign-in --
every run until a lucky one was a BLIND RUN. do_login() must now stay until the
sign-in has finished (or the password is rejected), and log only masked page text.

    python3 tests/test_login.py
"""
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
work = tempfile.mkdtemp()
os.environ.update(CB_EMAIL="someone@example.com", CB_PASSWORD="dummy-pw", TG_BOT_TOKEN="dummy",
                  TG_CHAT_ID="dummy", STATE_PATH=os.path.join(work, "state.json"),
                  FLAG_PATH=os.path.join(work, "started.flag"))
sys.path.insert(0, REPO)
import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

LOGS = []
_log = check.log
def rec_log(*a):
    LOGS.append(" ".join(str(x) for x in a))
    _log(*a)
check.log = rec_log

# One page for both steps, like the real one-page sign-in form.
#   picker:  show "Select from the following options" before the password field
#   delay:   ms between the password submit and the site's answer
#   outcome: redirect (signed in, goes to /home) | stay (signed in, same URL, form replaced)
#            | reject ("Unable to sign in", field stays) | hang (no answer at all)
#            | chain (goes to /login/callback, which needs 4 s before it signs in and goes on)
PAGE = """<html><body><div id=app>
<form onsubmit="return step1()"><input id=input28 type=text><input type=submit value=Next></form></div>
<p>Account someone@example.com, ref 12345</p>
<script>
const CFG = %s;
function pwForm() {
  document.getElementById('app').innerHTML =
    "<form onsubmit='return step2()'><input type=password id=pw><input type=submit value=Verify></form>";
}
function step1() {
  setTimeout(() => {
    if (CFG.picker) document.getElementById('app').innerHTML = "<p>Select from the following options</p>" +
      "<a href='#' aria-label='Select Password.' onclick='pwForm(); return false'>Select</a>";
    else pwForm();
  }, 300);
  return false;
}
function step2() {
  if (CFG.outcome === 'hang') return false;
  const ok = document.getElementById('pw').value === CFG.pw;
  setTimeout(() => {
    if (!ok || CFG.outcome === 'reject') {
      // split so the page source itself never contains the phrase (do_login's final
      // check reads the whole HTML, scripts included)
      document.getElementById('app').insertAdjacentHTML('beforeend', '<p>Unable to ' + 'sign in</p>');
      return;
    }
    if (CFG.outcome === 'chain') { location.href = '/login/callback?code=SECRET777'; return; }
    document.cookie = 'cb_signed_in=1; path=/';
    // like the real OAuth return: a one-time code in the query (must never reach the PUBLIC log)
    if (CFG.outcome === 'redirect') location.href = '/home?code=SECRET777&state=s1';
    else document.getElementById('app').innerHTML = '<p>Welcome back</p>';
  }, CFG.delay);
  return false;
}
</script></body></html>"""

CFG = {}
def handler(route):
    url = route.request.url
    if url.startswith("https://account.collegeboard.org/login/callback"):
        # still a login URL, no password field; the sign-in finishes only after 4 s
        route.fulfill(status=200, content_type="text/html", body="<html><body><p>Signing you in</p><script>"
                      "setTimeout(() => { document.cookie = 'cb_signed_in=1; path=/'; location.href = '/home'; }, 4000)"
                      "</script></body></html>")
    elif url.startswith("https://account.collegeboard.org/login/login"):
        route.fulfill(status=200, content_type="text/html", body=PAGE % json.dumps(CFG))
    else:
        route.fulfill(status=200, content_type="text/html", body="<html><body><h1>Home</h1></body></html>")

fails = 0
def expect(cond, msg):
    global fails
    print("   ", "ok  " if cond else "FAIL", msg)
    fails += not cond

def run(b, label, done_timeout=check.LOGIN_DONE_TIMEOUT, **cfg):
    CFG.clear()
    CFG.update(picker=False, delay=3000, outcome="redirect", pw=check.CB_PASSWORD)
    CFG.update(cfg)
    check.LOGIN_DONE_TIMEOUT = done_timeout
    LOGS.clear()
    ctx = b.new_context()
    ctx.route("https://account.collegeboard.org/**", handler)
    page = ctx.new_page()
    t0, err = time.time(), None
    try:
        check.do_login(page)
    except Exception as e:
        err = e
    took = time.time() - t0
    signed = any(c["name"] == "cb_signed_in" for c in ctx.cookies())
    body = ""
    try:
        body = page.inner_text("body")
    except Exception:
        pass
    url = page.url
    ctx.close()
    print(f"--- {label}: {took:.1f}s err={err!r} signed_in={signed} url={url}")
    return took, err, signed, url, body, list(LOGS)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)

    took, err, signed, url, body, logs = run(b, "password shown directly, sign-in takes 3 s, then redirect (12-13 Nov)")
    expect(err is None, "no error")
    expect(signed, "stayed until the sign-in finished (site cookie set before do_login returned)")
    expect(url.split("?")[0].endswith("/home"), "ended on the page the site redirected to")
    expect(not any("SECRET" in l or "code=" in l for l in logs), "no query string (one-time code) in the PUBLIC log")
    expect(took < 15, "no needless waiting (took < 15 s)")
    expect(any("sign-in finished" in l for l in logs), "logs that the sign-in finished")
    expect(any(l.endswith("login ok") for l in logs), "logs \"login ok\" after a confirmed sign-in")
    expect(not any("neither method picker" in l for l in logs), "password field was found by the race")

    took, err, signed, url, body, logs = run(b, "sign-in done on the same URL (form replaced, no redirect)",
                                             delay=2000, outcome="stay")
    expect(err is None and signed, "no error, signed in")
    expect("Welcome back" in body, "left after the site's answer, not before")
    expect(took < 15, "did not wait for the full timeout (took < 15 s)")

    took, err, signed, url, body, logs = run(b, "redirect chain: a login-URL page without a password field for 4 s",
                                             delay=1000, outcome="chain")
    expect(err is None and signed, "waited through the whole chain (site cookie set before do_login returned)")
    expect(url.endswith("/home"), "ended past the chain")
    expect(not any("SECRET" in l or "code=" in l for l in logs), "no query string in the PUBLIC log")

    took, err, signed, url, body, logs = run(b, "method picker shown first", picker=True, delay=1000)
    expect(err is None and signed and url.split("?")[0].endswith("/home"), "picker -> password -> signed in")
    expect(not any("no method-picker shown" in l for l in logs), "picker was recognised and clicked")

    took, err, signed, url, body, logs = run(b, "password rejected 1.5 s after the submit",
                                             delay=1500, outcome="reject")
    expect(isinstance(err, RuntimeError) and "LOGIN_FAILED_BAD_PASSWORD" in str(err),
           "raises LOGIN_FAILED_BAD_PASSWORD (the bot then pauses until the secret changes)")
    expect(took < 15, "noticed at once, not after the full timeout (took < 15 s)")

    took, err, signed, url, body, logs = run(b, "site never answers (timeout 4 s)", done_timeout=4,
                                             outcome="hang")
    expect(err is None, "no crash -- the run goes on and its blind-run check reports it")
    expect(4 <= took < 25, "waited the timeout, then went on")
    expect(not any(l.endswith("login ok") for l in logs), "does not claim \"login ok\" when the sign-in was not confirmed")
    expect(any("login NOT confirmed" in l for l in logs), "says the sign-in is not confirmed")
    warn = [l for l in logs if "sign-in not finished" in l]
    expect(bool(warn), "logs a WARNING with what the page shows")
    expect(bool(warn) and "<email>" in warn[0] and "#####" in warn[0], "page text in the log is masked")
    expect(not any("someone@example.com" in l or "12345" in l for l in logs),
           "no email and no digits from the page in the PUBLIC log")

    b.close()

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
