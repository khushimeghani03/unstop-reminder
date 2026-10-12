# Unstop Round Reminder

A small Windows app that reminds you before the rounds of competitions you've registered for on [Unstop](https://unstop.com).

- Connect your Unstop account(s) and your registered competitions are imported automatically, with every round's dates
- Windows notifications before a round starts and before it closes (timings are configurable)
- Skips competitions that have ended, and ones where you've been eliminated after round 1
- Re-syncs dates from Unstop every 6 hours
- Dashboard with live countdowns, plus export to Google Calendar (.ics) for phone alerts

## Requirements

- Windows 10/11
- Python 3.9+ (standard library only, nothing to `pip install`)

## Run it

```
python server.py
```

Then open http://127.0.0.1:8765. Or double-click `start.bat`, which starts it in the background and opens the page.

**Start automatically at login:** press `Win + R`, type `shell:startup`, and put a shortcut there that runs `pythonw.exe "path\to\server.py"`.

## Connect your Unstop account

1. Open the **Accounts** tab and drag **⏰ Connect to Reminder** to your bookmarks bar.
2. Log in on unstop.com and click the bookmark.
3. Name the account and press **Connect**. Repeat for each account.

You can also add any competition by pasting its Unstop link.

## Privacy

Everything stays on your PC. Your Unstop login token is stored in `accounts.json` (never your password) and your competitions in `data.json`. Both are listed in `.gitignore`, so they never get committed.

## Files

| File | What it does |
|---|---|
| `server.py` | Local server: fetches from Unstop, stores data, sends notifications |
| `index.html` | The web app |
| `start.bat` | Starts the server in the background and opens the app |

*Not affiliated with Unstop. Uses Unstop's website APIs, which may change without notice.*
