"""Checks the knowledge base follow-up and the currency wording, without a call.

Two things in this app are hard to see until a customer is already on the line:
whether a knowledge base answer actually gets delivered, and whether amounts are
read out as words. Both are pure logic, so both can be checked here.

    python scripts/check_knowledge_flow.py

Exits non-zero on the first thing that is wrong.
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import app  # noqa: E402
import context  # noqa: E402
import db  # noqa: E402

# These checks are about logic, not bookkeeping.
app.db.add_transcript_line = lambda *a, **k: None

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, wanted {want!r}")
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# The follow-up state machine
# ---------------------------------------------------------------------------

CREATED = {"type": "response.created"}
DONE = {"type": "response.done"}
SPEAKING = {"type": "input_audio_buffer.speech_started"}
STOPPED = {"type": "input_audio_buffer.speech_stopped"}


def search_started(item="m1"):
    return {"type": "response.mcp_call.in_progress", "item_id": item}


def search_finished(item="m1"):
    return {"type": "response.mcp_call.completed", "item_id": item}


def call(name, **overrides):
    """A fresh call with the timers wound right down."""
    app._mcp_state.pop(name, None)
    state = app._mcp(name)
    state.update(
        {
            "hold_delay": 0.0,
            "hold_interval": 0.0,
            "hold_max": 2,
            "hold_enabled": True,
            "timeout": 30.0,
            "customer": None,
        }
    )
    state.update(overrides)
    return state


def feed(name, *events):
    out = []
    for event in events:
        out.extend(app._handle_event(name, event))
    return out


def kinds(events):
    """Tells the two kinds of outbound response apart."""
    return [
        "hold" if (e.get("response") or {}).get("instructions") else "answer"
        for e in events
    ]


print("knowledge base follow-up")

# The service may finish the response before the search, or after. Neither
# order is safe to assume, so both are checked.
call("done-first", hold_max=0)
check("nothing while the search runs", kinds(feed("done-first", CREATED, search_started(), DONE)), [])
check("answer once it lands", kinds(feed("done-first", search_finished())), ["answer"])

call("search-first", hold_max=0)
feed("search-first", CREATED, search_started(), search_finished())
check("nothing while the agent speaks", kinds(feed("search-first", {"type": "noop"})), [])
check("answer once the agent stops", kinds(feed("search-first", DONE)), ["answer"])

# The caller having the floor defers the answer; it is never dropped.
call("interrupted", hold_max=0)
feed("interrupted", CREATED, search_started(), DONE)
check("held back while the caller speaks", kinds(feed("interrupted", SPEAKING, search_finished())), [])
check("delivered when they finish", kinds(feed("interrupted", STOPPED)), ["answer"])

# The follow-up must not arm another follow-up, or the call never ends.
call("no-loop", hold_max=0)
feed("no-loop", CREATED, search_started(), DONE, search_finished())
check("a tool-free response does not loop", kinds(feed("no-loop", CREATED, DONE)), [])

# A search that never reports back has to be let go of.
call("stuck", timeout=0.05, hold_max=0)
feed("stuck", CREATED, search_started(), DONE)
time.sleep(0.08)
check("timeout answers anyway", kinds(app._mcp_tick("stuck")), ["answer"])

print("holding phrases")

# Holding phrases are responses too, so each one occupies the floor.
call("waiting", hold_delay=0.02, hold_interval=0.02)
feed("waiting", CREATED, search_started(), DONE)
spoken = []
for _ in range(5):
    time.sleep(0.03)
    out = app._mcp_tick("waiting")
    spoken.extend(kinds(out))
    if out:
        feed("waiting", CREATED, DONE)
check("two, then silence", spoken, ["hold", "hold"])

state = call("busy")
feed("busy", CREATED, search_started())
check("silent while the agent speaks", kinds(app._mcp_tick("busy")), [])
state["active"], state["caller"] = False, True
check("silent while the caller speaks", kinds(app._mcp_tick("busy")), [])

call("landed")
feed("landed", CREATED, search_started(), DONE)
app._mcp_tick("landed")
feed("landed", CREATED, DONE)
check("the answer wins over another hold", kinds(feed("landed", search_finished())), ["answer"])

print("currency wording")

bare = re.compile(r"\b(QAR|USD|EUR|GBP|AED|SAR)\b")
template = app.agent_settings()["template"]

for row in db.list_customers():
    customer = db.get_customer(row["id"])
    name = customer["full_name_en"]

    leaked = bare.search(context.build_profile(customer))
    if leaked:
        check(f"{name} profile", leaked.group(0), "no bare code")

    for line in context.build_instructions(template, customer).splitlines():
        # The rule itself has to quote the code in order to teach it.
        if line.startswith("Pronunciation:") or not bare.search(line):
            continue
        check(f"{name} prompt", line.strip()[:70], "no bare code")

print(f"  ok    {len(db.list_customers())} profiles say amounts as words")

for amount, code, want in [
    (4820, "QAR", "4,820.00 Qatari riyals"),
    (1, "QAR", "1.00 Qatari riyal"),
    (-250.5, "USD", "-250.50 US dollars"),
    (99, "XYZ", "XYZ 99.00"),
]:
    check(f"{amount} {code}", context._money(amount, code), want)

print()
if FAILURES:
    print(f"{len(FAILURES)} problem(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all good")
