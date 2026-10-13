"""Drive check.main() end-to-end offline: the College Board flow is replaced by a
fixture page, Telegram by a recorder. Verifies exit codes, alerts and flag files
across consecutive runs (blind / recovered / unrecognised row / no seats).

    python3 tests/test_states.py
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
work = tempfile.mkdtemp()
os.environ.update(CB_EMAIL="dummy", CB_PASSWORD="dummy-pw", TG_BOT_TOKEN="dummy", TG_CHAT_ID="dummy",
                  STATE_PATH=os.path.join(work, "state.json"), FLAG_PATH=os.path.join(work, "started.flag"))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check  # noqa: E402
from test_probe import HEAD, TAIL, card, QSI, NO  # noqa: E402

SENT = []
check.tg = lambda text: (SENT.append(text), True)[1]
FIXTURE = {"html": ""}


def fake_flow(context, page):
    page.set_content(FIXTURE["html"])
    return page


check.ensure_on_test_center_search = fake_flow


def run(html, label):
    FIXTURE["html"] = html
    SENT.clear()
    try:
        check.main()
        code = 0
    except SystemExit as e:
        code = e.code
    flags = sorted(f for f in os.listdir(work) if f.startswith("started.flag"))
    print(f"--- {label}: exit={code} alerts={[s[:60] for s in SENT]} flags={flags}")
    return code, list(SENT)


fails = 0


def expect(cond, msg):
    global fails
    if not cond:
        fails += 1
        print("   FAIL:", msg)


# live-like head: the live body text is ~3600 chars and has no Select-ish control
# outside the card; the hostile head (test_probe.HEAD) has a stepper BUTTON
# "Select Date and Test Center", a hidden "Select" and stray Dushanbe mentions
COUNTRIES = "".join(f"<option>Country number {i}</option>" for i in range(150))
HEAD_LIVELIKE = (HEAD.replace("<button>Select Date and Test Center</button>", "<span>Select Date and Test Center</span>")
                     .replace('<div id="hidden-old-step" style="display:none"><button>Select</button> Dushanbe</div>', "")
                     .replace("<option>Tajikistan</option>", "<option>Tajikistan</option>" + COUNTRIES))

for head_name, head in [("live-like", HEAD_LIVELIKE), ("hostile", HEAD)]:
    print(f"===== {head_name} page =====")
    for f in os.listdir(work):
        os.remove(os.path.join(work, f))
    page_no_seats = head.replace("(N)", "(1)") + card(*QSI, NO) + TAIL
    page_blind = "<html><body>STEP 2 OF 4 Choose your test location <button>Find</button></body></html>"
    page_wrong_date = (head + card(*QSI, NO) + TAIL).replace("December 5, 2026", "November 7, 2026")
    page_unknown = head + card(*QSI, "<label><input type=radio> Choose</label>") + TAIL

    c, s = run(page_no_seats, "no seats (first run ever)")
    expect(c == 0, "exit 0")
    expect(len(s) == 1 and "запущен" in s[0], "only the one-time started msg")
    c, s = run(page_no_seats, "no seats again")
    expect(c == 0 and s == [], "quiet, exit 0")
    c, s = run(page_blind, "blind #1")
    expect(c == 1 and s == [], "exit 1, no alert on first blind run")
    c, s = run(page_blind, "blind #2")
    expect(c == 1 and len(s) == 1 and "НЕ дошли" in s[0], "alert on 2nd consecutive blind run")
    c, s = run(page_blind, "blind #3 (throttled)")
    expect(c == 1 and s == [], "throttled")
    c, s = run(page_wrong_date, "wrong date shown")
    expect(c == 1 and s == [], "wrong date = blind (alert throttled), not 'no seats'")
    c, s = run(page_no_seats, "recovered")
    expect(c == 0 and len(s) == 1 and "снова видит" in s[0], "recovery notice")
    c, s = run(page_unknown, "unknown row state (radio)")
    expect(c == 0 and len(s) == 1 and "ВОЗМОЖНО, МЕСТО ЕСТЬ" in s[0], "possible-seat alert")
    c, s = run(page_unknown, "unknown again (throttled 30 min)")
    expect(s == [], "throttled")
    c, s = run(page_no_seats, "back to no seats")
    expect(c == 0 and s == [] and not os.path.exists(check.UNKNOWN_ALERT_PATH), "re-armed")
    c, s = run(page_unknown, "unknown again after re-arm")
    expect(len(s) == 1, "alerts immediately again")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
