#!/usr/bin/env python3
"""
Gaming session bot - toy build.

One file, standard library only, SQLite for state, long polling.
No pip install, no framework to break on upgrade. Runs anywhere Python 3.11+
runs: a laptop, a Raspberry Pi, a free Oracle Cloud VM, Termux on a phone.

    export BOT_TOKEN=123456:AA...        # from @BotFather
    export ADMIN_ID=123456789            # your numeric Telegram id, from @userinfobot
    python3 bot.py

Everything lives in sessions.db next to this file. Back it up by copying it.
"""

import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- config ----

# Secrets live in a file called .env next to this one, never in this file.
# Copy .env.example to .env and put your token and id there.
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    for _line in open(_env):
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.strip().split("=", 1)
            os.environ.setdefault(_k, _v.strip().strip("'\""))

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DB_PATH = os.environ.get("BOT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db"))

DAY_START = 0                # 00:00, earliest slot offered
DAY_END = 24 * 60            # 24:00, i.e. the whole day
SLOT = 30                    # minutes per slot
MIN_PLAYERS = 3              # how many committed makes a session worth keeping (see recheck())
MIN_SESSION = 90             # minutes
MIN_FRAGMENT = 60            # ignore leftover slivers shorter than this
POLL_HOURS = 24              # how long a poll stays open
RECONFIRM_HOURS = 72         # ask again this long after agreement
STALE_DAYS = 7               # mark availability older than this
SUGGEST_HOUR = 19            # daily suggestion goes out at this hour, local
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
GAMES = ["Deep Rock", "CS2", "Valorant", "Baldur's Gate 3", "Minecraft", "Anything"]
STATUSES = ("9/5", "Remote", "On Vacation (No gaming)", "On Vacation (Can Join)")

API = "https://api.telegram.org/bot%s/" % TOKEN


# ------------------------------------------------------------ telegram io ---

def api(method, **params):
    """Call the Bot API. Returns the result dict, or None on a handled error."""
    data = json.dumps(params).encode()
    req = urllib.request.Request(API + method, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["result"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        # 403 = user blocked the bot. 400 = message unchanged, or gone. Both survivable.
        if e.code not in (400, 403):
            log("api %s failed: %s %s" % (method, e.code, body))
        return None
    except Exception as e:
        log("api %s error: %r" % (method, e))
        time.sleep(3)
        return None


def send(chat_id, text, kb=None, **extra):
    return api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
               reply_markup=kb or {"inline_keyboard": []}, **extra)


def edit(chat_id, msg_id, text, kb=None):
    return api("editMessageText", chat_id=chat_id, message_id=msg_id, text=text,
               parse_mode="HTML", reply_markup=kb or {"inline_keyboard": []})


def answer(cb_id, text=None):
    api("answerCallbackQuery", callback_query_id=cb_id, text=text or "")


def kb(*rows):
    """kb(["label", "cbdata"], ...) or kb([["a","1"],["b","2"]], ...) for a row."""
    out = []
    for row in rows:
        if row and isinstance(row[0], str):
            row = [row]
        out.append([{"text": b[0], "callback_data": b[1]} for b in row])
    return {"inline_keyboard": out}


def log(msg):
    print("%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# -------------------------------------------------------------- database ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS member (
    id INTEGER PRIMARY KEY, name TEXT, joined_at TEXT,
    muted INTEGER DEFAULT 0, touched_at TEXT, nickname TEXT, status TEXT, timezone TEXT);
CREATE TABLE IF NOT EXISTS baseline (
    member_id INTEGER, status TEXT, weekday INTEGER, start_min INTEGER, end_min INTEGER,
    PRIMARY KEY (member_id, status, weekday));
CREATE TABLE IF NOT EXISTS busy (
    id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER, day TEXT,
    start_min INTEGER, end_min INTEGER);
CREATE TABLE IF NOT EXISTS poll (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_by INTEGER, created_at TEXT,
    closes_at TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS poll_option (
    id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id INTEGER, day TEXT,
    start_min INTEGER, end_min INTEGER);
CREATE TABLE IF NOT EXISTS vote (
    option_id INTEGER, member_id INTEGER, commitment TEXT,
    PRIMARY KEY (option_id, member_id));
CREATE TABLE IF NOT EXISTS game_vote (
    poll_id INTEGER, member_id INTEGER, game TEXT,
    PRIMARY KEY (poll_id, member_id, game));
CREATE TABLE IF NOT EXISTS msg (
    kind TEXT, ref INTEGER, member_id INTEGER, chat_id INTEGER, message_id INTEGER,
    PRIMARY KEY (kind, ref, member_id));
CREATE TABLE IF NOT EXISTS sess (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, start_min INTEGER,
    end_min INTEGER, game TEXT, status TEXT, agreed_at TEXT, origin TEXT);
CREATE TABLE IF NOT EXISTS part (
    sess_id INTEGER, member_id INTEGER, commitment TEXT,
    confirm TEXT DEFAULT 'pending', reason TEXT,
    PRIMARY KEY (sess_id, member_id));
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER, title TEXT,
    game TEXT, day TEXT, start_min INTEGER, end_min INTEGER,
    weekly INTEGER DEFAULT 0, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS act_member (
    act_id INTEGER, member_id INTEGER, commitment TEXT,
    PRIMARY KEY (act_id, member_id));
CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, run_at TEXT, ref INTEGER,
    done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sent (
    member_id INTEGER, day TEXT, PRIMARY KEY (member_id, day));
CREATE TABLE IF NOT EXISTS state (
    member_id INTEGER PRIMARY KEY, key TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

db = None


def opendb():
    global db
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()
    migrate()


def migrate():
    """Bring tables from an older build up to the current schema."""
    cols = {r["name"] for r in q("PRAGMA table_info(member)")}
    for col in ("nickname", "status", "timezone"):
        if col not in cols:
            db.execute("ALTER TABLE member ADD COLUMN %s TEXT" % col)

    # baseline gained a 'status' column and a new primary key that includes it -
    # ALTER TABLE can't change a primary key, so the old table has to be rebuilt.
    bcols = {r["name"] for r in q("PRAGMA table_info(baseline)")}
    if "status" not in bcols:
        db.execute("ALTER TABLE baseline RENAME TO baseline_old")
        db.execute("CREATE TABLE baseline (member_id INTEGER, status TEXT, weekday INTEGER, "
                   "start_min INTEGER, end_min INTEGER, PRIMARY KEY (member_id, status, weekday))")
        db.execute("INSERT INTO baseline(member_id,status,weekday,start_min,end_min) "
                   "SELECT member_id, '', weekday, start_min, end_min FROM baseline_old")
        db.execute("DROP TABLE baseline_old")
    db.commit()

    # status used to be a free-text line; anything that isn't one of the fixed
    # options now forces that member through status selection again on /start.
    db.execute("UPDATE member SET status=NULL WHERE status IS NOT NULL AND status NOT IN (%s)" %
              ",".join("?" * len(STATUSES)), STATUSES)
    db.commit()


def q(sql, *a):
    return db.execute(sql, a).fetchall()


def q1(sql, *a):
    r = db.execute(sql, a).fetchone()
    return r


def x(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur.lastrowid


def getkv(k, default=None):
    r = q1("SELECT v FROM kv WHERE k=?", k)
    return r["v"] if r else default


def setkv(k, v):
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?", k, str(v), str(v))


# ------------------------------------------------------------ time helpers --

def hhmm(m):
    m = int(m) % (24 * 60) if m >= 24 * 60 else int(m)
    return "%02d:%02d" % (m // 60, m % 60)


def span(a, b):
    return "%s-%s" % (hhmm(a), hhmm(b))


def d(s):
    return date.fromisoformat(s)


def pretty(day):
    dd = d(day)
    return "%s %d %s" % (WEEKDAYS[dd.weekday()], dd.day, dd.strftime("%b"))


def now():
    return datetime.now()


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def host_offset_minutes(day=None):
    """The host machine's UTC offset on the given date (today if omitted), in minutes -
    the clock everything is stored on. Computed per-date, not cached, since the host's
    own civil offset can itself shift across a DST boundary."""
    if day is None:
        dt = datetime.now()
    else:
        dd = d(day)
        dt = datetime(dd.year, dd.month, dd.day, 12)
    off = dt.astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


def member_offset_minutes(mid, day=None):
    """This member's UTC offset on the given date. Looked up live via zoneinfo rather
    than stored as a fixed number, so zones with DST (Berlin, Sydney, ...) give the
    right offset for winter vs. summer instead of whatever they were on when picked."""
    r = q1("SELECT timezone FROM member WHERE id=?", mid)
    if not r or r["timezone"] in (None, ""):
        return host_offset_minutes(day)
    offset = int(r["timezone"])
    zone = TZ_ZONE.get(offset)
    if not zone:
        return offset * 60
    dd = d(day) if day else date.today()
    off = datetime(dd.year, dd.month, dd.day, 12, tzinfo=ZoneInfo(zone)).utcoffset()
    return int(off.total_seconds() // 60)


def localize(day, start_min, end_min, viewer):
    """Shift a (day, start, end) stored on the host's clock to viewer's own clock - display only."""
    delta = member_offset_minutes(viewer, day) - host_offset_minutes(day)
    if delta == 0:
        return day, start_min, end_min
    day_shift, local_start = divmod(start_min + delta, 24 * 60)
    local_end = end_min + delta - day_shift * 24 * 60
    return (d(day) + timedelta(days=day_shift)).isoformat(), local_start, local_end


def localize_dt(dt, viewer):
    day = dt.date().isoformat()
    delta = member_offset_minutes(viewer, day) - host_offset_minutes(day)
    return dt + timedelta(minutes=delta)


# ------------------------------------------------- pure availability logic --
# These four functions hold every rule that matters. They touch no database
# and no network, which is what makes them testable (see test_logic.py).

def subtract(base, blocks):
    """base and blocks are [(start,end)] in minutes. Returns base minus blocks."""
    out = []
    for bs, be in base:
        pieces = [(bs, be)]
        for cs, ce in blocks:
            nxt = []
            for ps, pe in pieces:
                if ce <= ps or cs >= pe:
                    nxt.append((ps, pe))
                    continue
                if cs > ps:
                    nxt.append((ps, cs))
                if ce < pe:
                    nxt.append((ce, pe))
            pieces = nxt
        out.extend(pieces)
    return [(s, e) for s, e in sorted(out) if e - s >= MIN_FRAGMENT]


def effective(member_id, day):
    """What this member can actually make on this host-clock date, for their current status.

    Baseline and busy are entered in the member's own local time (that's what the
    hour-picker shows them). This is the one place that gets converted onto the
    shared clock everything else - slot_counts, candidates, the board, polls,
    sessions - is built on, so two members' availability is actually comparable.
    """
    status = current_status(member_id)
    if not status:
        return []
    delta = host_offset_minutes(day) - member_offset_minutes(member_id, day)
    target = d(day)
    base = []
    for day_offset in range(-2, 3):
        wd = (target + timedelta(days=day_offset)).weekday()
        for r in q("SELECT start_min,end_min FROM baseline WHERE member_id=? AND status=? AND weekday=?",
                   member_id, status, wd):
            s = r["start_min"] + delta + day_offset * 24 * 60
            e = r["end_min"] + delta + day_offset * 24 * 60
            cs, ce = max(s, 0), min(e, 24 * 60)
            if ce > cs:
                base.append((cs, ce))
    if not base:
        return []
    blocks = [(r["start_min"] + delta, r["end_min"] + delta)
              for r in q("SELECT start_min,end_min FROM busy WHERE member_id=? AND day=?",
                         member_id, day)]
    return subtract(base, blocks)


def slot_counts(day, members):
    """For each slot in the day span, who is free. Returns [(slot_start, [ids])]."""
    rows = []
    avail = {m: effective(m, day) for m in members}
    for t in range(DAY_START, DAY_END, SLOT):
        who = [m for m, iv in avail.items()
               if any(s <= t and e >= t + SLOT for s, e in iv)]
        rows.append((t, who))
    return rows


def candidates(days, members, min_players=1):
    """Maximal runs where at least min_players are free for long enough."""
    out = []
    for day in days:
        run_start, run_members = None, None
        counts = slot_counts(day, members)
        for t, who in counts + [(DAY_END, [])]:
            ok = len(who) >= min_players
            if ok and run_start is None:
                run_start, run_members = t, set(who)
            elif ok:
                run_members &= set(who)
                if len(run_members) < min_players:
                    if t - run_start >= MIN_SESSION:
                        out.append((day, run_start, t, sorted(run_members | set(who))))
                    run_start, run_members = t, set(who)
            elif run_start is not None:
                if t - run_start >= MIN_SESSION:
                    out.append((day, run_start, t, sorted(run_members)))
                run_start, run_members = None, None
    out.sort(key=lambda c: (-len(c[3]), -(c[2] - c[1]), c[0], c[1]))
    return out


# ------------------------------------------------------------ member utils --

def members():
    return [r["id"] for r in q("SELECT id FROM member ORDER BY joined_at")]


def name(mid):
    r = q1("SELECT name,nickname FROM member WHERE id=?", mid)
    if not r:
        return str(mid)
    return r["nickname"] or r["name"]


def names(ids):
    return ", ".join(name(i) for i in ids) if ids else "nobody"


def is_member(uid):
    return q1("SELECT 1 FROM member WHERE id=?", uid) is not None


def current_status(mid):
    r = q1("SELECT status FROM member WHERE id=?", mid)
    return r["status"] if r else None


def has_baseline(mid, status=None):
    status = status or current_status(mid)
    return bool(status) and q1("SELECT 1 FROM baseline WHERE member_id=? AND status=?", mid, status) is not None


def onboarding_complete(mid):
    r = q1("SELECT nickname,status,timezone FROM member WHERE id=?", mid)
    return bool(r and r["nickname"] and r["status"] and r["timezone"] and has_baseline(mid, r["status"]))


def stale(mid):
    r = q1("SELECT touched_at FROM member WHERE id=?", mid)
    if not r or not r["touched_at"]:
        return True
    return (now() - parse_iso(r["touched_at"])).days > STALE_DAYS


def touch(mid):
    x("UPDATE member SET touched_at=? WHERE id=?", iso(now()), mid)


def week_days(offset=0):
    start = date.today() + timedelta(days=offset * 7)
    return [(start + timedelta(days=i)).isoformat() for i in range(7)]


def broadcast(text, kb_=None, kind=None, ref=None, skip=()):
    """text/kb_ can be a plain value, or a callable(viewer_id) for per-recipient content."""
    for m in members():
        if m in skip:
            continue
        t = text(m) if callable(text) else text
        k_ = kb_(m) if callable(kb_) else kb_
        r = send(m, t, k_)
        if r and kind:
            x("INSERT OR REPLACE INTO msg(kind,ref,member_id,chat_id,message_id) VALUES(?,?,?,?,?)",
              kind, ref, m, m, r["message_id"])


def refresh(kind, ref, text, kb_=None):
    """text/kb_ can be a plain value, or a callable(viewer_id) for per-recipient content."""
    for r in q("SELECT * FROM msg WHERE kind=? AND ref=?", kind, ref):
        t = text(r["member_id"]) if callable(text) else text
        k_ = kb_(r["member_id"]) if callable(kb_) else kb_
        edit(r["chat_id"], r["message_id"], t, k_)


# ------------------------------------------------------------ state helper --

def set_state(mid, key, data=None):
    x("INSERT INTO state(member_id,key,data) VALUES(?,?,?) "
      "ON CONFLICT(member_id) DO UPDATE SET key=?, data=?",
      mid, key, json.dumps(data or {}), key, json.dumps(data or {}))


def get_state(mid):
    r = q1("SELECT key,data FROM state WHERE member_id=?", mid)
    if not r:
        return None, {}
    return r["key"], json.loads(r["data"] or "{}")


def clear_state(mid):
    x("DELETE FROM state WHERE member_id=?", mid)


# ------------------------------------------------------------------ menus ---

# Text on these buttons is matched literally in on_message - keep the two in sync.
MENU_WEEK = "\U0001F4C5 Default free time"
MENU_BUSY = "\U0001F6AB I'm busy..."
MENU_BOARD = "\U0001F4CA Group board"
MENU_POLL = "\U0001F5F3 Start a poll"
MENU_ACT = "\U0001F3AE Activities"
MENU_STATS = "\U0001F4C8 Stats"
MENU_PROFILE = "\U0001F464 Profile"
MENU_ADMIN = "⚙️ Admin"


def reply_kb(uid):
    """Persistent keyboard shown under the text box, Telegram-native nav."""
    rows = [[MENU_WEEK, MENU_BUSY], [MENU_BOARD, MENU_POLL],
            [MENU_ACT, MENU_STATS], [MENU_PROFILE]]
    if uid == ADMIN_ID:
        rows.append([MENU_ADMIN])
    return {"keyboard": rows, "resize_keyboard": True}


def menu_kb(uid):
    """Small inline 'back to menu' button attached to specific screens."""
    return kb([("Back to menu", "m|menu")])


def show_menu(uid):
    send(uid, "What do you want to do? Use the buttons below the message box any time.",
         reply_kb(uid))


def start_onboarding_or_menu(uid):
    """Nickname -> status -> (default free time, first time on that status only) -> time zone."""
    clear_state(uid)
    r = q1("SELECT nickname,status,timezone FROM member WHERE id=?", uid)
    if not r or not r["nickname"]:
        set_state(uid, "profile_nick", {"then": "menu"})
        send(uid, "Let's set up your profile first.\nWhat nickname should we use for you?")
    elif not r["status"]:
        set_state(uid, "profile_status", {"then": "menu"})
        send(uid, "What's your usual status?", status_kb("pr|st"))
    elif not has_baseline(uid, r["status"]):
        set_state(uid, "baseline_pick", {"status": r["status"], "then": "menu"})
        send(uid, "First time on <b>%s</b>. " % esc(r["status"]) + baseline_pick_prompt(r["status"]),
             baseline_pick_kb(uid, r["status"]))
    elif not r["timezone"]:
        set_state(uid, "profile_tz", {"then": "menu"})
        send(uid, "One more thing - what's your time zone?", tz_kb("pr|tz"))
    else:
        show_menu(uid)


# ----------------------------------------------------------------- profile --

def status_kb(prefix):
    return kb(*[[(s, "%s|%s" % (prefix, s))] for s in STATUSES])


# offset (whole hours from UTC, as of standard time) -> the most popular city that
# keeps that time, and the IANA zone used to work out its *actual* current offset -
# cities that observe DST (Berlin, Santiago, Sydney, ...) shift a further hour for
# part of the year, and the zone is what tells us when.
TIMEZONES = [
    (-12, "Baker Island", "Etc/GMT+12"), (-11, "Pago Pago", "Pacific/Pago_Pago"),
    (-10, "Honolulu", "Pacific/Honolulu"), (-9, "Anchorage", "America/Anchorage"),
    (-8, "Los Angeles", "America/Los_Angeles"), (-7, "Denver", "America/Denver"),
    (-6, "Mexico City", "America/Mexico_City"), (-5, "New York", "America/New_York"),
    (-4, "Santiago", "America/Santiago"), (-3, "Buenos Aires", "America/Argentina/Buenos_Aires"),
    (-2, "South Georgia", "Atlantic/South_Georgia"), (-1, "Azores", "Atlantic/Azores"),
    (0, "London", "Europe/London"), (1, "Berlin", "Europe/Berlin"), (2, "Cairo", "Africa/Cairo"),
    (3, "Moscow", "Europe/Moscow"), (4, "Dubai", "Asia/Dubai"), (5, "Almaty", "Asia/Almaty"),
    (6, "Dhaka", "Asia/Dhaka"), (7, "Bangkok", "Asia/Bangkok"), (8, "Beijing", "Asia/Shanghai"),
    (9, "Tokyo", "Asia/Tokyo"), (10, "Sydney", "Australia/Sydney"), (11, "Noumea", "Pacific/Noumea"),
    (12, "Auckland", "Pacific/Auckland"), (13, "Nuku'alofa", "Pacific/Tongatapu"),
    (14, "Kiritimati", "Pacific/Kiritimati"),
]
TZ_CITY = {offset: city for offset, city, _ in TIMEZONES}
TZ_ZONE = {offset: zone for offset, _, zone in TIMEZONES}


def tz_label(offset):
    return "UTC %s%d:00, %s time" % ("+" if offset >= 0 else "-", abs(offset), TZ_CITY[offset])


def tz_kb(prefix):
    rows, row = [], []
    for offset, city, _ in TIMEZONES:
        row.append(("UTC%s%d %s" % ("+" if offset >= 0 else "-", abs(offset), city),
                    "%s|%d" % (prefix, offset)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return kb(*rows)


def profile_text(uid):
    r = q1("SELECT name,nickname,status,timezone FROM member WHERE id=?", uid)
    nick = esc(r["nickname"] or r["name"])
    status = esc(r["status"]) if r["status"] else "<i>not set</i>"
    tz = esc(tz_label(int(r["timezone"]))) if r["timezone"] not in (None, "") else "<i>not set</i>"
    dft = baseline_text(uid, r["status"]) if r["status"] else "<i>not set</i>"
    return "<b>%s</b>\nStatus: %s\nTime zone: %s\n\n<b>Default free time</b>\n%s" % (nick, status, tz, dft)


def profile_kb():
    return kb([("Edit profile", "pr|edit")], [("Edit default free time", "b|edit")])


# --------------------------------------------------------- baseline setup ---

def baseline_text(mid, status=None):
    status = status or current_status(mid)
    rows = q("SELECT weekday,start_min,end_min FROM baseline WHERE member_id=? AND status=? ORDER BY weekday",
             mid, status)
    if not rows:
        return "You haven't set default free time for this status yet."
    return "\n".join("<b>%s</b>  %s" % (WEEKDAYS[r["weekday"]], span(r["start_min"], r["end_min"]))
                     for r in rows)


def baseline_pick_kb(mid, status):
    """One row per weekday - each can carry its own hours, or none at all."""
    existing = {r["weekday"]: (r["start_min"], r["end_min"]) for r in
                q("SELECT weekday,start_min,end_min FROM baseline WHERE member_id=? AND status=?", mid, status)}
    rows = []
    for wd in range(7):
        if wd in existing:
            s, e = existing[wd]
            rows.append([("%s  %s" % (WEEKDAYS[wd], span(s, e)), "b|pick|%d" % wd),
                         ("clear", "b|clear|%d" % wd)])
        else:
            rows.append([("%s  not set" % WEEKDAYS[wd], "b|pick|%d" % wd)])
    rows.append([("Done", "b|done")])
    return kb(*rows)


def baseline_pick_prompt(status):
    return ("Tap a day to set its free hours for <b>%s</b> - each day can be different. "
            "Tap Done when finished." % esc(status))


def hour_kb(prefix, lo, hi):
    rows, row = [], []
    for h in range(lo, hi + 1):
        row.append(("%02d:00" % (h % 24), "%s|%d" % (prefix, h * 60)))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return kb(*rows)


# ----------------------------------------------------------- board render ---

def board(offset=0, viewer=None):
    ms = members()
    days = week_days(offset)
    days = [dd for dd in days if any(effective(m, dd) for m in ms)]
    hours = list(range(DAY_START, DAY_END, 60))
    head = "     " + "".join("%4s" % hhmm(h)[:2] for h in hours)
    lines = [head]
    for dd in days:
        counts = dict(slot_counts(dd, ms))
        # one symbol per gap BETWEEN two hour labels, not under either of them -
        # 24 labels give 23 gaps, hence the shorter, shifted row. Marked only
        # when EVERYONE overlaps that hour, not just anyone.
        cells = "".join("   ▲" if len(counts.get(h, [])) == len(ms) else "   -" for h in hours[:-1])
        lines.append("%-5s  %s" % (pretty(dd)[:5], cells))
    grid = "<pre>%s</pre>" % esc("\n".join(lines)) if len(lines) > 1 else ""

    cs = candidates(days, ms, min_players=len(ms))
    txt = "<b>%s - %s</b>  %d members\n%s\nhow many of us are free  <i>(grid: shared reference clock)</i>\n" % (
        pretty(days[0]) if days else "?", pretty(days[-1]) if days else "?", len(ms), grid)
    if cs:
        txt += "\n<b>Best windows</b>  <i>(your local time)</i>\n"
        for i, (dy, s, e, who) in enumerate(cs[:3], 1):
            ldy, ls, le = localize(dy, s, e, viewer) if viewer else (dy, s, e)
            txt += "%d. %s, %s - %s\n" % (i, pretty(ldy), span(ls, le), names(who))
    else:
        txt += "\nNo window yet where anyone's free for %d minutes.\n" % MIN_SESSION
    st = [name(m) for m in ms if stale(m)]
    if st:
        txt += "\n<i>Old data: %s</i>" % ", ".join(st)
    rows = [[("Start a poll", "p|new")], [("I'm busy this week", "u|open|0")],
            [("Next week", "w|show|1") if offset == 0 else ("This week", "w|show|0")]]
    return txt, kb(*rows)


# ------------------------------------------------------------------ polls ---

def open_poll():
    return q1("SELECT * FROM poll WHERE status='open'")


def poll_text(pid, viewer):
    p = q1("SELECT * FROM poll WHERE id=?", pid)
    txt = "<b>When?</b>  started by %s  <i>(times in your local zone)</i>\n" % name(p["created_by"])
    for o in q("SELECT * FROM poll_option WHERE poll_id=? ORDER BY id", pid):
        h = q("SELECT member_id FROM vote WHERE option_id=? AND commitment='hard'", o["id"])
        s = q("SELECT member_id FROM vote WHERE option_id=? AND commitment='soft'", o["id"])
        ldy, ls, le = localize(o["day"], o["start_min"], o["end_min"], viewer)
        txt += "\n%s, %s\n   in: %d   maybe: %d" % (
            pretty(ldy), span(ls, le), len(h), len(s))
    gv = {}
    for r in q("SELECT game, COUNT(*) c FROM game_vote WHERE poll_id=? GROUP BY game", pid):
        gv[r["game"]] = r["c"]
    if gv:
        txt += "\n\n<b>Games</b>  " + "  ".join("%s %d" % (g, c) for g, c in
                                                sorted(gv.items(), key=lambda i: -i[1]))
    voted = {r["member_id"] for r in q(
        "SELECT DISTINCT member_id FROM vote v JOIN poll_option o ON o.id=v.option_id "
        "WHERE o.poll_id=?", pid)}
    waiting = [name(m) for m in members() if m not in voted]
    txt += "\n\n<i>closes %s%s</i>" % (
        localize_dt(parse_iso(p["closes_at"]), viewer).strftime("%a %H:%M"),
        (" - waiting on " + ", ".join(waiting)) if waiting else " - everyone voted")
    return txt


def poll_kb(pid, viewer):
    rows = []
    for o in q("SELECT * FROM poll_option WHERE poll_id=? ORDER BY id", pid):
        ldy, ls, le = localize(o["day"], o["start_min"], o["end_min"], viewer)
        label = "%s %s" % (pretty(ldy)[:5], span(ls, le))
        rows.append([("%s  I'm in" % label, "p|v|%d|hard" % o["id"]),
                     ("maybe", "p|v|%d|soft" % o["id"])])
    grow = []
    for g in GAMES:
        grow.append((g, "p|g|%d|%s" % (pid, g)))
    for i in range(0, len(grow), 2):
        rows.append(grow[i:i + 2])
    rows.append([("Close it now", "p|close|%d" % pid)])
    return kb(*rows)


def create_poll(by):
    if open_poll():
        return None
    ms = members()
    days = [dd for dd in week_days(0) + week_days(1) if d(dd) >= date.today()]
    cs = candidates(days, ms)
    if not cs:
        return None
    pid = x("INSERT INTO poll(created_by,created_at,closes_at) VALUES(?,?,?)",
            by, iso(now()), iso(now() + timedelta(hours=POLL_HOURS)))
    for dy, s, e, who in cs[:4]:
        x("INSERT INTO poll_option(poll_id,day,start_min,end_min) VALUES(?,?,?,?)", pid, dy, s, e)
    x("INSERT INTO job(kind,run_at,ref) VALUES('close_poll',?,?)",
      iso(now() + timedelta(hours=POLL_HOURS)), pid)
    broadcast(lambda uid: poll_text(pid, uid), lambda uid: poll_kb(pid, uid), kind="poll", ref=pid)
    return pid


def close_poll(pid):
    p = q1("SELECT * FROM poll WHERE id=? AND status='open'", pid)
    if not p:
        return
    x("UPDATE poll SET status='closed' WHERE id=?", pid)
    best, best_key = None, None
    for o in q("SELECT * FROM poll_option WHERE poll_id=?", pid):
        h = q("SELECT member_id FROM vote WHERE option_id=? AND commitment='hard'", o["id"])
        s = q("SELECT member_id FROM vote WHERE option_id=? AND commitment='soft'", o["id"])
        key = (len(h), len(s), -(o["start_min"]))
        if len(h) >= MIN_PLAYERS and (best_key is None or key > best_key):
            best, best_key = (o, h, s), key
    refresh("poll", pid, lambda uid: poll_text(pid, uid), kb())
    if not best:
        broadcast("Poll closed with nothing that worked - no window got %d committed. "
                  "Try again once a few of us update the week." % MIN_PLAYERS)
        return
    o, hard, soft = best
    gv = q("SELECT game, COUNT(*) c FROM game_vote WHERE poll_id=? GROUP BY game ORDER BY c DESC", pid)
    game = gv[0]["game"] if gv else "whatever we feel like"
    sid = x("INSERT INTO sess(day,start_min,end_min,game,status,agreed_at,origin) "
            "VALUES(?,?,?,?,'agreed',?,'poll')",
            o["day"], o["start_min"], o["end_min"], game, iso(now()))
    for r in hard:
        x("INSERT INTO part(sess_id,member_id,commitment) VALUES(?,?,'hard')", sid, r["member_id"])
    for r in soft:
        x("INSERT OR IGNORE INTO part(sess_id,member_id,commitment) VALUES(?,?,'soft')",
          sid, r["member_id"])
    schedule_session_jobs(sid)
    broadcast(lambda uid: sess_text(sid, uid), sess_kb(sid), kind="sess", ref=sid)


# --------------------------------------------------------------- sessions ---

def sess_text(sid, viewer):
    s = q1("SELECT * FROM sess WHERE id=?", sid)
    hard = [r["member_id"] for r in q(
        "SELECT member_id FROM part WHERE sess_id=? AND commitment='hard' AND confirm!='out'", sid)]
    soft = [r["member_id"] for r in q(
        "SELECT member_id FROM part WHERE sess_id=? AND commitment='soft' AND confirm!='out'", sid)]
    out = [r for r in q("SELECT member_id,reason FROM part WHERE sess_id=? AND confirm='out'", sid)]
    head = {"agreed": "Agreed", "at_risk": "At risk", "cancelled": "Called off",
            "played": "Played", "missed": "Never happened"}[s["status"]]
    ldy, ls, le = localize(s["day"], s["start_min"], s["end_min"], viewer)
    txt = "<b>%s - %s, %s</b>\n%s\n\nIn: %s" % (
        head, pretty(ldy), span(ls, le), esc(s["game"]), names(hard))
    if soft:
        txt += "\nProbably: %s" % names(soft)
    for r in out:
        txt += "\n<i>%s dropped out%s</i>" % (
            name(r["member_id"]), (" - " + esc(r["reason"])) if r["reason"] else "")
    if s["status"] == "at_risk":
        txt += "\n\n<i>That's under %d committed. Keep it or call it off?</i>" % MIN_PLAYERS
    return txt


def sess_kb(sid):
    s = q1("SELECT * FROM sess WHERE id=?", sid)
    if s["status"] in ("cancelled", "played", "missed"):
        return kb()
    rows = [[("My plans changed", "e|out|%d" % sid), ("Count me in", "e|in|%d" % sid)]]
    if s["status"] == "at_risk":
        rows.append([("Keep the slot", "e|keep|%d" % sid), ("Call it off", "e|kill|%d" % sid)])
    return kb(*rows)


def schedule_session_jobs(sid):
    s = q1("SELECT * FROM sess WHERE id=?", sid)
    start = datetime.combine(d(s["day"]), datetime.min.time()) + timedelta(minutes=s["start_min"])
    rc = min(now() + timedelta(hours=RECONFIRM_HOURS), start - timedelta(hours=12))
    for kind, when in (("reconfirm", rc), ("remind24", start - timedelta(hours=24)),
                       ("remind1", start - timedelta(hours=1)), ("wrap", start + timedelta(hours=5))):
        if when > now():
            x("INSERT INTO job(kind,run_at,ref) VALUES(?,?,?)", kind, iso(when), sid)


def recheck(sid):
    s = q1("SELECT * FROM sess WHERE id=?", sid)
    if s["status"] not in ("agreed", "at_risk"):
        return
    n = len(q("SELECT 1 FROM part WHERE sess_id=? AND commitment='hard' AND confirm!='out'", sid))
    new = "at_risk" if n < MIN_PLAYERS else "agreed"
    if new != s["status"]:
        x("UPDATE sess SET status=? WHERE id=?", new, sid)
    refresh("sess", sid, lambda uid: sess_text(sid, uid), sess_kb(sid))


# ------------------------------------------------------------- activities ---

def act_text(aid, viewer):
    a = q1("SELECT * FROM activity WHERE id=?", aid)
    hard = [r["member_id"] for r in q(
        "SELECT member_id FROM act_member WHERE act_id=? AND commitment='hard'", aid)]
    soft = [r["member_id"] for r in q(
        "SELECT member_id FROM act_member WHERE act_id=? AND commitment='soft'", aid)]
    ldy, ls, le = localize(a["day"], a["start_min"], a["end_min"], viewer)
    txt = "<b>%s</b>\n%s, %s%s\nby %s\n\nIn: %s" % (
        esc(a["title"]), pretty(ldy), span(ls, le),
        "  (weekly)" if a["weekly"] else "", name(a["creator_id"]), names(hard))
    if soft:
        txt += "\nProbably: %s" % names(soft)
    if a["status"] == "cancelled":
        txt += "\n\n<i>cancelled</i>"
    return txt


def act_kb(aid):
    a = q1("SELECT * FROM activity WHERE id=?", aid)
    if a["status"] == "cancelled":
        return kb()
    rows = [[("I'm in", "a|j|%d|hard" % aid), ("Probably", "a|j|%d|soft" % aid),
             ("Out", "a|j|%d|no" % aid)]]
    if a["weekly"]:
        rows.append([("Leave the series", "a|series|%d" % aid)])
    rows.append([("Cancel it", "a|kill|%d" % aid)])
    return kb(*rows)


def roll_weekly(aid):
    a = q1("SELECT * FROM activity WHERE id=?", aid)
    if not a or not a["weekly"] or a["status"] == "cancelled":
        return
    nxt = (d(a["day"]) + timedelta(days=7)).isoformat()
    nid = x("INSERT INTO activity(creator_id,title,game,day,start_min,end_min,weekly) "
            "VALUES(?,?,?,?,?,?,1)", a["creator_id"], a["title"], a["game"], nxt,
            a["start_min"], a["end_min"])
    for r in q("SELECT member_id,commitment FROM act_member WHERE act_id=?", aid):
        x("INSERT INTO act_member(act_id,member_id,commitment) VALUES(?,?,?)",
          nid, r["member_id"], r["commitment"])
    broadcast(lambda uid: act_text(nid, uid), act_kb(nid), kind="act", ref=nid)


# ------------------------------------------------------------------ stats ---

def stats_text():
    played = q("SELECT * FROM sess WHERE status='played' ORDER BY day")
    total = len(played)
    recent = len([s for s in played if (date.today() - d(s["day"])).days <= 30])
    txt = "<b>Since we started</b>\nsessions played: %d\nlast 30 days: %d\n" % (total, recent)
    if played:
        games = {}
        for s in played:
            games[s["game"]] = games.get(s["game"], 0) + 1
        top = sorted(games.items(), key=lambda i: -i[1])[0]
        txt += "most played: %s (%d)\n" % (esc(top[0]), top[1])
        weeks = sorted({d(s["day"]).isocalendar()[:2] for s in played})
        best = run = 1
        for i in range(1, len(weeks)):
            prev = date.fromisocalendar(weeks[i - 1][0], weeks[i - 1][1], 1)
            cur = date.fromisocalendar(weeks[i][0], weeks[i][1], 1)
            run = run + 1 if (cur - prev).days == 7 else 1
            best = max(best, run)
        txt += "longest run: %d weeks\n" % best
        txt += "\n<b>Turned up when they said they would</b>\n"
        for m in members():
            said = len(q("SELECT 1 FROM part p JOIN sess s ON s.id=p.sess_id "
                         "WHERE p.member_id=? AND p.commitment='hard'", m))
            did = len(q("SELECT 1 FROM part p JOIN sess s ON s.id=p.sess_id "
                        "WHERE p.member_id=? AND p.commitment='hard' AND p.confirm!='out' "
                        "AND s.status='played'", m))
            if said:
                txt += "%s %d%%\n" % (name(m), round(100 * did / said))
    starters = q("SELECT created_by c, COUNT(*) n FROM poll GROUP BY created_by ORDER BY n DESC")
    if starters:
        txt += "\nmost polls started: %s (%d)" % (name(starters[0]["c"]), starters[0]["n"])
    return txt


# ------------------------------------------------------------- scheduling ---

def run_jobs():
    for j in q("SELECT * FROM job WHERE done=0 AND run_at<=?", iso(now())):
        try:
            do_job(j)
        except Exception:
            log("job %s failed:\n%s" % (j["kind"], traceback.format_exc()))
        x("UPDATE job SET done=1 WHERE id=?", j["id"])


def do_job(j):
    k, ref = j["kind"], j["ref"]
    if k == "close_poll":
        close_poll(ref)
    elif k == "reconfirm":
        s = q1("SELECT * FROM sess WHERE id=?", ref)
        if s and s["status"] in ("agreed", "at_risk"):
            for r in q("SELECT member_id FROM part WHERE sess_id=? AND confirm='pending'", ref):
                ldy, ls, le = localize(s["day"], s["start_min"], s["end_min"], r["member_id"])
                send(r["member_id"],
                     "We agreed on <b>%s, %s</b> - %s.\nStill good for you?" % (
                         pretty(ldy), span(ls, le), esc(s["game"])),
                     kb([("Still good", "e|ok|%d" % ref), ("Something changed", "e|out|%d" % ref)]))
    elif k in ("remind24", "remind1"):
        s = q1("SELECT * FROM sess WHERE id=?", ref)
        if s and s["status"] in ("agreed", "at_risk"):
            when = "Tomorrow" if k == "remind24" else "In an hour"
            for r in q("SELECT member_id FROM part WHERE sess_id=? AND confirm!='out'", ref):
                ldy, ls, le = localize(s["day"], s["start_min"], s["end_min"], r["member_id"])
                send(r["member_id"], "%s: <b>%s</b>, %s - %s." % (
                    when, esc(s["game"]), pretty(ldy), span(ls, le)))
    elif k == "wrap":
        s = q1("SELECT * FROM sess WHERE id=?", ref)
        if s and s["status"] in ("agreed", "at_risk"):
            x("UPDATE sess SET status='played' WHERE id=?", ref)
            refresh("sess", ref, lambda uid: sess_text(ref, uid), kb())
    elif k == "roll":
        roll_weekly(ref)


def daily_suggestion():
    today = date.today().isoformat()
    if getkv("suggested") == today or now().hour < SUGGEST_HOUR:
        return
    setkv("suggested", today)
    if open_poll():
        return
    ms = members()
    days = [dd for dd in week_days(0) + week_days(1) if d(dd) >= date.today()]
    cs = candidates(days, ms)
    for m in ms:
        if not onboarding_complete(m):
            continue
        if q1("SELECT 1 FROM member WHERE id=? AND muted=1", m):
            continue
        if q1("SELECT 1 FROM sent WHERE member_id=? AND day=?", m, today):
            continue
        lines = []
        if cs:
            dy, s, e, who = cs[0]
            ldy, ls, le = localize(dy, s, e, m)
            lines.append("%d of us are free %s, %s. Worth a poll?" % (
                len(who), pretty(ldy), span(ls, le)))
        if stale(m):
            lines.append("Your week is looking old - anything changed?")
        if not lines:
            continue
        x("INSERT OR IGNORE INTO sent(member_id,day) VALUES(?,?)", m, today)
        send(m, "\n\n".join(lines), kb([("Start a poll", "p|new"), ("I'm busy...", "u|open|0")]))


# --------------------------------------------------------- shared views -----
# Built once, used both when an existing message is edited in place (inline
# keyboard callbacks) and when sent fresh (reply-keyboard button, /commands).

def busy_open_view(uid, off):
    rows = []
    status = current_status(uid)
    for dd in week_days(off):
        if d(dd) < date.today() or not effective(uid, dd):
            if d(dd) < date.today():
                continue
            if not q("SELECT 1 FROM baseline WHERE member_id=? AND status=? AND weekday=?",
                     uid, status, d(dd).weekday()):
                continue
        busy = q("SELECT 1 FROM busy WHERE member_id=? AND day=?", uid, dd)
        rows.append([("%s  %s" % (pretty(dd), "BUSY" if busy else "free"), "u|all|%s" % dd),
                     ("part of it", "u|part|%s" % dd)])
    if not rows:
        return "Nothing scheduled that week", None
    rows.append([("Next week", "u|open|1") if off == 0 else ("This week", "u|open|0")])
    rows.append([("Done", "m|menu")])
    return "Tap a day you <b>can't</b> make. Everything else stays free.", kb(*rows)


def activities_list_view(viewer):
    rows = []
    for a in q("SELECT * FROM activity WHERE status='open' AND day>=? ORDER BY day",
               date.today().isoformat()):
        ldy, _, _ = localize(a["day"], a["start_min"], a["end_min"], viewer)
        rows.append([("%s - %s" % (pretty(ldy), a["title"][:24]), "a|show|%d" % a["id"])])
    txt = "Activities" if rows else "Nothing planned yet."
    rows.append([("Create one", "a|new")])
    return txt, kb(*rows)


def admin_panel_view():
    ms = q("SELECT * FROM member")
    txt = "<b>Admin</b>  %d members\n\n" % len(ms)
    txt += "\n".join("%s  <code>%d</code>%s" % (
        m["name"], m["id"], "  (no status/default free time set)" if not has_baseline(m["id"]) else "")
        for m in ms)
    txt += "\n\n<i>/add 123456789 Name  ·  /remove 123456789</i>"
    p = open_poll()
    rows = [[("Close the open poll", "p|close|%d" % p["id"])]] if p else []
    return txt, kb(*rows)


# --------------------------------------------------------------- handlers ---

def on_message(msg):
    uid = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    if not is_member(uid):
        if uid == ADMIN_ID and text.startswith("/start"):
            x("INSERT OR IGNORE INTO member(id,name,joined_at) VALUES(?,?,?)",
              uid, msg["from"].get("first_name", "admin"), iso(now()))
            send(uid, "You're in as admin. Add the others with /add &lt;id&gt; &lt;name&gt;.")
            start_onboarding_or_menu(uid)
        return  # everyone else: silence

    key, data = get_state(uid)
    if key == "profile_nick" and text and not text.startswith("/"):
        x("UPDATE member SET nickname=? WHERE id=?", text[:30], uid)
        set_state(uid, "profile_status", {"then": data.get("then", "menu")})
        send(uid, "What's your usual status?", status_kb("pr|st"))
        return
    if key == "act_title" and text and not text.startswith("/"):
        data["title"] = text[:60]
        set_state(uid, "act_day", data)
        rows = [[(pretty(dd), "a|day|%s" % dd)] for dd in
                [(date.today() + timedelta(days=i)).isoformat() for i in range(1, 8)]]
        send(uid, "Which day?", kb(*rows))
        return

    if text.startswith("/add") and uid == ADMIN_ID:
        m = re.match(r"/add\s+(\d+)\s*(.*)", text)
        if m:
            x("INSERT OR IGNORE INTO member(id,name,joined_at) VALUES(?,?,?)",
              int(m.group(1)), (m.group(2) or "friend")[:30], iso(now()))
            send(uid, "Added %s. Tell them to send /start." % (m.group(2) or m.group(1)))
        else:
            send(uid, "Use: /add 123456789 Marat")
        return
    if text.startswith("/remove") and uid == ADMIN_ID:
        m = re.match(r"/remove\s+(\d+)", text)
        if m:
            x("DELETE FROM member WHERE id=?", int(m.group(1)))
            send(uid, "Removed.")
        return
    if text.startswith("/start") or text.startswith("/menu"):
        start_onboarding_or_menu(uid)
        return
    if text.startswith("/profile"):
        clear_state(uid)
        send(uid, profile_text(uid), profile_kb())
        return
    if text.startswith("/week"):
        t, k = board(0, uid)
        send(uid, t, k)
        return
    if text.startswith("/stats"):
        send(uid, stats_text())
        return

    if text == MENU_WEEK:
        clear_state(uid)
        status = current_status(uid)
        set_state(uid, "baseline_pick", {"status": status})
        send(uid, baseline_pick_prompt(status), baseline_pick_kb(uid, status))
        return
    if text == MENU_BUSY:
        clear_state(uid)
        txt, k = busy_open_view(uid, 0)
        send(uid, txt, k)
        return
    if text == MENU_BOARD:
        clear_state(uid)
        t, k = board(0, uid)
        send(uid, t, k)
        return
    if text == MENU_POLL:
        clear_state(uid)
        if open_poll():
            send(uid, "There's already a poll open")
        else:
            pid = create_poll(uid)
            send(uid, "Poll sent to everyone" if pid else "No window works right now")
        return
    if text == MENU_ACT:
        clear_state(uid)
        txt, k = activities_list_view(uid)
        send(uid, txt, k)
        return
    if text == MENU_STATS:
        clear_state(uid)
        send(uid, stats_text())
        return
    if text == MENU_PROFILE:
        clear_state(uid)
        send(uid, profile_text(uid), profile_kb())
        return
    if text == MENU_ADMIN and uid == ADMIN_ID:
        clear_state(uid)
        txt, k = admin_panel_view()
        send(uid, txt, k)
        return

    show_menu(uid)


def on_callback(cb):
    uid = cb["from"]["id"]
    cid = cb["id"]
    if not is_member(uid):
        answer(cid)
        return
    parts = cb["data"].split("|")
    dom, act = parts[0], parts[1]
    args = parts[2:]
    chat = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]

    # ---- default free time (per status, per weekday)
    if dom == "b":
        key, data = get_state(uid)
        if act == "edit":
            status = current_status(uid)
            set_state(uid, "baseline_pick", {"status": status})
            edit(chat, mid, baseline_pick_prompt(status), baseline_pick_kb(uid, status))
        elif act == "pick":
            status = data.get("status") or current_status(uid)
            wd = int(args[0])
            set_state(uid, "baseline_hour", {"status": status, "then": data.get("then"), "weekday": wd})
            edit(chat, mid, "%s - free <b>from</b> when?" % WEEKDAYS[wd], hour_kb("b|from", 0, 23))
        elif act == "clear":
            status = data.get("status") or current_status(uid)
            wd = int(args[0])
            x("DELETE FROM baseline WHERE member_id=? AND status=? AND weekday=?", uid, status, wd)
            set_state(uid, "baseline_pick", {"status": status, "then": data.get("then")})
            edit(chat, mid, baseline_pick_prompt(status), baseline_pick_kb(uid, status))
        elif act == "from":
            data["from"] = int(args[0])
            set_state(uid, "baseline_hour", data)
            edit(chat, mid, "And <b>until</b>?", hour_kb("b|to", data["from"] // 60 + 1, 24))
        elif act == "to":
            status, wd, lo, hi = data["status"], data["weekday"], data["from"], int(args[0])
            x("DELETE FROM baseline WHERE member_id=? AND status=? AND weekday=?", uid, status, wd)
            x("INSERT INTO baseline(member_id,status,weekday,start_min,end_min) VALUES(?,?,?,?,?)",
              uid, status, wd, lo, hi)
            touch(uid)
            set_state(uid, "baseline_pick", {"status": status, "then": data.get("then")})
            edit(chat, mid, "Saved %s %s.\nTap another day, or Done." % (WEEKDAYS[wd], span(lo, hi)),
                 baseline_pick_kb(uid, status))
        elif act == "done":
            status = data.get("status") or current_status(uid)
            if not has_baseline(uid, status):
                answer(cid, "Set at least one day first")
                return
            then = data.get("then")
            if then:
                # part of onboarding / an edit-profile chain - carry on to time zone
                set_state(uid, "profile_tz", {"then": then})
                edit(chat, mid, "Default free time saved for <b>%s</b>:\n\n%s\n\nAnd your time zone?" %
                     (esc(status), baseline_text(uid, status)), tz_kb("pr|tz"))
            else:
                clear_state(uid)
                edit(chat, mid, "Saved. Default free time for <b>%s</b>:\n\n%s\n\n"
                                "From now on just tell me when you're <b>busy</b>." %
                     (esc(status), baseline_text(uid, status)), menu_kb(uid))
        answer(cid)
        return

    # ---- busy / unavailability
    if dom == "u":
        if act == "open":
            off = int(args[0])
            txt, k = busy_open_view(uid, off)
            if k is None:
                answer(cid, txt)
                return
            edit(chat, mid, txt, k)
        elif act == "all":
            dd = args[0]
            if q("SELECT 1 FROM busy WHERE member_id=? AND day=?", uid, dd):
                x("DELETE FROM busy WHERE member_id=? AND day=?", uid, dd)
                answer(cid, "%s free again" % pretty(dd))
            else:
                x("INSERT INTO busy(member_id,day,start_min,end_min) VALUES(?,?,?,?)",
                  uid, dd, DAY_START, DAY_END)
                answer(cid, "%s marked busy" % pretty(dd))
            touch(uid)
            on_callback({**cb, "data": "u|open|0"})
            return
        elif act == "part":
            set_state(uid, "busy_from", {"day": args[0]})
            edit(chat, mid, "%s - busy <b>from</b>?" % pretty(args[0]), hour_kb("u|pf", 0, 23))
        elif act == "pf":
            key, data = get_state(uid)
            data["from"] = int(args[0])
            set_state(uid, "busy_to", data)
            edit(chat, mid, "busy <b>until</b>?", hour_kb("u|pt", data["from"] // 60 + 1, 24))
        elif act == "pt":
            key, data = get_state(uid)
            x("INSERT INTO busy(member_id,day,start_min,end_min) VALUES(?,?,?,?)",
              uid, data["day"], data["from"], int(args[0]))
            touch(uid)
            clear_state(uid)
            left = effective(uid, data["day"])
            edit(chat, mid, "Noted: busy %s on %s.\n%s" % (
                span(data["from"], int(args[0])), pretty(data["day"]),
                ("Still free " + ", ".join(span(s, e) for s, e in left)) if left
                else "That's you out for the evening."), menu_kb(uid))
        answer(cid)
        return

    # ---- board
    if dom == "w":
        t, k = board(int(args[0]), uid)
        edit(chat, mid, t, k)
        answer(cid)
        return

    # ---- polls
    if dom == "p":
        if act == "new":
            if open_poll():
                answer(cid, "There's already a poll open")
                return
            pid = create_poll(uid)
            answer(cid, "Poll sent to everyone" if pid else "No window works right now")
        elif act == "v":
            oid, com = int(args[0]), args[1]
            o = q1("SELECT * FROM poll_option WHERE id=?", oid)
            if not o or q1("SELECT status FROM poll WHERE id=?", o["poll_id"])["status"] != "open":
                answer(cid, "That poll is closed")
                return
            x("INSERT INTO vote(option_id,member_id,commitment) VALUES(?,?,?) "
              "ON CONFLICT(option_id,member_id) DO UPDATE SET commitment=?", oid, uid, com, com)
            refresh("poll", o["poll_id"], lambda v: poll_text(o["poll_id"], v),
                     lambda v: poll_kb(o["poll_id"], v))
            answer(cid, "Counted as %s" % ("in" if com == "hard" else "maybe"))
        elif act == "g":
            pid, game = int(args[0]), args[1]
            if q1("SELECT 1 FROM game_vote WHERE poll_id=? AND member_id=? AND game=?", pid, uid, game):
                x("DELETE FROM game_vote WHERE poll_id=? AND member_id=? AND game=?", pid, uid, game)
            else:
                x("INSERT INTO game_vote(poll_id,member_id,game) VALUES(?,?,?)", pid, uid, game)
            refresh("poll", pid, lambda v: poll_text(pid, v), lambda v: poll_kb(pid, v))
            answer(cid)
        elif act == "close":
            close_poll(int(args[0]))
            answer(cid, "Closed")
        return

    # ---- sessions
    if dom == "e":
        sid = int(args[0])
        s = q1("SELECT * FROM sess WHERE id=?", sid)
        if not s:
            answer(cid, "Gone")
            return
        if act == "ok":
            x("UPDATE part SET confirm='confirmed' WHERE sess_id=? AND member_id=?", sid, uid)
            answer(cid, "See you then")
            ldy, ls, le = localize(s["day"], s["start_min"], s["end_min"], uid)
            edit(chat, mid, "Confirmed - %s, %s." % (pretty(ldy), span(ls, le)))
        elif act == "out":
            x("INSERT INTO part(sess_id,member_id,commitment,confirm) VALUES(?,?,'hard','out') "
              "ON CONFLICT(sess_id,member_id) DO UPDATE SET confirm='out'", sid, uid)
            for r in q("SELECT member_id FROM part WHERE sess_id=? AND confirm!='out'", sid):
                ldy, ls, le = localize(s["day"], s["start_min"], s["end_min"], r["member_id"])
                send(r["member_id"], "%s is out of %s, %s." % (name(uid), pretty(ldy), span(ls, le)))
            recheck(sid)
            answer(cid, "Taken off the list")
        elif act == "in":
            x("INSERT INTO part(sess_id,member_id,commitment,confirm) VALUES(?,?,'hard','confirmed') "
              "ON CONFLICT(sess_id,member_id) DO UPDATE SET confirm='confirmed', commitment='hard'",
              sid, uid)
            recheck(sid)
            answer(cid, "You're in")
        elif act == "keep":
            x("UPDATE sess SET status='agreed' WHERE id=?", sid)
            refresh("sess", sid, lambda v: sess_text(sid, v), sess_kb(sid))
            answer(cid, "Kept")
        elif act == "kill":
            x("UPDATE sess SET status='cancelled' WHERE id=?", sid)
            x("UPDATE job SET done=1 WHERE ref=? AND kind IN ('reconfirm','remind24','remind1','wrap')", sid)
            refresh("sess", sid, lambda v: sess_text(sid, v), kb())
            answer(cid, "Called off")
        return

    # ---- activities
    if dom == "a":
        if act == "list":
            txt, k = activities_list_view(uid)
            edit(chat, mid, txt, k)
        elif act == "new":
            set_state(uid, "act_title", {})
            edit(chat, mid, "Send me a name for it.\n<i>Example: Late-night Deep Rock run</i>")
        elif act == "day":
            key, data = get_state(uid)
            data["day"] = args[0]
            set_state(uid, "act_from", data)
            edit(chat, mid, "Starting at?", hour_kb("a|from", 0, 23))
        elif act == "from":
            key, data = get_state(uid)
            data["from"] = int(args[0])
            set_state(uid, "act_to", data)
            edit(chat, mid, "Until?", hour_kb("a|to", data["from"] // 60 + 1, 24))
        elif act == "to":
            key, data = get_state(uid)
            data["to"] = int(args[0])
            set_state(uid, "act_rep", data)
            edit(chat, mid, "Every week, or just this once?",
                 kb([("Every week", "a|make|1"), ("Just once", "a|make|0")]))
        elif act == "make":
            key, data = get_state(uid)
            aid = x("INSERT INTO activity(creator_id,title,day,start_min,end_min,weekly) "
                    "VALUES(?,?,?,?,?,?)", uid, data["title"], data["day"],
                    data["from"], data["to"], int(args[0]))
            x("INSERT INTO act_member(act_id,member_id,commitment) VALUES(?,?,'hard')", aid, uid)
            if int(args[0]):
                start = datetime.combine(d(data["day"]), datetime.min.time()) + timedelta(hours=5)
                x("INSERT INTO job(kind,run_at,ref) VALUES('roll',?,?)", iso(start + timedelta(days=1)), aid)
            clear_state(uid)
            edit(chat, mid, "Created. Everyone can see it now.", menu_kb(uid))
            broadcast(lambda v: act_text(aid, v), act_kb(aid), kind="act", ref=aid, skip=(uid,))
            send(uid, act_text(aid, uid), act_kb(aid))
        elif act == "show":
            aid = int(args[0])
            edit(chat, mid, act_text(aid, uid), act_kb(aid))
        elif act == "j":
            aid, com = int(args[0]), args[1]
            if com == "no":
                x("DELETE FROM act_member WHERE act_id=? AND member_id=?", aid, uid)
            else:
                x("INSERT INTO act_member(act_id,member_id,commitment) VALUES(?,?,?) "
                  "ON CONFLICT(act_id,member_id) DO UPDATE SET commitment=?", aid, uid, com, com)
            refresh("act", aid, lambda v: act_text(aid, v), act_kb(aid))
            answer(cid, {"hard": "You're in", "soft": "Down as probably", "no": "Out"}[com])
            return
        elif act == "series":
            aid = int(args[0])
            a = q1("SELECT * FROM activity WHERE id=?", aid)
            x("DELETE FROM act_member WHERE act_id IN "
              "(SELECT id FROM activity WHERE title=? AND weekly=1) AND member_id=?", a["title"], uid)
            answer(cid, "Out of the whole series")
            refresh("act", aid, lambda v: act_text(aid, v), act_kb(aid))
            return
        elif act == "kill":
            aid = int(args[0])
            a = q1("SELECT * FROM activity WHERE id=?", aid)
            if uid not in (a["creator_id"], ADMIN_ID):
                answer(cid, "Only %s can cancel it" % name(a["creator_id"]))
                return
            x("UPDATE activity SET status='cancelled' WHERE id=?", aid)
            x("UPDATE job SET done=1 WHERE kind='roll' AND ref=?", aid)
            refresh("act", aid, lambda v: act_text(aid, v), kb())
            answer(cid, "Cancelled")
            return
        answer(cid)
        return

    # ---- profile
    if dom == "pr":
        if act == "show":
            edit(chat, mid, profile_text(uid), profile_kb())
        elif act == "edit":
            set_state(uid, "profile_nick", {"then": "profile"})
            edit(chat, mid, "New nickname?")
        elif act == "st":
            status = args[0]
            key, data = get_state(uid)
            then = data.get("then", "menu") if key == "profile_status" else "menu"
            x("UPDATE member SET status=? WHERE id=?", status, uid)
            if not has_baseline(uid, status):
                set_state(uid, "baseline_pick", {"status": status, "then": then})
                edit(chat, mid, "First time on <b>%s</b>. " % esc(status) + baseline_pick_prompt(status),
                     baseline_pick_kb(uid, status))
            else:
                set_state(uid, "profile_tz", {"then": then})
                edit(chat, mid, "Status set to <b>%s</b>.\nAnd your time zone?" % esc(status), tz_kb("pr|tz"))
        elif act == "tz":
            key, data = get_state(uid)
            then = data.get("then", "menu") if key == "profile_tz" else "menu"
            x("UPDATE member SET timezone=? WHERE id=?", args[0], uid)
            clear_state(uid)
            edit(chat, mid, "Profile saved:\n\n%s" % profile_text(uid))
            if then == "profile":
                send(uid, profile_text(uid), profile_kb())
            else:
                show_menu(uid)
        answer(cid)
        return

    # ---- stats / menu / admin
    if dom == "s":
        edit(chat, mid, stats_text(), menu_kb(uid))
    elif dom == "m":
        edit(chat, mid, "Use the buttons below the message box any time.", kb())
    elif dom == "x" and uid == ADMIN_ID:
        if act == "panel":
            txt, k = admin_panel_view()
            edit(chat, mid, txt, k)
    answer(cid)


# ------------------------------------------------------------------- main ---

def register_commands():
    """The '/' tab next to the emoji icon: Telegram's native command menu."""
    api("setMyCommands", commands=[
        {"command": "menu", "description": "\U0001F3E0 Menu"},
        {"command": "profile", "description": "\U0001F464 Profile"},
        {"command": "week", "description": "\U0001F4C5 Group board"},
        {"command": "stats", "description": "\U0001F4C8 Stats"},
    ])
    api("setChatMenuButton", menu_button={"type": "commands"})


def main():
    if not TOKEN or not ADMIN_ID:
        print("Set BOT_TOKEN and ADMIN_ID first. See the top of this file.")
        sys.exit(1)
    opendb()
    me = api("getMe")
    register_commands()
    log("started as @%s, db=%s" % (me["username"] if me else "?", DB_PATH))
    offset = int(getkv("offset", 0))
    last_tick = 0
    while True:
        try:
            ups = api("getUpdates", offset=offset, timeout=25,
                      allowed_updates=["message", "callback_query"]) or []
            for u in ups:
                offset = u["update_id"] + 1
                setkv("offset", offset)
                try:
                    if "message" in u:
                        on_message(u["message"])
                    elif "callback_query" in u:
                        on_callback(u["callback_query"])
                except Exception:
                    log("handler failed:\n%s" % traceback.format_exc())
            if time.time() - last_tick > 60:
                last_tick = time.time()
                run_jobs()
                daily_suggestion()
        except KeyboardInterrupt:
            log("bye")
            return
        except Exception:
            log("loop error:\n%s" % traceback.format_exc())
            time.sleep(5)


if __name__ == "__main__":
    main()
