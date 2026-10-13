"""Offline checks of check.py's seat probe against fake result pages.

Run locally (needs `pip install playwright && playwright install chromium`):
    python3 tests/test_probe.py
No network, no credentials -- every page here is a local fixture.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for k in ["CB_EMAIL", "CB_PASSWORD", "TG_BOT_TOKEN", "TG_CHAT_ID"]:
    os.environ.setdefault(k, "dummy")
sys.path.insert(0, REPO)
import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

# a deliberately hostile page around the result card: a Dushanbe mention in a
# <script>, a hidden old wizard step with a "Select" button, an autocomplete list
# mentioning Dushanbe, and a stepper BUTTON whose text starts with "Select"
HEAD = """<html><head><title>SAT</title>
<script>window.__STATE__ = {"city": "Dushanbe", "school": "Qsi International"};</script></head><body>
<div id="stepper">STEP 2 OF 4 <button>Select Date and Test Center</button></div>
<div id="hidden-old-step" style="display:none"><button>Select</button> Dushanbe</div>
<div id="form">Choose your test location <input id="test-center-country-city-input" value="Dushanbe">
<ul id="autocomplete" style="position:absolute"><li>Dushanbe, Tajikistan</li></ul>
<select id="country"><option>Tajikistan</option><option>Turkey</option></select>
<button id="test-center-country-find-button-enabled">Find</button></div>
<div id="results">Find a Test Center View your (N) search results. <button>Filter</button>
"""
TAIL = """<nav>Go to previous page page 1 Go to next page</nav>
<button id="back">Back</button><button id="cont-continue">Continue</button></div></body></html>"""


def card(name, addr, inner):
    return f"""<div class="card"><h3>{name}</h3><p>Saturday, December 5, 2026</p>
<a href="#">click here to open the address in Google Maps</a><p>{addr}</p>{inner}</div>"""


def card_nodate(name, addr, inner):
    return card(name, addr, inner).replace("<p>Saturday, December 5, 2026</p>", "")


def card_2maps(name, addr, inner):
    return card(name, addr, inner).replace(
        "<p>" + addr + "</p>", f'<a href="https://maps.google.com/?q=x">{addr}</a>')


QSI = ("Qsi International School Of Dushanbe", "3 Turakul Zehni Street, Dushanbe, Tajikistan, 20521")
KHU = ("Khujand Lyceum", "1 Lenin St, Khujand, Tajikistan")
DUS2 = ("Dushanbe Presidential School", "5 Rudaki Ave, Dushanbe, Tajikistan")
NO = "<p>No seats available.</p>"
RADIO = "<label><input type=radio name=c> Choose</label>"
STRAY_HEADING = "<h2>Test centers near Qsi International School Of Dushanbe</h2>"

CASES = {
    "live-like no seats": ([card(*QSI, NO)], "no_seats"),
    "seat: <button>Select</button>": ([card(*QSI, "<button>Select</button>")], "button"),
    "seat: div[role=button] Select": ([card(*QSI, "<div role='button' tabindex=0>Select</div>")], "button"),
    "seat: aria-label only": ([card(*QSI, "<button aria-label='Select this test center'><svg width=10 height=10></svg></button>")], "button"),
    "seat: radio only (unrecognised)": ([card(*QSI, "<label><input type=radio name=c> Choose</label>")], "unknown"),
    "seat btn disabled, no text": ([card(*QSI, "<button disabled>Select</button>")], "unknown"),
    "seat btn aria-disabled": ([card(*QSI, "<button aria-disabled='true'>Select</button>")], "unknown"),
    "other center has seat, QSI none": ([card(*KHU, "<button>Select</button>"), card(*QSI, NO)], "no_seats"),
    "QSI has seat, other center none": ([card(*KHU, NO), card(*QSI, "<button>Select</button>")], "button"),
    "QSI absent, other center seat": ([card(*KHU, "<button>Select</button>")], "?"),
    "no results at all": ([], "?"),
    # QSI's own card shows neither signal: the walk must not borrow a NEIGHBOUR's
    "QSI radio, next card no seats": ([card(*QSI, RADIO), card(*KHU, NO)], "unknown"),
    "prev card no seats, QSI radio": ([card(*KHU, NO), card(*QSI, RADIO)], "unknown"),
    "QSI text-only seat, Dushanbe #2 none": ([card(*QSI, "<p>Seats available</p>"), card(*DUS2, NO)], "unknown"),
    "QSI no status, other center Select": ([card(*QSI, ""), card(*KHU, "<button>Select</button>")], "unknown"),
    "two QSI rows: radio + no seats": ([card(*QSI, RADIO), card(*QSI, NO)], "unknown"),
    "no date lines: Maps-link fallback": ([card_nodate(*QSI, RADIO), card_nodate(*KHU, NO)], "unknown"),
    "no date lines, plain no seats": ([card_nodate(*QSI, NO), card_nodate(*KHU, NO)], "no_seats"),
    "card with 2 Maps links, no seats": ([card_2maps(*QSI, NO)], "no_seats"),
    "card with 2 Maps links, seat": ([card_2maps(*QSI, "<button>Select</button>")], "button"),
    "stray QSI heading above 2 cards": ([STRAY_HEADING, card(*KHU, NO), card(*QSI, NO)], "no_seats"),
    "stray QSI heading, QSI has seat": ([STRAY_HEADING, card(*KHU, NO), card(*QSI, "<button>Select</button>")], "button"),
}


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        bad = 0
        for name, (cards, want) in CASES.items():
            pg.set_content(HEAD + "".join(cards) + TAIL)
            info, _btn = check.probe_seat_state(pg)
            got = info["verdict"]
            ok = (want == "?") or (got == want)
            bad += not ok
            print(f"{'OK ' if ok else 'BAD'} {name:38s} want={want:9s} got={got:9s} btn={info['btn_label']!r} "
                  f"card={info['card_tag']} len={info['card_len']} starts={info['starts']}")
        # the stuck-wizard dump goes to PUBLIC run logs: shape yes, personal data no
        pg.set_content("""<h2>Confirm Contact Information</h2><p>osim.test@example.com, +992 93 555 0142</p>
            <p>12 Rudaki Ave, Dushanbe 734025. Born 05/14/2009</p>
            <input id=phone value='+992935550142'><input id=pw type=password value='hunter2'>
            <select id=dob-year><option>2009</option></select>
            <select id=qc-id-personalinfo-list-grade><option value=6 selected>11th Grade</option></select>""")
        dump = str(pg.evaluate(check.STUCK_DUMP_JS))
        leaked = [v for v in ["osim.test@example.com", "555", "0142", "734025", "2009", "hunter2", "Rudaki"]
                  if v in dump]
        ok = (leaked == ["Rudaki"] and "'value': '6'" in dump and "value_len" in dump
              and "'pw'" not in dump)  # not even the password's length
        bad += not ok
        print(f"{'OK ' if ok else 'BAD'} stuck dump: personal values masked (street name stays) leaked={leaked}")
        b.close()
    print("FAILURES:", bad)
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
