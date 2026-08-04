# Gaming session bot — toy build

One Python file, no dependencies, SQLite for state. Runs on anything with Python 3.11+.
This is a 2nd version with the following updates:
1) Scheduling and display bugs fixed for the "Group Board" tab
2) New profile attributes added, to simplify the long-term application of the bot 

## What it does

- Each person picks a **status** (`9/5`, `Remote`, `On Vacation (No gaming)`, `On Vacation (Can Join)`) and sets **default free time** for it, day by day — Monday can look nothing like Tuesday. Switching status later swaps in that status's own default free time — set once per status, the first time you use it.
- After that they only log when they're **busy**.
- Each person also sets a **time zone** on their profile. Default free time and busy hours are entered in that person's own local time and converted onto one shared clock before anyone's overlap is compared, and poll windows, session cards, activities, and reminders are converted back to each person's own local time for display - two people in different zones each enter and read times in their own head, and the bot reconciles the difference. The `/week` grid itself stays on that shared clock, labeled as such, since rotating a shared table per viewer isn't practical - and it only marks an hour when *everyone* overlaps it, not just anyone.
- `/week` shows everyone's overlap as a grid, with the best windows named.
- Anyone starts a **poll** over real candidate windows; you vote *I'm in* or *maybe*, and pick games separately.
- The poll closes into an **agreement**: one day, one time, one game, one participant list.
- Everyone gets a **reconfirmation** 72 h later, plus reminders 24 h and 1 h before, all by DM.
- **My plans changed** is on the session card permanently. Dropping below 3 committed flips it to *at risk* and asks the rest whether to keep it or call it off. The bot never cancels by itself.
- **Activities**: anyone proposes something, others join, weekly repeat supported.
- **Stats**: sessions played, top game, longest run of weeks, who turns up when they said they would.
- One proactive suggestion per person per day, maximum.
- Non-members get **no reply at all** — not an error, silence.

## Setup, 5 minutes

1. Message **@BotFather** on Telegram → `/newbot` → copy the token.
2. Message **@userinfobot** → copy your numeric id.
3. Turn off group privacy you don't need: BotFather → `/setjoingroups` → **Disable**. This bot only works in private chats.

```bash
export BOT_TOKEN='123456:AA...'
export ADMIN_ID='123456789'
python3 bot.py
```

4. Send `/start` to your bot. You're added as admin.
5. Add your friends by numeric id: `/add 987654321 Marat`. Each of them sends `/start` and walks through setup.

They can get their own id from @userinfobot. Anyone not added is ignored completely.

## Free hosting, in the order I'd try them

**Your own machine.** A laptop that's usually on, an old desktop, a Raspberry Pi. Long polling means no port forwarding, no public IP, no firewall changes — the bot dials out to Telegram. Cheapest and most private. If it's off for two hours nobody notices; jobs fire on the next start because every timer lives in the database, not in memory.

On Linux, keep it running with systemd:

```ini
# /etc/systemd/system/sessionbot.service
[Unit]
Description=Gaming session bot
After=network-online.target

[Service]
User=YOURNAME
WorkingDirectory=/home/YOURNAME/sessionbot
Environment=BOT_TOKEN=123456:AA...
Environment=ADMIN_ID=123456789
ExecStart=/usr/bin/python3 /home/YOURNAME/sessionbot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now sessionbot
journalctl -u sessionbot -f      # watch it
```

On Windows, Task Scheduler with "run at logon" works. On macOS, a launchd plist or just leave a terminal open.

**Oracle Cloud Always Free.** A real VM, free forever, same systemd unit as above. Two catches: it needs a credit card at signup, and Oracle reclaims instances that look idle (under 20% CPU, network and memory across 7 days) — which this bot will be. Converting the account to Pay As You Go exempts you from reclamation and still costs nothing inside the free limits, but it means a live card on the account.

**Not worth it:** free PaaS tiers with ephemeral disks. The bot will run and then lose `sessions.db` on the next redeploy, silently, taking everyone's schedule with it. Also avoid the "free Telegram bot hosting" sites — you'd be handing your token to someone you can't audit, and at least one of them requires re-arming every 24 hours, which breaks a bot built around 72-hour timers.

## Backups

The whole state is one file. On the machine running it:

```bash
crontab -e
0 */6 * * * sqlite3 /home/YOURNAME/sessionbot/sessions.db ".backup '/home/YOURNAME/backups/sessions-$(date +\%u\%H).db'"
```

That keeps a rolling week. Copy one off the machine occasionally — a backup on the same disk isn't a backup. To restore: stop the service, copy the file back to `sessions.db`, start it.

## Tuning

Constants at the top of `bot.py`:

| Name | Default | Meaning |
|---|---|---|
| `DAY_START` / `DAY_END` | 00:00 / 24:00 | the span you're ever asked about; the whole day |
| `MIN_PLAYERS` | 3 | how many committed keeps an agreed session off the at-risk list |
| `MIN_SESSION` | 90 min | shorter windows are ignored |
| `POLL_HOURS` | 24 | how long a poll stays open |
| `RECONFIRM_HOURS` | 72 | when the "still good?" DM goes out |
| `SUGGEST_HOUR` | 19 | when the one daily suggestion is sent |
| `GAMES` | list | edit to your library |

## Tests

```bash
python3 test_logic.py
```

Covers interval subtraction and candidate-window generation — the two places where a bug would be quiet and wrong rather than loud and obvious.

## Known limits of the toy build

- A member picks a city, not a raw offset - the offset used for conversion is looked up live for the date in question, so it correctly follows that city's own daylight saving (Berlin's +1 in January, +2 in August; Almaty has none and stays +5 year-round). Changing which city you've picked, though, doesn't retroactively reinterpret default free time already entered under the old one.
- On Windows, the DST-aware lookup needs the `tzdata` package (`pip install tzdata`) since Windows has no built-in IANA time zone database; Linux and macOS normally have one already and need nothing extra.
- No invite codes — the admin adds numeric ids by hand.
- No audit log or rate limiting beyond Telegram's own. Fine for ten friends, not for a public bot.
- One contiguous free window per weekday per status, not several separate ones on the same day.
- Weekly recurrence only, no "every other Thursday".
- `sqlite3` CLI needed for the backup command above; the bot itself only needs the Python module.
