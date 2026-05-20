# ProFloat v3 — Low Float Catalyst Scanner

A professional trading scanner that runs 24/7 online and sends push
notifications to your phone when a low float stock has a high quality
catalyst with real volume and price action confirmation.

---

## What it does

Scans 9 news feeds every 60 seconds looking for:

| Catalyst | Score |
|---|---|
| FDA Approval | 10/10 |
| Merger / Acquisition | 9/10 |
| Clinical Trial Win | 8/10 |
| Government Contract | 8/10 |
| Earnings Beat | 7/10 |
| Short Squeeze | 7/10 |
| Defence Surge | 7/10 |
| Energy Surge | 6/10 |
| Partnership Deal | 6/10 |

When a catalyst is detected it checks the stock for:
- Gap % from prior close
- Float size in millions
- Relative volume vs 20-day average
- **Volume spike right now** — is volume accelerating this minute
- **Price making new highs** — is the stock still going up

Then assigns a grade and fires an alert.

---

## Grading System

Each signal scores points on 6 factors:

| Factor | What it checks |
|---|---|
| 1 | Catalyst score >= 6/10 |
| 2 | Gap >= 20% from prior close |
| 3 | Float <= 5M shares |
| 4 | RVOL >= 5x average |
| 5 | Volume spike happening RIGHT NOW |
| 6 | Price making new highs |

| Grade | Points | Action |
|---|---|---|
| A+ | 6 | Highest conviction — act immediately |
| A | 5 | Strong signal — enter with full size |
| B | 4 | Good setup — enter with smaller size |
| C | 3 | Marginal — watch only |
| D | 0-2 | Filtered out |

---

## Scanning Sessions

| Session | Hours (Eastern) | Purpose |
|---|---|---|
| Pre Market | 4:00 AM – 9:30 AM | Best signals — act on these |
| Regular | 9:30 AM – 4:00 PM | Live trading session |
| After Hours | 4:00 PM – 8:00 PM | Watchlist builder for tomorrow |
| Closed | 8:00 PM – 4:00 AM | Scanner sleeps |

---

## App Features

- **Signals tab** — Live alerts with grade, volume spike, new highs indicator, direct TradingView link
- **Watchlist tab** — Add tickers manually or from news, live quotes refresh
- **News tab** — Full feed with filter by catalyst type, one-click ticker add
- **Lookup tab** — Search any ticker for price, gap, float, RVOL, volume spike status
- **Settings tab** — Scanner config, session schedule, feed status
- **Sound alerts** — Toggle on/off, different sound per grade (A+ = siren, A = double beep, B = single tone)
- **Push notifications** — Pushover alerts to your phone with full signal details

---

## Files

```
profloat_v3/
├── app.py              ← Main scanner and web server
├── requirements.txt    ← Python dependencies
├── Procfile            ← Server start command for Render
├── render.yaml         ← Render deployment config
├── README.md           ← This file
└── templates/
    └── index.html      ← Full dashboard UI
```

---

## Deploy on Render (free tier works — $7/mo for 24/7)

### Step 1 — GitHub
Upload all these files to a GitHub repository.
Make sure the templates folder with index.html is included.

### Step 2 — Render
1. Go to render.com and sign up free
2. Click New → Web Service
3. Connect your GitHub repository
4. Render auto-detects settings from render.yaml
5. Click Create Web Service
6. Wait 3 minutes for the build

### Step 3 — Add Pushover tokens
1. In Render click Environment tab
2. Add these two variables:
   - PUSHOVER_USER = your user token from pushover.net
   - PUSHOVER_TOKEN = your app token from pushover.net
3. Click Save Changes — Render restarts automatically

### Step 4 — Get your URL
Render gives you a free URL like:
```
https://profloat-v3.onrender.com
```

Open it on your phone and add to home screen for app-like experience.

---

## Environment Variables

| Variable | Default | What it does |
|---|---|---|
| PUSHOVER_USER | — | Your Pushover user token |
| PUSHOVER_TOKEN | — | Your Pushover app token |
| MIN_SCORE | 5 | Minimum catalyst score 1-10 |
| MIN_GAP | 10.0 | Minimum gap % from prior close |
| MAX_FLOAT | 10.0 | Maximum float in millions |
| MIN_PRICE | 0.30 | Minimum stock price |
| MAX_PRICE | 50.0 | Maximum stock price |
| MIN_VOLUME | 50000 | Minimum volume today |
| MIN_RVOL | 1.5 | Minimum relative volume |
| SCAN_SECS | 60 | Seconds between scans |

---

## Push Notification Setup

1. Go to pushover.net — sign up free
2. Install Pushover app on phone — one time $5
3. Create an application at pushover.net/apps/build
4. Copy User Token and App Token
5. Add both to Render Environment variables

### What the phone alert looks like

```
💊 ACME Grade A+ +67%
FDA Approval

Acme Pharma receives FDA approval for cancer drug

$4.82 | Gap +67.3%
Vol 2.4M | Float 1.2M
RVOL 12.5x | Score 10/10
⚡ Vol Spike 8.2x recent
↑ Making New Highs
09:35:22 ET
```

---

## Workflow with TradingView

```
Scanner fires A+ alert on phone
↓
Open TradingView
↓
Load ticker on 1-minute chart
↓
Lookup ticker in app to get float number
↓
Set that float number in Low Float Breakout v3 indicator
↓
Wait for HOD break signal on chart
↓
Enter trade using entry stop and target shown in label
```

---

## Cost

| Service | Cost |
|---|---|
| GitHub | Free |
| Render free tier | Free but sleeps after 15 min |
| Render Starter | $7/month — runs 24/7 |
| Pushover app | $5 one time |
| Pushover service | Free |
| **Total for 24/7** | **$7/month** |
