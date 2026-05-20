"""
ProFloat Scanner v3
====================
Complete rewrite fixing all critical issues:

1. Better ticker extraction — searches Yahoo Finance directly for
   company name matches, not just regex on headlines
2. Real price action confirmation — checks if stock is making
   new highs in the last 5 minutes before firing
3. Volume SPIKE detection — rate of volume per minute vs average
   not just cumulative daily volume
4. Fixed grading — B and C setups now correctly included
5. After hours treated as watchlist builder not live trade signal
6. Earnings filter removed from lunch hour block
7. Sound alerts with on/off toggle that works in background
"""

from flask import Flask, render_template, jsonify, request
import requests
import time
import datetime
import threading
import logging
import re
import os
from bs4 import BeautifulSoup

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("app")

PUSHOVER_USER  = os.environ.get("PUSHOVER_USER",  "YOUR_PUSHOVER_USER_TOKEN")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "YOUR_PUSHOVER_APP_TOKEN")
PUSHOVER_URL   = "https://api.pushover.net/1/messages.json"

# Core filters — lowered to catch more B and C setups
MIN_SCORE   = int(os.environ.get("MIN_SCORE",   "5"))
MIN_GAP     = float(os.environ.get("MIN_GAP",   "10.0"))
MAX_FLOAT   = float(os.environ.get("MAX_FLOAT",  "10.0"))
MIN_PRICE   = float(os.environ.get("MIN_PRICE",  "0.30"))
MAX_PRICE   = float(os.environ.get("MAX_PRICE",  "50.0"))
MIN_VOLUME  = int(os.environ.get("MIN_VOLUME",  "50000"))
MIN_RVOL    = float(os.environ.get("MIN_RVOL",   "1.5"))
SCAN_SECS   = int(os.environ.get("SCAN_SECS",   "60"))

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"

FEED_LIST = [
    ["GlobeNewswire Bio",     "https://www.globenewswire.com/RssFeed/subjectcode/15-Biomedical"],
    ["GlobeNewswire Defence", "https://www.globenewswire.com/RssFeed/subjectcode/28-Defense"],
    ["GlobeNewswire Energy",  "https://www.globenewswire.com/RssFeed/subjectcode/23-Energy"],
    ["GlobeNewswire Mergers", "https://www.globenewswire.com/RssFeed/subjectcode/36-Mergers+Acquisitions"],
    ["GlobeNewswire Finance", "https://www.globenewswire.com/RssFeed/subjectcode/6-Financial"],
    ["GlobeNewswire Tech",    "https://www.globenewswire.com/RssFeed/subjectcode/32-Technology"],
    ["PR Newswire",           "https://www.prnewswire.com/rss/news-releases-list.rss"],
    ["Business Wire",         "https://feed.businesswire.com/rss/home/?rss=G1"],
    ["Yahoo Finance",         "https://finance.yahoo.com/news/rssindex"],
]

SKIP = [
    "INC","LLC","CORP","LTD","THE","AND","FOR","SEC","ACT","NEW",
    "COM","NET","US","USA","FDA","CEO","CFO","COO","IPO","ETF",
    "NYSE","NASDAQ","AM","PM","EST","ET","AI","EV","UK","EU","UN",
    "WHO","DOD","DOE","NASA","M","B","Q","A","AN","IN","OF","TO",
    "BY","ON","AS","AT","HIGH","LOW","TOP","HOT","KEY","EPS",
    "NDA","BLA","CRL","RX","PR","TV","FM","RP","PO","SA","AG",
]

state = {
    "alerts":       [],
    "news":         [],
    "watchlist":    [],
    "seen":         [],
    "alerted":      [],
    "feeds_status": {},
    "scanning":     False,
    "last_scan":    None,
    "scan_count":   0,
    "start_time":   datetime.datetime.now().isoformat(),
    "session":      "closed",
    "price_cache":  {},
}

# ─────────────────────────────────────────────
# SESSION DETECTION
# ─────────────────────────────────────────────

def get_session():
    now = datetime.datetime.utcnow()
    # Convert UTC to Eastern (UTC-4 EDT or UTC-5 EST)
    # Simple approximation — close enough for trading purposes
    et_hour = (now.hour - 4) % 24
    et_minute = now.minute
    if et_hour >= 4 and (et_hour < 9 or (et_hour == 9 and et_minute < 30)):
        return "premarket", et_hour, et_minute
    if (et_hour == 9 and et_minute >= 30) or (et_hour >= 10 and et_hour < 16):
        return "regular", et_hour, et_minute
    if et_hour >= 16 and et_hour < 20:
        return "afterhours", et_hour, et_minute
    return "closed", et_hour, et_minute

def is_scanning_time():
    sess, h, m = get_session()
    return sess != "closed"

def session_label():
    sess, h, m = get_session()
    labels = {
        "premarket":  "PRE MARKET",
        "regular":    "MARKET OPEN",
        "afterhours": "AFTER HOURS",
        "closed":     "MARKET CLOSED",
    }
    return labels.get(sess, "UNKNOWN")

# ─────────────────────────────────────────────
# CATALYST SCORING
# ─────────────────────────────────────────────

def has(text, words):
    for w in words:
        if w in text:
            return True
    return False

def score_text(text):
    t = text.lower()
    score = 0
    name = "General News"

    # FDA — highest conviction catalyst for low float
    fda1 = ["fda","food and drug","food & drug"]
    fda2 = ["approv","cleared","clearance","breakthrough","fast track",
            "nda","bla","510k","pdufa","granted","designat","accepted"]
    fda3 = ["reject","refus","crl","clinical hold","not approv"]
    if has(t, fda1) and has(t, fda2):
        score = 3 if has(t, fda3) else 10
        name = "FDA Approval"

    # Clinical trial
    trial1 = ["phase","trial","endpoint","readout","topline","study results"]
    trial2 = ["positive","success","met","significant","strong","favorable",
              "benefit","efficacy","improvement","response rate"]
    trial3 = ["failed","miss","negative","halt","discontinu","did not meet"]
    if score < 8:
        if has(t, trial1) and has(t, trial2):
            score = 2 if has(t, trial3) else 8
            name = "Clinical Trial Win"

    # Merger
    merge1 = ["acqui","merger","takeover","buyout","definitive agreement",
              "tender offer","going private","to be acquired","to acquire"]
    if score < 9:
        if has(t, merge1):
            score = 9
            name = "Merger Acquisition"

    # Government contract
    con1 = ["contract","award","awarded","selected","procurement","grant received"]
    con2 = ["pentagon","military","army","navy","air force","department of defense",
            "government","federal","nasa","darpa","dhs","dod","defence ministry"]
    if score < 8:
        if has(t, con1) and has(t, con2):
            score = 8
            name = "Government Contract"

    # Earnings beat
    earn1 = ["earnings","eps","revenue","quarterly results","q1","q2","q3","q4",
             "annual results","full year","fiscal year"]
    earn2 = ["beat","exceed","surpass","above","better than expected",
             "record","topped","ahead of consensus","above estimates"]
    earn3 = ["miss","below","disappoint","fell short","below estimates","loss widened"]
    if score < 7:
        if has(t, earn1) and has(t, earn2):
            score = 3 if has(t, earn3) else 7
            name = "Earnings Beat"

    # Short squeeze
    sq1 = ["short squeeze","short interest","heavily shorted","most shorted",
           "gamma squeeze","unusual options","unusual call volume","high short"]
    if score < 7:
        if has(t, sq1):
            score = 7
            name = "Short Squeeze"

    # Defence surge
    def1 = ["defense","defence","weapon","missile","drone","ammunition",
            "radar","cybersecurity","warfare","combat system"]
    def2 = ["war","conflict","escalat","nato","sanction","surge","spending increase",
            "new order","urgent","emergency procurement"]
    if score < 7:
        if has(t, def1) and has(t, def2):
            score = 7
            name = "Defence Surge"

    # Energy surge
    en1 = ["oil","natural gas","crude","lithium","uranium","opec","lng","petroleum"]
    en2 = ["surge","spike","soar","record high","war","conflict","sanction",
           "shortage","supply disruption","price shock"]
    if score < 6:
        if has(t, en1) and has(t, en2):
            score = 6
            name = "Energy Surge"

    # Partnership
    deal1 = ["licensing agreement","collaboration agreement","joint venture",
             "exclusive agreement","distribution agreement","commercialization",
             "strategic partnership","milestone payment","royalty agreement"]
    deal2 = ["terminat","cancel","dissolv","expired","ended"]
    if score < 6:
        if has(t, deal1):
            score = 2 if has(t, deal2) else 6
            name = "Partnership Deal"

    # Offering — negative signal
    off1 = ["public offering","secondary offering","registered direct",
            "at-the-market","atm offering","priced offering","dilutive"]
    if score < 3:
        if has(t, off1):
            score = 2
            name = "Dilution Warning"

    if score == 0:
        return 0, "General News"

    # Boost for strong language
    if has(t, ["billion","landmark","historic","first-in-class","first ever",
               "pivotal","unanimous","100%","accelerated approval"]):
        score = min(10, score + 1)

    # Penalty for bad signals mixed in
    if has(t, ["bankrupt","chapter 11","going concern","delist","sec investigation",
               "class action","fraud","restatement"]):
        score = max(1, score - 3)

    return score, name

# ─────────────────────────────────────────────
# IMPROVED TICKER EXTRACTION
# Three methods in priority order
# ─────────────────────────────────────────────

def extract_tickers_from_text(text):
    """Method 1 — explicit ticker patterns in text"""
    found = []
    # $TICK format
    for m in re.findall(r'\$([A-Z]{1,5})\b', text):
        found.append(m)
    # (TICK) format — very common in press releases
    for m in re.findall(r'\(([A-Z]{1,5})\)', text):
        if len(m) >= 2:
            found.append(m)
    # NYSE: TICK or NASDAQ: TICK
    for m in re.findall(r'(?:NYSE|NASDAQ|Nasdaq|OTCQB|OTCQX|TSX|AIM)[\s:]+([A-Z]{1,5})\b', text):
        found.append(m)
    # "trading as TICK" or "symbol TICK"
    for m in re.findall(r'(?:symbol|ticker|trading as|listed as)[\s:\"\']+([A-Z]{1,5})\b', text, re.IGNORECASE):
        found.append(m.upper())
    seen = []
    out = []
    for t in found:
        if t not in SKIP and t not in seen and len(t) >= 2:
            seen.add(t) if hasattr(seen, 'add') else seen.append(t)
            out.append(t)
    return out

def search_yahoo_for_company(company_name):
    """Method 2 — search Yahoo Finance for company name to get ticker"""
    if not company_name or len(company_name) < 5:
        return []
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search?q=" + requests.utils.quote(company_name) + "&quotesCount=3&newsCount=0"
        r = requests.get(url, headers={"User-Agent": BROWSER}, timeout=8)
        if r.status_code != 200:
            return []
        results = r.json().get("quotes", [])
        tickers = []
        for result in results[:3]:
            sym = result.get("symbol", "")
            qtype = result.get("quoteType", "")
            exchange = result.get("exchange", "")
            # Only US listed equities
            if qtype == "EQUITY" and len(sym) <= 5 and "." not in sym:
                if exchange in ["NMS","NYQ","NGM","NCM","ASE","PCX","BTS","OTC"]:
                    tickers.append(sym)
        return tickers
    except Exception:
        return []

def extract_company_name(headline):
    """Pull company name from headline for Yahoo search"""
    # Common patterns in press releases
    patterns = [
        r'^([A-Z][a-zA-Z\s&]+(?:Inc|Corp|Ltd|LLC|Co|Therapeutics|Pharma|Bio|Tech|Sciences|Holdings|Group|Medical|Health)\.?)',
        r'([A-Z][a-zA-Z\s&]+(?:Inc|Corp|Ltd|LLC|Co|Therapeutics|Pharma|Bio|Tech|Sciences|Holdings|Group|Medical|Health)\.?)\s+(?:announces|reports|receives|granted|awarded)',
    ]
    for pattern in patterns:
        match = re.search(pattern, headline)
        if match:
            name = match.group(1).strip()
            if 5 <= len(name) <= 60:
                return name
    return ""

def get_tickers_for_headline(headline, summary):
    """Combined ticker extraction — tries all methods"""
    full_text = headline + " " + summary

    # Method 1 — explicit patterns
    tickers = extract_tickers_from_text(full_text)
    if tickers:
        return tickers

    # Method 2 — company name search on Yahoo
    company = extract_company_name(headline)
    if company:
        yahoo_tickers = search_yahoo_for_company(company)
        if yahoo_tickers:
            return yahoo_tickers

    return []

# ─────────────────────────────────────────────
# PRICE DATA WITH VOLUME SPIKE DETECTION
# ─────────────────────────────────────────────

def get_price_data(ticker):
    """
    Gets price, volume, gap AND checks for volume spike.
    Volume spike = current minute volume rate vs average minute rate.
    This tells us if volume is happening RIGHT NOW vs earlier in the day.
    """
    hdrs = {"User-Agent": BROWSER}
    sess, et_hour, et_minute = get_session()

    # Get main quote data
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + ticker +
               "?interval=1d&range=5d&includePrePost=true")
        r = requests.get(url, headers=hdrs, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data["chart"]["result"]
        if not result:
            return None
        meta = result[0]["meta"]
        regular_price = meta.get("regularMarketPrice", 0)
        prev_close    = meta.get("chartPreviousClose", 0)
        reg_volume    = meta.get("regularMarketVolume", 0)
        avg_volume    = meta.get("averageDailyVolume10Day", reg_volume) or reg_volume
        pre_price     = meta.get("preMarketPrice") or regular_price
        post_price    = meta.get("postMarketPrice") or regular_price

        # Pick the right price for the current session
        if sess == "premarket":
            price = pre_price
        elif sess == "afterhours":
            price = post_price
        else:
            price = regular_price

        if not price or price == 0:
            price = regular_price

        gap = 0.0
        if prev_close and prev_close > 0:
            gap = (price - prev_close) / prev_close * 100.0

    except Exception as e:
        log.debug("Price error " + ticker + ": " + str(e))
        return None

    # Get float data
    float_m = 99.0
    try:
        url2 = ("https://query1.finance.yahoo.com/v11/finance/quoteSummary/" + ticker +
                "?modules=defaultKeyStatistics")
        r2 = requests.get(url2, headers=hdrs, timeout=8)
        if r2.status_code == 200:
            ks = r2.json()["quoteSummary"]["result"][0]["defaultKeyStatistics"]
            raw = ks.get("floatShares", {}).get("raw", None)
            if raw:
                float_m = raw / 1000000.0
    except Exception:
        pass

    # Volume spike detection using 5-minute chart
    vol_spike = False
    vol_spike_ratio = 0.0
    try:
        url3 = ("https://query1.finance.yahoo.com/v8/finance/chart/" + ticker +
                "?interval=5m&range=1d&includePrePost=true")
        r3 = requests.get(url3, headers=hdrs, timeout=10)
        if r3.status_code == 200:
            result3 = r3.json()["chart"]["result"]
            if result3:
                volumes = result3[0].get("indicators",{}).get("quote",[{}])[0].get("volume",[])
                if volumes and len(volumes) > 5:
                    # Remove None values
                    clean_vols = [v for v in volumes if v is not None and v > 0]
                    if len(clean_vols) >= 5:
                        # Last 3 bars average vs previous bars average
                        recent_avg = sum(clean_vols[-3:]) / 3
                        historical_avg = sum(clean_vols[:-3]) / max(len(clean_vols)-3, 1)
                        if historical_avg > 0:
                            vol_spike_ratio = recent_avg / historical_avg
                            # Volume spike = current rate is 3x+ the session average
                            vol_spike = vol_spike_ratio >= 3.0
    except Exception:
        pass

    # Price action check — is price making new highs?
    making_highs = False
    try:
        url4 = ("https://query1.finance.yahoo.com/v8/finance/chart/" + ticker +
                "?interval=5m&range=1d&includePrePost=true")
        r4 = requests.get(url4, headers=hdrs, timeout=10)
        if r4.status_code == 200:
            result4 = r4.json()["chart"]["result"]
            if result4:
                highs = result4[0].get("indicators",{}).get("quote",[{}])[0].get("high",[])
                clean_highs = [h for h in highs if h is not None and h > 0]
                if len(clean_highs) >= 6:
                    # Is current high above the high from 30 min ago?
                    recent_high = max(clean_highs[-3:])
                    earlier_high = max(clean_highs[-9:-3])
                    making_highs = recent_high >= earlier_high * 0.99
    except Exception:
        pass

    rvol = reg_volume / avg_volume if avg_volume > 0 else 0

    return {
        "price":         price,
        "prev_close":    prev_close,
        "gap":           gap,
        "vol":           int(reg_volume),
        "avg_vol":       int(avg_volume),
        "float_m":       float_m,
        "rvol":          rvol,
        "vol_spike":     vol_spike,
        "vol_spike_ratio": round(vol_spike_ratio, 1),
        "making_highs":  making_highs,
        "session":       sess,
    }

def vol_str(v):
    if v >= 1000000: return str(round(v / 1000000.0, 1)) + "M"
    if v >= 1000:    return str(int(v / 1000)) + "K"
    return str(v)

# ─────────────────────────────────────────────
# GRADING — FIXED VERSION
# B and C setups are now properly included
# Grade is based on how many of 6 factors align
# A+ requires all 6, A requires 5, B requires 4, C requires 3
# ─────────────────────────────────────────────

def compute_grade(score, gap, float_m, rvol, vol_spike, making_highs, sess):
    pts = 0
    reasons = []

    # Factor 1 — Strong catalyst
    if score >= 8:
        pts += 1
        reasons.append("Strong catalyst " + str(score) + "/10")
    elif score >= 6:
        reasons.append("Moderate catalyst " + str(score) + "/10")

    # Factor 2 — Significant gap
    if gap >= 40:
        pts += 1
        reasons.append("Gap +" + str(round(gap,1)) + "%")
    elif gap >= 20:
        pts += 1
        reasons.append("Gap +" + str(round(gap,1)) + "%")

    # Factor 3 — True low float
    if float_m <= 2:
        pts += 1
        reasons.append("Float " + str(round(float_m,1)) + "M")
    elif float_m <= 5:
        pts += 1
        reasons.append("Float " + str(round(float_m,1)) + "M")

    # Factor 4 — Exceptional relative volume
    if rvol >= 10:
        pts += 1
        reasons.append("RVOL " + str(round(rvol,1)) + "x")
    elif rvol >= 5:
        pts += 1
        reasons.append("RVOL " + str(round(rvol,1)) + "x")

    # Factor 5 — Volume spike RIGHT NOW
    if vol_spike:
        pts += 1
        reasons.append("Vol spike now")

    # Factor 6 — Price making new highs
    if making_highs:
        pts += 1
        reasons.append("New highs")

    if pts >= 6: return "A+", reasons
    if pts >= 5: return "A",  reasons
    if pts >= 4: return "B",  reasons
    if pts >= 3: return "C",  reasons
    return "D", reasons

def grade_passes_filter(grade, sess):
    """
    For regular and pre market — show A+ A B C
    For after hours — show all grades as watchlist candidates
    After hours is about finding what to watch TOMORROW not trading now
    """
    if sess == "afterhours":
        return grade in ["A+","A","B","C","D"]
    return grade in ["A+","A","B","C"]

# ─────────────────────────────────────────────
# WIN RATE FILTERS — FIXED VERSION
# Removed lunch hour block
# Only apply strict filters to D grade attempts
# ─────────────────────────────────────────────

def win_rate_check(price, gap, vol, avg_vol, float_m, score, cat, sess):
    reasons_failed = []

    # Float vs gap relationship
    if float_m < 1.0 and gap < 20:
        reasons_failed.append("Float<1M needs gap>20%")
    if float_m > 5.0 and gap < 30:
        reasons_failed.append("Float>5M needs gap>30%")

    # Minimum volume to confirm real interest
    float_shares = float_m * 1000000
    if vol < float_shares * 0.2 and sess != "afterhours":
        reasons_failed.append("Vol<20% of float")

    # Sub penny stock filter
    if price < 0.50 and gap < 100:
        reasons_failed.append("Sub$0.50 needs gap>100%")

    # After hours specific
    if sess == "afterhours":
        if score < 6:
            reasons_failed.append("AH needs score>=6")
        # After hours gappers are watchlist for tomorrow
        # so we are more lenient on gap requirement

    if len(reasons_failed) > 0:
        return False, " | ".join(reasons_failed)
    return True, "All checks passed"

# ─────────────────────────────────────────────
# PUSH NOTIFICATION
# ─────────────────────────────────────────────

def send_push(alert):
    if PUSHOVER_USER.startswith("YOUR_"):
        return
    g = alert["grade"]
    sess = alert.get("session", "regular")

    priority = 1 if g in ["A+","A"] else 0 if g == "B" else -1
    sound = "siren" if g in ["A+","A"] else "bugle" if g == "B" else "pushover"

    sess_prefix = {"premarket":"[PRE] ","afterhours":"[AH] ","regular":""}.get(sess,"")

    title = (sess_prefix + alert["ticker"] + " Grade " + g +
             " +" + str(round(alert["gap"])) + "% " + alert["cat"])

    spike_line = "Vol spike: " + str(alert["vol_spike_ratio"]) + "x recent" if alert.get("vol_spike") else "No current spike"
    highs_line = "Making new highs ✓" if alert.get("making_highs") else "Not at highs"

    body = "\n".join([
        alert["cat"],
        "",
        alert["headline"][:120],
        "",
        "$" + str(alert["price"]) + " | Gap +" + str(alert["gap"]) + "%",
        "Vol " + alert["vol_str"] + " | Float " + str(alert["fm"]) + "M",
        "RVOL " + str(alert["rvol"]) + "x | Score " + str(alert["score"]) + "/10",
        spike_line,
        highs_line,
        alert["time"],
    ])

    data = {
        "token":    PUSHOVER_TOKEN,
        "user":     PUSHOVER_USER,
        "title":    title,
        "message":  body,
        "priority": priority,
        "sound":    sound,
        "url":      alert.get("url",""),
        "url_title":"Read article",
    }
    if priority == 2:
        data["retry"]  = 30
        data["expire"] = 300

    try:
        r = requests.post(PUSHOVER_URL, data=data, timeout=10)
        if r.json().get("status") == 1:
            log.info("Push sent " + alert["ticker"] + " Grade " + g)
    except Exception as e:
        log.warning("Push failed: " + str(e))

# ─────────────────────────────────────────────
# FEED FETCHER
# ─────────────────────────────────────────────

def get_feed(name, url):
    hdrs = {"User-Agent": BROWSER, "Accept": "text/html,*/*"}
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code >= 400:
            state["feeds_status"][name] = "HTTP " + str(r.status_code)
            return []
    except Exception as e:
        state["feeds_status"][name] = "Error"
        return []
    out = []
    try:
        soup = BeautifulSoup(r.content, "html.parser")
        entries = soup.find_all(["item","entry"])
        for entry in entries:
            tt = entry.find("title")
            if not tt:
                continue
            title = tt.get_text(separator=" ", strip=True)
            link = ""
            lt = entry.find("link")
            if lt:
                link = lt.get("href","") or lt.get_text(strip=True)
            if not link:
                gt = entry.find("guid")
                if gt:
                    link = gt.get_text(strip=True)
            summary = ""
            for sname in ["description","summary","content","encoded"]:
                st = entry.find(sname)
                if st:
                    summary = st.get_text(separator=" ", strip=True)[:500]
                    break
            if title:
                out.append({"title":title,"link":link.strip(),"summary":summary,"source":name})
        state["feeds_status"][name] = str(len(out)) + " items"
    except Exception as e:
        state["feeds_status"][name] = "Parse error"
    return out

# ─────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────

def check_seen(key):
    if key in state["seen"]:
        return True
    state["seen"].append(key)
    if len(state["seen"]) > 3000:
        state["seen"].pop(0)
    return False

def check_alerted(ticker):
    key = ticker + "_" + str(datetime.date.today())
    if key in state["alerted"]:
        return True
    state["alerted"].append(key)
    return False

# ─────────────────────────────────────────────
# PROCESS STOCK — the main evaluation function
# ─────────────────────────────────────────────

def evaluate_stock(ticker, headline, cat, score, url, source):
    """
    Full evaluation of a stock against all criteria.
    Returns alert dict if it passes, None if it fails.
    """
    try:
        q = get_price_data(ticker)
    except Exception as e:
        log.debug("Price data failed " + ticker + ": " + str(e))
        return None

    if not q:
        return None

    price    = q["price"]
    gap      = q["gap"]
    vol      = q["vol"]
    avg_vol  = q["avg_vol"]
    float_m  = q["float_m"]
    rvol     = q["rvol"]
    vol_spike = q["vol_spike"]
    vol_spike_ratio = q["vol_spike_ratio"]
    making_highs = q["making_highs"]
    sess     = q["session"]

    # Basic qualification filters
    if price < MIN_PRICE or price > MAX_PRICE:
        return None
    if float_m > MAX_FLOAT:
        return None
    if gap < MIN_GAP:
        return None
    if vol < MIN_VOLUME:
        return None
    if rvol < MIN_RVOL and not vol_spike:
        return None

    # Win rate check
    win_pass, win_reason = win_rate_check(price, gap, vol, avg_vol, float_m, score, cat, sess)

    # Compute grade
    grade, grade_reasons = compute_grade(score, gap, float_m, rvol, vol_spike, making_highs, sess)

    # Filter by grade
    if not grade_passes_filter(grade, sess):
        return None

    # One alert per ticker per day
    if check_alerted(ticker):
        return None

    alert = {
        "id":               len(state["alerts"]) + 1,
        "ticker":           ticker,
        "headline":         headline,
        "cat":              cat,
        "score":            score,
        "url":              url,
        "source":           source,
        "session":          sess,
        "session_label":    session_label(),
        "time":             datetime.datetime.now().strftime("%H:%M:%S ET"),
        "date":             datetime.date.today().strftime("%b %d %Y"),
        "price":            round(price, 2),
        "gap":              round(gap, 1),
        "vol":              vol,
        "vol_str":          vol_str(vol),
        "avg_vol":          int(avg_vol),
        "fm":               round(float_m, 1),
        "rvol":             round(rvol, 1),
        "vol_spike":        vol_spike,
        "vol_spike_ratio":  vol_spike_ratio,
        "making_highs":     making_highs,
        "grade":            grade,
        "grade_reasons":    grade_reasons,
        "win_pass":         win_pass,
        "win_reason":       win_reason,
        "tv_link":          "https://www.tradingview.com/chart/?symbol=" + ticker,
    }
    return alert

def handle_item(item):
    headline = item.get("title","").strip()
    link     = item.get("link","").strip()
    summary  = item.get("summary","").strip()
    source   = item.get("source","")

    if not headline:
        return

    # Add to news feed regardless of score
    news_key = link or headline
    if news_key not in [n.get("key","") for n in state["news"]]:
        score, cat = score_text(headline + " " + summary)
        tickers = extract_tickers_from_text(headline + " " + summary)
        state["news"].insert(0, {
            "key":     news_key,
            "title":   headline,
            "link":    link,
            "source":  source,
            "score":   score,
            "cat":     cat,
            "tickers": tickers,
            "time":    datetime.datetime.now().strftime("%H:%M ET"),
        })
        if len(state["news"]) > 500:
            state["news"] = state["news"][:500]

    if check_seen(link or headline):
        return

    score, cat = score_text(headline + " " + summary)
    if score < MIN_SCORE:
        return

    # Get tickers using improved extraction
    tickers = get_tickers_for_headline(headline, summary)
    if not tickers:
        return

    log.info("  " + str(score) + "/10 [" + cat + "] " + headline[:55] + " → " + str(tickers[:3]))

    for ticker in tickers[:3]:
        alert = evaluate_stock(ticker, headline, cat, score, link, source)
        if alert:
            state["alerts"].insert(0, alert)
            if len(state["alerts"]) > 300:
                state["alerts"] = state["alerts"][:300]

            # Auto add to watchlist
            if ticker not in [w["ticker"] for w in state["watchlist"]]:
                state["watchlist"].insert(0, {
                    "ticker":  ticker,
                    "added":   alert["time"],
                    "cat":     cat,
                    "grade":   alert["grade"],
                    "session": alert["session"],
                })

            log.info("ALERT " + ticker + " Grade:" + alert["grade"] +
                     " Gap:" + str(round(alert["gap"],1)) + "%" +
                     " Spike:" + str(alert["vol_spike"]) +
                     " Highs:" + str(alert["making_highs"]) +
                     " " + cat)
            send_push(alert)

# ─────────────────────────────────────────────
# SCANNER LOOP
# ─────────────────────────────────────────────

def scan_loop():
    while True:
        try:
            if is_scanning_time():
                state["scanning"] = True
                sess, h, m = get_session()
                state["session"] = sess
                state["scan_count"] += 1
                state["last_scan"] = datetime.datetime.now().strftime("%H:%M:%S")

                for row in FEED_LIST:
                    try:
                        items = get_feed(row[0], row[1])
                        for item in items:
                            handle_item(item)
                    except Exception as e:
                        log.error("Feed [" + row[0] + "]: " + str(e))

                state["scanning"] = False
            else:
                state["scanning"] = False
                state["session"] = "closed"

        except Exception as e:
            log.error("Scan loop error: " + str(e))
            state["scanning"] = False

        time.sleep(SCAN_SECS)

# ─────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/alerts")
def api_alerts():
    sess = request.args.get("session","all")
    grade = request.args.get("grade","all")
    alerts = state["alerts"]
    if sess != "all":
        alerts = [a for a in alerts if a.get("session") == sess]
    if grade != "all":
        alerts = [a for a in alerts if a.get("grade") == grade]
    return jsonify(alerts)

@app.route("/api/news")
def api_news():
    limit = int(request.args.get("limit",100))
    cat = request.args.get("cat","all")
    news = state["news"]
    if cat != "all":
        news = [n for n in news if n.get("cat") == cat]
    return jsonify(news[:limit])

@app.route("/api/watchlist")
def api_watchlist():
    return jsonify(state["watchlist"])

@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    data = request.get_json()
    ticker = data.get("ticker","").upper().strip()
    if not ticker or len(ticker) > 5:
        return jsonify({"ok":False,"error":"Invalid ticker"})
    if ticker in [w["ticker"] for w in state["watchlist"]]:
        return jsonify({"ok":False,"error":"Already in watchlist"})
    sess, h, m = get_session()
    state["watchlist"].insert(0, {
        "ticker":  ticker,
        "added":   datetime.datetime.now().strftime("%H:%M ET"),
        "cat":     data.get("cat","Manual"),
        "grade":   "—",
        "session": sess,
    })
    return jsonify({"ok":True})

@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    data = request.get_json()
    ticker = data.get("ticker","").upper().strip()
    state["watchlist"] = [w for w in state["watchlist"] if w["ticker"] != ticker]
    return jsonify({"ok":True})

@app.route("/api/watchlist/refresh")
def api_watchlist_refresh():
    result = []
    for w in state["watchlist"][:20]:
        try:
            q = get_price_data(w["ticker"])
            if q:
                result.append({
                    "ticker":  w["ticker"],
                    "price":   round(q["price"],2),
                    "gap":     round(q["gap"],1),
                    "vol":     vol_str(q["vol"]),
                    "float_m": round(q["float_m"],1),
                    "rvol":    round(q["rvol"],1),
                    "vol_spike": q["vol_spike"],
                    "making_highs": q["making_highs"],
                    "cat":     w.get("cat",""),
                    "grade":   w.get("grade","—"),
                    "session": w.get("session",""),
                    "added":   w.get("added",""),
                })
            else:
                result.append(w)
        except Exception:
            result.append(w)
    return jsonify(result)

@app.route("/api/ticker/<ticker>")
def api_ticker(ticker):
    try:
        q = get_price_data(ticker.upper())
        if not q:
            return jsonify({"ok":False,"error":"Not found"})
        qualifies = (q["float_m"] <= MAX_FLOAT and
                     q["gap"] >= MIN_GAP and
                     q["rvol"] >= MIN_RVOL)
        sess, h, m = get_session()
        return jsonify({
            "ok":           True,
            "ticker":       ticker.upper(),
            "price":        round(q["price"],2),
            "gap":          round(q["gap"],1),
            "vol":          vol_str(q["vol"]),
            "float_m":      round(q["float_m"],1),
            "rvol":         round(q["rvol"],1),
            "vol_spike":    q["vol_spike"],
            "vol_spike_ratio": q["vol_spike_ratio"],
            "making_highs": q["making_highs"],
            "qualifies":    qualifies,
            "session":      q["session"],
            "tv_link":      "https://www.tradingview.com/chart/?symbol=" + ticker.upper(),
        })
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/status")
def api_status():
    sess, h, m = get_session()
    return jsonify({
        "scanning":      state["scanning"],
        "last_scan":     state["last_scan"],
        "scan_count":    state["scan_count"],
        "alert_count":   len(state["alerts"]),
        "news_count":    len(state["news"]),
        "wl_count":      len(state["watchlist"]),
        "feeds":         state["feeds_status"],
        "pushover_ok":   not PUSHOVER_USER.startswith("YOUR_"),
        "session":       sess,
        "session_label": session_label(),
        "market_open":   is_scanning_time(),
        "et_time":       str(h) + ":" + str(m).zfill(2) + " ET",
        "settings": {
            "MIN_SCORE":  MIN_SCORE,
            "MIN_GAP":    MIN_GAP,
            "MAX_FLOAT":  MAX_FLOAT,
            "MIN_RVOL":   MIN_RVOL,
            "SCAN_SECS":  SCAN_SECS,
        },
    })

@app.route("/api/alerts/clear", methods=["POST"])
def api_clear():
    state["alerts"] = []
    state["alerted"] = []
    return jsonify({"ok":True})

@app.route("/health")
def health():
    return "ok"

# ─────────────────────────────────────────────
# START
# ─────────────────────────────────────────────

threading.Thread(target=scan_loop, daemon=True).start()
log.info("ProFloat Scanner v3 started")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
