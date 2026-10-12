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

## Or host it on Vercel

The hosted version runs online, so reminders arrive as notifications on your phone and computer, even when your PC is off. It's the same web app, backed by [`api/index.py`](api/index.py) instead of `server.py`.

1. **Database:** in your Vercel project, open **Storage**, create an **Upstash for Redis** database (free plan) and connect it to the project. This adds the `KV_REST_API_*` variables automatically.
2. **Secrets:** in **Settings → Environment Variables**, add:
   - `APP_PASSWORD`: the password you'll use to open the app. Pick a strong one, because your Unstop login tokens are stored on the server.
   - `CRON_SECRET`: a long random string, for the reminder scheduler.
3. **Redeploy** (Deployments → ⋯ → Redeploy) so the new settings take effect. The project's Framework Preset should be **Other**.
4. **Reminder schedule:** Vercel's free plan only runs scheduled jobs once a day, which is too slow for reminders. Create a free account at [cron-job.org](https://cron-job.org) and add a job that opens
   `https://<your-app>.vercel.app/api/cron?key=<your CRON_SECRET>` **every 5 minutes**. (On Vercel Pro you can change the schedule in `vercel.json` to `*/5 * * * *` instead.)
5. Open your app, enter the password, then in **Settings** press **Turn on** under *Notifications on this device*. Do this on every phone and computer where you want reminders, then press **Send test**.
   - **iPhone/iPad:** first tap Share → **Add to Home Screen** and open the app from there. Safari only allows notifications for home-screen apps.
6. In **Accounts**, drag the **⏰ Connect to Reminder** button to your bookmarks bar again. A bookmark made from the local app points at your PC and won't work with the hosted app.

## Connect your Unstop account

1. Open the **Accounts** tab and drag **⏰ Connect to Reminder** to your bookmarks bar.
2. Log in on unstop.com and click the bookmark.
3. Name the account and press **Connect**. Repeat for each account.

You can also add any competition by pasting its Unstop link.

## Privacy

**Hosted version:** your Unstop login token and competitions are stored in your own Upstash database and protected by `APP_PASSWORD`.

**Local version:** everything stays on your PC. Your Unstop login token is stored in `accounts.json` (never your password) and your competitions in `data.json`. Both are listed in `.gitignore`, so they never get committed.

## Files

| File | What it does |
|---|---|
| `server.py` | Local server: fetches from Unstop, stores data, sends notifications |
| `index.html` | The web app |
| `start.bat` | Starts the server in the background and opens the app |
| `api/index.py` | Hosted backend for Vercel: same features, data in Upstash Redis, browser push notifications |
| `sw.js`, `manifest.webmanifest`, `icon-*.png` | Let the browser show notifications and install the app on your phone |
| `vercel.json`, `requirements.txt`, `.vercelignore` | Vercel configuration |

*Not affiliated with Unstop. Uses Unstop's website APIs, which may change without notice.*
