"""Offline end-to-end test of check.main()'s seat -> reserve path against fake pages.
The College Board flow is replaced by a fixture page, Telegram by a recorder. The
reserve path cannot be exercised live until a real seat appears -- this is the
only way to run it before then.

    python3 tests/test_reserve.py
"""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
work = tempfile.mkdtemp()
os.environ.update(CB_EMAIL="dummy", CB_PASSWORD="dummy-pw", TG_BOT_TOKEN="dummy", TG_CHAT_ID="dummy",
                  STATE_PATH=os.path.join(work, "state.json"), FLAG_PATH=os.path.join(work, "started.flag"))
sys.path.insert(0, REPO)
import check  # noqa: E402

SENT = []
TG_UP = {"up": True}
TG_SEAT_DELAY = {"s": 0}  # how long the seat alert's send takes (slow Telegram)
def fake_tg(text):
    if "ПОЯВИЛОСЬ" in text:
        time.sleep(TG_SEAT_DELAY["s"])
    SENT.append(text)
    return TG_UP["up"]
check.tg = fake_tg
open(check.FLAG_PATH, "w").write("started")  # skip the one-time started message

FIXTURE = {"html": ""}
def fake_flow(context, page):
    page.set_content(FIXTURE["html"])
    return page
check.ensure_on_test_center_search = fake_flow

STEP3 = "<h1>STEP 3 OF 4</h1><p>Upload Your Photo</p><p>You have 20 minutes to complete payment.</p>"

def results(card_inner, extra_head="", continue_html=None, step3_delay=800):
    if continue_html is None:
        continue_html = "<button id='test-location-continue-disabled' disabled>Continue</button>"
    return f"""<html><body>
<div>STEP 2 OF 4 <a href='#'>Select Date and Test Center</a></div>{extra_head}
<div>Choose your test location. Test Date Saturday, December 5, 2026</div>
<div id=results>Find a Test Center View your (1) search results.
 <div class=card><h3>Qsi International School Of Dushanbe</h3><p>Saturday, December 5, 2026</p>
 <p>3 Turakul Zehni Street, Dushanbe, Tajikistan, 20521</p>{card_inner}</div>
 <button id=back>Back</button><span id=contslot>{continue_html}</span></div>
<script>
function enableContinue(delay) {{
  setTimeout(() => {{
    document.getElementById('contslot').innerHTML =
      "<button id='test-location-continue-enabled' onclick='goStep3()'>Continue</button>";
  }}, delay);
}}
function goStep3() {{ setTimeout(() => {{ document.body.innerHTML = {STEP3!r}; }}, {step3_delay}); }}
</script></body></html>"""

SELECT_SAME_PAGE = "<button onclick='enableContinue(1500)'>Select</button>"
SELECT_NEW_TAB = ("<button onclick=\"var w = window.open('about:blank'); w.document.write("
                  "document.documentElement.outerHTML.replace('enableContinue(1500)', 'enableContinue(500)')); "
                  "w.document.close(); setTimeout(() => w.enableContinue(300), 50);\">Select</button>")
SELECT_DEAD = "<button onclick='void 0'>Select</button>"
# someone else takes the seat 5s after the results render unless we click first
SELECT_VANISHING = ("<button id=selbtn onclick='window.__picked = 1; enableContinue(1500)'>Select</button>"
                    "<script>setTimeout(() => { if (!window.__picked) document.getElementById('selbtn')"
                    ".outerHTML = '<p>No seats available.</p>'; }, 5000)</script>")
STALE_TESTDATE = ("<div style='transform:translateX(-5000px)'>Choose a test date "
                  "<button id='testdate-continue-button' onclick=\"document.body.innerHTML='<h1>Choose a test date</h1>'\">Continue</button></div>")
PHOTO_NOTE = "<p>Remember to bring a valid photo ID. Upload instructions follow after payment.</p>"


def run(html, label):
    FIXTURE["html"] = html
    SENT.clear()
    try:
        check.main()
        code = 0
    except SystemExit as e:
        code = e.code
    print(f"--- {label}: exit={code}")
    for s in SENT:
        print("      tg:", s[:90].replace("\n", " "))
    return code, list(SENT)

fails = 0
def expect(cond, msg):
    global fails
    print("   ", "ok  " if cond else "FAIL", msg)
    fails += not cond

c, s = run(results(SELECT_SAME_PAGE), "seat, same-page SPA flow, Continue appears after 1.5s")
expect(len(s) == 2 and "ПОЯВИЛОСЬ" in s[0] and s[1].startswith("✅"), "seat alert then ✅ confirmation")
expect(c == 0, "exit 0")

c, s = run(results(SELECT_NEW_TAB), "seat, Select opens a NEW TAB")
expect(len(s) == 2 and s[1].startswith("✅"), "followed the new tab to the photo step")

c, s = run(results(SELECT_DEAD), "seat, but Continue never enables")
expect(len(s) == 2 and s[1].startswith("⚠️"), "honest 'not sure, go manually'")

c, s = run(results(SELECT_DEAD, extra_head=PHOTO_NOTE), "results page already mentions photo/upload/payment, clicks do nothing")
expect(len(s) == 2 and s[1].startswith("⚠️"), "no false ✅ from pre-existing 'photo' text")

c, s = run(results(SELECT_SAME_PAGE, extra_head=STALE_TESTDATE), "stale ENABLED testdate-continue-button mounted earlier in DOM")
expect(len(s) == 2 and s[1].startswith("✅"), "clicked the real Continue, not the stale one")

c, s = run(results(SELECT_SAME_PAGE, extra_head=PHOTO_NOTE, step3_delay=8000),
           "results page mentions photo/payment, step 3 renders 8s after Continue (site under load)")
expect(len(s) == 2 and s[1].startswith("✅"), "waited for the NEW step text, not the pre-existing 'photo'")

ORIG_FIND_CONTINUE = check.FIND_CONTINUE_JS
check.FIND_CONTINUE_JS = ("() => { if (!window.__threwOnce) { window.__threwOnce = true; "
                          "throw new Error('Execution context was destroyed (simulated)'); } "
                          "return (" + ORIG_FIND_CONTINUE.strip() + ")(); }")
c, s = run(results(SELECT_SAME_PAGE), "first Continue lookup throws (page still navigating after Select)")
expect(len(s) == 2 and s[1].startswith("✅"), "kept polling and completed the reserve, no abort")
check.FIND_CONTINUE_JS = ORIG_FIND_CONTINUE

TG_SEAT_DELAY["s"] = 20
c, s = run(results(SELECT_VANISHING), "Telegram takes 20s to send; the seat is taken by someone else 5s after render")
expect(len(s) == 2 and any("ПОЯВИЛОСЬ" in x for x in s) and any(x.startswith("✅") for x in s),
       "Select clicked before the seat vanished; both alerts still delivered")
expect(c == 0, "exit 0")
TG_SEAT_DELAY["s"] = 0

TG_UP["up"] = False
c, s = run(results(SELECT_SAME_PAGE), "seat, Telegram completely down")
expect(c == 1, "run fails so GitHub emails the owner")
TG_UP["up"] = True

c, s = run(results("<p>No seats available.</p>"), "plain no seats (with a stepper LINK 'Select Date and Test Center')")
expect(c == 0 and s == [], "quiet -- the old row logic's false-positive case")
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
