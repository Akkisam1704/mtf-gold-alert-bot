# MGC MTF Alignment + Liquidity Sweep Alert Bot

Runs on a schedule via GitHub Actions, fetches fresh MGC (Micro Gold) bars
from TopstepX, checks whether the 15M trend is aligned with the 1H trend
(and how it got there — liquidity sweep or clean higher-low/lower-high),
and sends you a Telegram message whenever that status changes. No notebook,
no local machine needed to stay running — GitHub runs it for you.

## What you get alerted on

A message is sent only when something actually changes since the last
check (not every 15 minutes regardless):
- 1H bias flips (bullish/bearish/ranging)
- 15M bias flips
- 15M becomes aligned or un-aligned with 1H

## One-time setup

### 1. Create the repo
Create a new GitHub repo (private is fine) and add these files exactly as
given: `alert_bot.py`, `requirements.txt`, `state.json`, and
`.github/workflows/mtf_alert.yml`.

### 2. Create your Telegram bot
1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`, give it a name and a username (must end in `bot`).
3. BotFather replies with a token that looks like
   `123456789:ABCdefGhIJKlmNoPQRstuVwxYZ`. **Save this** — it's your
   `TELEGRAM_BOT_TOKEN`.

### 3. Get your chat ID
1. Send any message to your new bot (search its username, hit Start, say "hi").
2. In your browser, visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   (replace `<YOUR_TOKEN>` with the token from step 2).
3. Look for `"chat":{"id":123456789,...}` in the response — that number is
   your `TELEGRAM_CHAT_ID`.

If you get an empty response `{"ok":true,"result":[]}`, you haven't sent
the bot a message yet — send one and refresh the URL.

### 4. Add secrets to GitHub
In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add all four:
- `TOPSTEPX_USERNAME`
- `TOPSTEPX_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 5. Push and test
Push the files to GitHub. Then go to the **Actions** tab → "MTF Gold Signal
Check" → **Run workflow** (this is the `workflow_dispatch` trigger) to fire
it manually and confirm you get a Telegram message. Check the run's logs
if you don't.

Once that works, it'll run automatically every 15 minutes on the schedule
in `.github/workflows/mtf_alert.yml` — no further action needed.

## Adjusting things

- **Alert frequency**: edit the cron line in `mtf_alert.yml`
  (`*/15 * * * *` = every 15 min). GitHub's minimum is 5 minutes
  (`*/5 * * * *`), but actual runs can lag behind the schedule by a few
  minutes, more so on free-tier runners during peak load.
- **Swing sensitivity / liquidity lookback**: these are the same tunable
  parameters from the Colab version. Open `alert_bot.py`, find the call to
  `run_mtf_detector(df_1h, df_15m)` inside `main()`, and pass your tuned
  values, e.g.:
  ```python
  report = run_mtf_detector(
      df_1h, df_15m,
      swing_left_1h=2, swing_right_1h=2,
      swing_left_15m=2, swing_right_15m=2,
      ext_lookback_15m=40, int_lookback_15m=10,
  )
  ```
- **Always alert every run** (instead of only on change): in `main()`,
  replace the `if state_changed(...)` block with an unconditional call to
  `send_telegram_message(format_alert(report))`.

## Notes

- The bot fetches a rolling recent window each run (120 days of 1H, 60
  days of 15M) rather than the full multi-year history — plenty for swing
  structure, and much lighter than re-pulling everything every 15 minutes.
- `state.json` is committed back to the repo by the workflow after each
  run so the bot remembers the last signal across runs. Don't edit it by
  hand while the workflow is active — it'll just get overwritten.
- This checks one instrument (MGC) and one timeframe pair (1H/15M) as
  built. Extending it to more instruments or timeframe pairs means
  duplicating the fetch + `run_mtf_detector` call for each and combining
  the messages — say the word if you want that built out.
