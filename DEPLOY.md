# Deploy to Railway (Free Cloud Hosting)

Railway gives you $5/month free credit — enough to run this app 24/7.
Your laptop stays OFF. Everything happens in the cloud automatically.

---

## One-Time Setup (takes ~15 minutes)

### Step 1 — Create accounts
1. Go to https://railway.app → click **Start a New Project** → sign up with GitHub
2. Go to https://github.com → create a free account if you don't have one

### Step 2 — Push your code to GitHub
Open Command Prompt in your project folder and run:
```
cd "D:\Projects\AI Stock Market Analyzer"
git init
git add .
git commit -m "Initial commit"
```
Then go to github.com → New Repository → name it `ai-stock-analyzer` → copy the commands it gives you to push.

### Step 3 — Deploy on Railway
1. Go to railway.app → **New Project** → **Deploy from GitHub repo**
2. Select your `ai-stock-analyzer` repo
3. Railway will auto-detect Python and start building (takes ~5 min)

### Step 4 — Add a Persistent Volume (so your database survives restarts)
1. In Railway, click your service → **Volumes** tab → **Add Volume**
2. Mount path: `/data`
3. This keeps your trades and database safe even if the app restarts

### Step 5 — Set Environment Variables
In Railway → your service → **Variables** tab, add these:

| Variable | Value |
|----------|-------|
| `DATA_DIR` | `/data` |
| `TELEGRAM_BOT_TOKEN` | your token from @BotFather |
| `TELEGRAM_CHAT_ID` | your chat ID from @userinfobot |
| `ALERT_EMAIL_FROM` | your Gmail |
| `ALERT_EMAIL_PASSWORD` | your Gmail app password |
| `ALERT_EMAIL_TO` | where to send alerts |
| `ALERT_MIN_CONFIDENCE` | `0.55` |
| `DATA_ADAPTER` | `yfinance` |

### Step 6 — Get your public URL
Railway gives you a URL like `https://ai-stock-analyzer.up.railway.app`
Open it in any browser — that's your live dashboard.

---

## What runs automatically after deployment

| When | What happens |
|------|-------------|
| Every weekday at 3:35 PM IST | Paper trading runs, checks signals, logs trades, sends Telegram alert |
| 1st of every month at 7:00 AM IST | Model retrains on fresh data automatically |
| Always | Dashboard is live at your Railway URL |

---

## Check if it's working
- Open your Railway URL → dashboard should load
- Go to Railway → your service → **Logs** tab to see daily run output
- You'll get a Telegram message every weekday at 3:35 PM IST

---

## Cost
Railway free tier: **$5/month credit** (no credit card for signup).
This app uses roughly $3-4/month. You're fine on the free tier.
