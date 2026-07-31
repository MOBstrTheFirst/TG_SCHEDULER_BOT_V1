#!/usr/bin/env python3
"""Tests for the only part of the bot that can be wrong in a subtle way."""
import os
import sys
import tempfile

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("ADMIN_ID", "1")
os.environ["BOT_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

import bot  # noqa: E402

ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
    else:
        fail += 1
        print("FAIL %s\n  got  %r\n  want %r" % (label, got, want))


H = 60
# ---- subtract ---------------------------------------------------------------
check("no blocks", bot.subtract([(19 * H, 23 * H)], []), [(19 * H, 23 * H)])
check("middle bite", bot.subtract([(19 * H, 23 * H)], [(20 * H, 21 * H)]),
      [(19 * H, 20 * H), (21 * H, 23 * H)])
check("front bite", bot.subtract([(19 * H, 23 * H)], [(19 * H, 21 * H)]), [(21 * H, 23 * H)])
check("whole day", bot.subtract([(19 * H, 23 * H)], [(16 * H, 26 * H)]), [])
check("sliver dropped", bot.subtract([(19 * H, 23 * H)], [(19 * H + 30, 23 * H)]), [])
check("two blocks", bot.subtract([(16 * H, 26 * H)], [(17 * H, 18 * H), (20 * H, 22 * H)]),
      [(16 * H, 17 * H), (18 * H, 20 * H), (22 * H, 26 * H)])
check("past midnight kept", bot.subtract([(22 * H, 26 * H)], [(23 * H, 24 * H)]),
      [(22 * H, 23 * H), (24 * H, 26 * H)])
check("block outside base", bot.subtract([(19 * H, 23 * H)], [(10 * H, 12 * H)]),
      [(19 * H, 23 * H)])

# ---- effective + candidates -------------------------------------------------
bot.opendb()
from datetime import date, timedelta  # noqa: E402

DAY = (date.today() + timedelta(days=1)).isoformat()
WD = date.fromisoformat(DAY).weekday()

for i, (a, b) in enumerate([(19 * H, 23 * H), (19 * H, 23 * H), (19 * H, 23 * H),
                            (21 * H, 26 * H), (16 * H, 18 * H)], start=1):
    bot.x("INSERT INTO member(id,name,joined_at) VALUES(?,?,'2026-01-01')", i, "M%d" % i)
    bot.x("INSERT INTO baseline(member_id,weekday,start_min,end_min) VALUES(?,?,?,?)", i, WD, a, b)

ms = [1, 2, 3, 4, 5]
check("effective clean", bot.effective(1, DAY), [(19 * H, 23 * H)])

bot.x("INSERT INTO busy(member_id,day,start_min,end_min) VALUES(?,?,?,?)", 2, DAY, 19 * H, 21 * H)
check("effective with block", bot.effective(2, DAY), [(21 * H, 23 * H)])

cs = bot.candidates([DAY], ms)
check("one candidate", len(cs), 1)
day, s, e, who = cs[0]
check("window start", bot.hhmm(s), "21:00")
check("window end", bot.hhmm(e), "23:00")
check("window members", who, [1, 2, 3, 4])

# member 5 never overlaps the evening crowd
check("m5 excluded", 5 in who, False)

# raise the bar past what anyone can meet
check("no candidate at 6", bot.candidates([DAY], ms, min_players=6), [])

# a weekday nobody set a baseline for (baselines exist only for WD)
EMPTY = (date.today() + timedelta(days=2)).isoformat()
check("empty weekday", bot.candidates([EMPTY], ms), [])
check("effective on empty weekday", bot.effective(1, EMPTY), [])

# ---- formatting -------------------------------------------------------------
check("hhmm 26h wraps", bot.hhmm(26 * H), "02:00")
check("hhmm plain", bot.hhmm(20 * H + 30), "20:30")
check("span", bot.span(20 * H, 23 * H), "20:00-23:00")

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
