from flask import Flask, Response, jsonify, request
import requests as req
import time, datetime, threading, logging, re, os
from bs4 import BeautifulSoup

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("app")

PUSHOVER_USER  = os.environ.get("PUSHOVER_USER",  "YOUR_PUSHOVER_USER_TOKEN")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "YOUR_PUSHOVER_APP_TOKEN")
PUSHOVER_URL   = "https://api.pushover.net/1/messages.json"
MIN_SCORE  = int(os.environ.get("MIN_SCORE",   "5"))
MIN_GAP    = float(os.environ.get("MIN_GAP",   "10.0"))
MAX_FLOAT  = float(os.environ.get("MAX_FLOAT", "10.0"))
MIN_PRICE  = float(os.environ.get("MIN_PRICE",  "0.30"))
MAX_PRICE  = float(os.environ.get("MAX_PRICE", "50.0"))
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", "50000"))
MIN_RVOL   = float(os.environ.get("MIN_RVOL",  "1.5"))
SCAN_SECS  = int(os.environ.get("SCAN_SECS",  "60"))
BROWSER    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"

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

SKIP = ["INC","LLC","CORP","LTD","THE","AND","FOR","SEC","ACT","NEW","COM","NET","US","USA","FDA","CEO","CFO","COO","IPO","ETF","NYSE","NASDAQ","AM","PM","EST","ET","AI","EV","UK","EU","UN","WHO","DOD","DOE","NASA","M","B","Q","A","AN","IN","OF","TO","BY","ON","AS","AT","HIGH","LOW","TOP","HOT","KEY","EPS","NDA","BLA","CRL","RX","PR","TV","FM","RP","PO","SA","AG"]

state = {"alerts":[],"news":[],"watchlist":[],"seen":[],"alerted":[],"feeds_status":{},"scanning":False,"last_scan":None,"scan_count":0,"session":"closed"}

def get_session():
    now = datetime.datetime.utcnow()
    h = (now.hour - 4) % 24
    m = now.minute
    if h >= 4 and (h < 9 or (h == 9 and m < 30)): return "premarket", h, m
    if (h == 9 and m >= 30) or (10 <= h < 16): return "regular", h, m
    if 16 <= h < 20: return "afterhours", h, m
    return "closed", h, m

def is_scanning():
    s, h, m = get_session()
    return s != "closed"

def sess_label():
    s, h, m = get_session()
    return {"premarket":"PRE MARKET","regular":"MARKET OPEN","afterhours":"AFTER HOURS","closed":"MARKET CLOSED"}.get(s, "UNKNOWN")

def has(t, words):
    for w in words:
        if w in t: return True
    return False

def score_text(text):
    t = text.lower()
    score = 0
    name = "General News"
    if has(t,["fda","food and drug"]) and has(t,["approv","cleared","breakthrough","nda","bla","510k","pdufa","granted"]):
        score = 3 if has(t,["reject","refus","crl","clinical hold"]) else 10
        name = "FDA Approval"
    if score < 8 and has(t,["phase","trial","endpoint","readout","topline"]) and has(t,["positive","success","met","significant","strong","favorable"]):
        if not has(t,["failed","miss","negative","halt"]): score = 8; name = "Clinical Trial Win"
    if score < 9 and has(t,["acqui","merger","takeover","buyout","definitive agreement","tender offer","going private"]):
        score = 9; name = "Merger Acquisition"
    if score < 8 and has(t,["contract","award","awarded","selected","procurement"]) and has(t,["pentagon","military","army","navy","air force","department of defense","government","federal","nasa","darpa"]):
        score = 8; name = "Government Contract"
    if score < 7 and has(t,["earnings","eps","revenue","quarterly","q1","q2","q3","q4"]) and has(t,["beat","exceed","surpass","above","better than expected","record","topped"]):
        if not has(t,["miss","below","disappoint"]): score = 7; name = "Earnings Beat"
    if score < 7 and has(t,["short squeeze","short interest","heavily shorted","most shorted","gamma squeeze","unusual options"]):
        score = 7; name = "Short Squeeze"
    if score < 7 and has(t,["defense","defence","weapon","missile","drone","ammunition","warfare"]) and has(t,["war","conflict","escalat","nato","sanction","surge","spending"]):
        score = 7; name = "Defence Surge"
    if score < 6 and has(t,["oil","natural gas","crude","lithium","uranium","opec","lng"]) and has(t,["surge","spike","soar","war","conflict","sanction","shortage"]):
        score = 6; name = "Energy Surge"
    if score < 6 and has(t,["licensing","collaboration","joint venture","exclusive agreement","distribution agreement"]):
        if not has(t,["terminat","cancel","dissolv"]): score = 6; name = "Partnership Deal"
    if score == 0: return 0, "General News"
    if has(t,["billion","landmark","historic","first ever","pivotal"]): score = min(10, score + 1)
    if has(t,["bankrupt","chapter 11","going concern","delist","lawsuit"]): score = max(1, score - 2)
    return score, name

def get_tickers(text):
    found = []
    for m in re.findall(r'\$([A-Z]{1,5})\b', text): found.append(m)
    for m in re.findall(r'\(([A-Z]{1,5})\)', text):
        if len(m) >= 2: found.append(m)
    for m in re.findall(r'(?:NYSE|NASDAQ|Nasdaq)[\s:]+([A-Z]{1,5})\b', text): found.append(m)
    seen = []; out = []
    for t in found:
        if t not in SKIP and t not in seen and len(t) >= 2: seen.append(t); out.append(t)
    return out

def search_yahoo(company):
    if not company or len(company) < 5: return []
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search?q=" + req.utils.quote(company) + "&quotesCount=3&newsCount=0"
        r = req.get(url, headers={"User-Agent": BROWSER}, timeout=8)
        if r.status_code != 200: return []
        results = r.json().get("quotes", [])
        tickers = []
        for res in results[:3]:
            sym = res.get("symbol",""); qt = res.get("quoteType",""); ex = res.get("exchange","")
            if qt == "EQUITY" and len(sym) <= 5 and "." not in sym:
                if ex in ["NMS","NYQ","NGM","NCM","ASE","PCX","BTS","OTC"]: tickers.append(sym)
        return tickers
    except Exception: return []

def extract_company(headline):
    patterns = [r'^([A-Z][a-zA-Z\s&]+(?:Inc|Corp|Ltd|LLC|Co|Therapeutics|Pharma|Bio|Tech|Sciences|Holdings|Group|Medical|Health)\.?)',r'([A-Z][a-zA-Z\s&]+(?:Inc|Corp|Ltd|LLC|Co|Therapeutics|Pharma|Bio|Tech|Sciences|Holdings|Group|Medical|Health)\.?)\s+(?:announces|reports|receives|granted|awarded)']
    for p in patterns:
        match = re.search(p, headline)
        if match:
            name = match.group(1).strip()
            if 5 <= len(name) <= 60: return name
    return ""

def find_tickers(headline, summary):
    full = headline + " " + summary
    tickers = get_tickers(full)
    if tickers: return tickers
    company = extract_company(headline)
    if company:
        yahoo = search_yahoo(company)
        if yahoo: return yahoo
    return []

def get_price(ticker):
    hdrs = {"User-Agent": BROWSER}
    sess, h, m = get_session()
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker + "?interval=1d&range=5d&includePrePost=true"
        r = req.get(url, headers=hdrs, timeout=10)
        if r.status_code != 200: return None
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", 0)
        prev  = meta.get("chartPreviousClose", 0)
        vol   = meta.get("regularMarketVolume", 0)
        avg   = meta.get("averageDailyVolume10Day", vol) or vol
        if sess == "premarket":
            pre = meta.get("preMarketPrice") or price
            if pre: price = pre
        elif sess == "afterhours":
            post = meta.get("postMarketPrice") or price
            if post: price = post
        gap = ((price - prev) / prev * 100.0) if prev else 0.0
    except Exception: return None
    fm = 99.0
    try:
        u2 = "https://query1.finance.yahoo.com/v11/finance/quoteSummary/" + ticker + "?modules=defaultKeyStatistics"
        r2 = req.get(u2, headers=hdrs, timeout=8)
        if r2.status_code == 200:
            raw = r2.json()["quoteSummary"]["result"][0]["defaultKeyStatistics"].get("floatShares",{}).get("raw",None)
            if raw: fm = raw / 1000000.0
    except Exception: pass
    vol_spike = False; vol_spike_ratio = 0.0; making_highs = False
    try:
        u3 = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker + "?interval=5m&range=1d&includePrePost=true"
        r3 = req.get(u3, headers=hdrs, timeout=10)
        if r3.status_code == 200:
            res3 = r3.json()["chart"]["result"]
            if res3:
                q3 = res3[0].get("indicators",{}).get("quote",[{}])[0]
                vols  = [v for v in q3.get("volume",[]) if v is not None and v > 0]
                highs = [x for x in q3.get("high",[])   if x is not None and x > 0]
                if len(vols) >= 6:
                    ra = sum(vols[-3:]) / 3; ha = sum(vols[:-3]) / max(len(vols)-3, 1)
                    if ha > 0: vol_spike_ratio = ra / ha; vol_spike = vol_spike_ratio >= 3.0
                if len(highs) >= 6:
                    rh = max(highs[-3:]); eh = max(highs[-9:-3]) if len(highs) >= 9 else max(highs[:-3])
                    making_highs = rh >= eh * 0.99
    except Exception: pass
    rvol = vol / avg if avg > 0 else 0
    return {"price":price,"gap":gap,"vol":int(vol),"avg":int(avg),"float_m":fm,"rvol":rvol,"vol_spike":vol_spike,"vol_spike_ratio":round(vol_spike_ratio,1),"making_highs":making_highs,"session":sess}

def vol_str(v):
    if v >= 1000000: return str(round(v/1000000.0,1)) + "M"
    if v >= 1000: return str(int(v/1000)) + "K"
    return str(v)

def compute_grade(score, gap, fm, rvol, vol_spike, making_highs):
    pts = 0; reasons = []
    if score >= 7:   pts += 1; reasons.append("Score " + str(score) + "/10")
    if gap >= 20:    pts += 1; reasons.append("Gap +" + str(round(gap,1)) + "%")
    if fm <= 5:      pts += 1; reasons.append("Float " + str(round(fm,1)) + "M")
    if rvol >= 5:    pts += 1; reasons.append("RVOL " + str(round(rvol,1)) + "x")
    if vol_spike:    pts += 1; reasons.append("Vol spike now")
    if making_highs: pts += 1; reasons.append("New highs")
    if pts >= 6: return "A+", reasons
    if pts >= 5: return "A",  reasons
    if pts >= 4: return "B",  reasons
    if pts >= 3: return "C",  reasons
    return "D", reasons

def send_push(alert):
    if PUSHOVER_USER.startswith("YOUR_"): return
    g = alert["grade"]; sess = alert.get("session","regular")
    pri = 1 if g in ["A+","A"] else 0 if g == "B" else -1
    snd = "siren" if g in ["A+","A"] else "bugle" if g == "B" else "pushover"
    pre = {"premarket":"[PRE] ","afterhours":"[AH] ","regular":""}.get(sess,"")
    title = pre + alert["ticker"] + " Grade " + g + " +" + str(round(alert["gap"])) + "% " + alert["cat"]
    body = (alert["cat"] + "\n\n" + alert["headline"][:140] + "\n\n" + "$" + str(alert["price"]) + " Gap +" + str(alert["gap"]) + "%" + "\nVol " + alert["vol_str"] + " Float " + str(alert["fm"]) + "M" + "\nRVOL " + str(alert["rvol"]) + "x Score " + str(alert["score"]) + "/10" + "\nSpike: " + str(alert["vol_spike"]) + " Highs: " + str(alert["making_highs"]) + "\n" + alert["time"])
    data = {"token":PUSHOVER_TOKEN,"user":PUSHOVER_USER,"title":title,"message":body,"priority":pri,"sound":snd,"url":alert.get("url",""),"url_title":"Read article"}
    try:
        r = req.post(PUSHOVER_URL, data=data, timeout=10)
        if r.json().get("status") == 1: log.info("Push sent " + alert["ticker"] + " Grade " + g)
    except Exception as e: log.warning("Push failed: " + str(e))

def get_feed(name, url):
    hdrs = {"User-Agent": BROWSER, "Accept": "text/html,*/*"}
    try:
        r = req.get(url, headers=hdrs, timeout=15)
        if r.status_code >= 400: state["feeds_status"][name] = "HTTP " + str(r.status_code); return []
    except Exception: state["feeds_status"][name] = "Error"; return []
    out = []
    try:
        soup = BeautifulSoup(r.content, "html.parser")
        for entry in soup.find_all(["item","entry"]):
            tt = entry.find("title")
            if not tt: continue
            title = tt.get_text(separator=" ", strip=True)
            link = ""
            lt = entry.find("link")
            if lt: link = lt.get("href","") or lt.get_text(strip=True)
            if not link:
                gt = entry.find("guid")
                if gt: link = gt.get_text(strip=True)
            summary = ""
            for sn in ["description","summary","content","encoded"]:
                st = entry.find(sn)
                if st: summary = st.get_text(separator=" ", strip=True)[:500]; break
            if title: out.append({"title":title,"link":link.strip(),"summary":summary,"source":name})
        state["feeds_status"][name] = str(len(out)) + " items"
    except Exception: state["feeds_status"][name] = "Parse error"
    return out

def check_seen(key):
    if key in state["seen"]: return True
    state["seen"].append(key)
    if len(state["seen"]) > 3000: state["seen"].pop(0)
    return False

def check_alerted(ticker):
    key = ticker + "_" + str(datetime.date.today())
    if key in state["alerted"]: return True
    state["alerted"].append(key)
    return False

def handle_item(item):
    headline = item.get("title","").strip()
    link     = item.get("link","").strip()
    summary  = item.get("summary","").strip()
    source   = item.get("source","")
    if not headline: return
    news_key = link or headline
    if news_key not in [n.get("key","") for n in state["news"]]:
        score, cat = score_text(headline + " " + summary)
        tickers = get_tickers(headline + " " + summary)
        state["news"].insert(0,{"key":news_key,"title":headline,"link":link,"source":source,"score":score,"cat":cat,"tickers":tickers,"time":datetime.datetime.now().strftime("%H:%M ET")})
        if len(state["news"]) > 500: state["news"] = state["news"][:500]
    if check_seen(link or headline): return
    score, cat = score_text(headline + " " + summary)
    if score < MIN_SCORE: return
    tickers = find_tickers(headline, summary)
    if not tickers: return
    log.info("  " + str(score) + "/10 [" + cat + "] " + headline[:55])
    for ticker in tickers[:3]:
        try:
            q = get_price(ticker)
            if not q: continue
            price = q["price"]; gap = q["gap"]; vol = q["vol"]; avg = q["avg"]; fm = q["float_m"]; rvol = q["rvol"]; sess = q["session"]
            if price < MIN_PRICE or price > MAX_PRICE: continue
            if fm > MAX_FLOAT: continue
            if gap < MIN_GAP: continue
            if vol < MIN_VOLUME: continue
            if rvol < MIN_RVOL and not q["vol_spike"]: continue
            grade, reasons = compute_grade(score, gap, fm, rvol, q["vol_spike"], q["making_highs"])
            if grade == "D": continue
            if check_alerted(ticker): continue
            alert = {"id":len(state["alerts"])+1,"ticker":ticker,"headline":headline,"cat":cat,"score":score,"url":link,"source":source,"session":sess,"time":datetime.datetime.now().strftime("%H:%M:%S ET"),"date":datetime.date.today().strftime("%b %d %Y"),"price":round(price,2),"gap":round(gap,1),"vol":vol,"vol_str":vol_str(vol),"fm":round(fm,1),"rvol":round(rvol,1),"vol_spike":q["vol_spike"],"vol_spike_ratio":q["vol_spike_ratio"],"making_highs":q["making_highs"],"grade":grade,"reasons":reasons,"tv_link":"https://www.tradingview.com/chart/?symbol="+ticker}
            state["alerts"].insert(0, alert)
            if len(state["alerts"]) > 300: state["alerts"] = state["alerts"][:300]
            if ticker not in [w["ticker"] for w in state["watchlist"]]:
                state["watchlist"].insert(0,{"ticker":ticker,"added":alert["time"],"cat":cat,"grade":grade,"session":sess})
            log.info("ALERT " + ticker + " Grade:" + grade + " Gap:" + str(round(gap,1)) + "% " + cat)
            send_push(alert)
        except Exception as e: log.debug("Error " + ticker + ": " + str(e))

def scan_loop():
    while True:
        try:
            if is_scanning():
                state["scanning"] = True; state["scan_count"] += 1; state["last_scan"] = datetime.datetime.now().strftime("%H:%M:%S")
                sess, h, m = get_session(); state["session"] = sess
                for row in FEED_LIST:
                    try:
                        items = get_feed(row[0], row[1])
                        for item in items: handle_item(item)
                    except Exception as e: log.error("Feed [" + row[0] + "]: " + str(e))
                state["scanning"] = False
            else:
                state["scanning"] = False; state["session"] = "closed"
        except Exception as e: log.error("Scan loop: " + str(e)); state["scanning"] = False
        time.sleep(SCAN_SECS)

@app.route("/")
def index():
    return Response(HTML.encode("ascii"), mimetype="text/html; charset=utf-8")

@app.route("/api/alerts")
def api_alerts():
    sess = request.args.get("session","all"); grade = request.args.get("grade","all"); data = state["alerts"]
    if sess  != "all": data = [a for a in data if a.get("session") == sess]
    if grade != "all": data = [a for a in data if a.get("grade")   == grade]
    return jsonify(data)

@app.route("/api/news")
def api_news():
    cat = request.args.get("cat","all"); data = state["news"]
    if cat != "all": data = [n for n in data if n.get("cat") == cat]
    return jsonify(data[:100])

@app.route("/api/watchlist")
def api_watchlist():
    return jsonify(state["watchlist"])

@app.route("/api/watchlist/add", methods=["POST"])
def api_wl_add():
    d = request.get_json(); t = d.get("ticker","").upper().strip()
    if not t or len(t) > 5: return jsonify({"ok":False,"error":"Invalid ticker"})
    if t in [w["ticker"] for w in state["watchlist"]]: return jsonify({"ok":False,"error":"Already in watchlist"})
    sess, h, m = get_session()
    state["watchlist"].insert(0,{"ticker":t,"added":datetime.datetime.now().strftime("%H:%M ET"),"cat":d.get("cat","Manual"),"grade":"--","session":sess})
    return jsonify({"ok":True})

@app.route("/api/watchlist/remove", methods=["POST"])
def api_wl_remove():
    d = request.get_json(); t = d.get("ticker","").upper().strip()
    state["watchlist"] = [w for w in state["watchlist"] if w["ticker"] != t]
    return jsonify({"ok":True})

@app.route("/api/watchlist/refresh")
def api_wl_refresh():
    result = []
    for w in state["watchlist"][:20]:
        try:
            q = get_price(w["ticker"])
            if q:
                result.append({"ticker":w["ticker"],"price":round(q["price"],2),"gap":round(q["gap"],1),"vol":vol_str(q["vol"]),"float_m":round(q["float_m"],1),"rvol":round(q["rvol"],1),"vol_spike":q["vol_spike"],"making_highs":q["making_highs"],"cat":w.get("cat",""),"grade":w.get("grade","--"),"session":w.get("session",""),"added":w.get("added","")})
            else: result.append(w)
        except Exception: result.append(w)
    return jsonify(result)

@app.route("/api/ticker/<ticker>")
def api_ticker(ticker):
    try:
        q = get_price(ticker.upper())
        if not q: return jsonify({"ok":False,"error":"Not found"})
        qualifies = q["float_m"] <= MAX_FLOAT and q["gap"] >= MIN_GAP and q["rvol"] >= MIN_RVOL
        return jsonify({"ok":True,"ticker":ticker.upper(),"price":round(q["price"],2),"gap":round(q["gap"],1),"vol":vol_str(q["vol"]),"float_m":round(q["float_m"],1),"rvol":round(q["rvol"],1),"vol_spike":q["vol_spike"],"vol_spike_ratio":q["vol_spike_ratio"],"making_highs":q["making_highs"],"qualifies":qualifies,"session":q["session"],"tv_link":"https://www.tradingview.com/chart/?symbol="+ticker.upper()})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/status")
def api_status():
    sess, h, m = get_session()
    return jsonify({"scanning":state["scanning"],"last_scan":state["last_scan"],"scan_count":state["scan_count"],"alert_count":len(state["alerts"]),"news_count":len(state["news"]),"wl_count":len(state["watchlist"]),"feeds":state["feeds_status"],"pushover_ok":not PUSHOVER_USER.startswith("YOUR_"),"session":sess,"session_label":sess_label(),"market_open":is_scanning(),"settings":{"MIN_SCORE":MIN_SCORE,"MIN_GAP":MIN_GAP,"MAX_FLOAT":MAX_FLOAT,"MIN_RVOL":MIN_RVOL,"SCAN_SECS":SCAN_SECS}})

@app.route("/api/alerts/clear", methods=["POST"])
def api_clear():
    state["alerts"] = []; state["alerted"] = []
    return jsonify({"ok":True})

@app.route("/health")
def health():
    return "ok"

threading.Thread(target=scan_loop, daemon=True).start()
log.info("ProFloat Scanner v3 started")


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>ProFloat v3</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#07090d;--bg2:#0c0f16;--bg3:#12161f;--bg4:#181d28;--border:#1c2030;--border2:#252d40;--text:#dde4f0;--muted:#4a5568;--muted2:#6b7a95;--accent:#3b82f6;--cyan:#06b6d4;--green:#10b981;--yellow:#f59e0b;--red:#ef4444;--orange:#f97316;--purple:#8b5cf6;--ap:#00ff88;--a:#10b981;--b:#3b82f6;--c:#f59e0b;--d:#4a5568;}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
html,body{height:100%;overflow:hidden;}
body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;}
.app{display:flex;flex-direction:column;height:100vh;}
.topbar{display:flex;align-items:center;gap:8px;padding:9px 14px;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;}
.logo{font-size:15px;font-weight:800;letter-spacing:-.5px;white-space:nowrap;}
.logo span{color:var(--cyan);}
.sess-badge{padding:3px 9px;border-radius:20px;font-size:10px;font-family:'JetBrains Mono',monospace;font-weight:700;border:1px solid;letter-spacing:.04em;white-space:nowrap;}
.sb-premarket{background:#8b5cf620;color:#8b5cf6;border-color:#8b5cf640;}
.sb-regular{background:#10b98120;color:#10b981;border-color:#10b98140;}
.sb-afterhours{background:#f9731620;color:#f97316;border-color:#f9731640;}
.sb-closed{background:#4a556820;color:#6b7a95;border-color:#1c2030;}
.topbar-right{display:flex;gap:6px;align-items:center;margin-left:auto;}
.pill{display:flex;align-items:center;gap:5px;padding:4px 9px;border-radius:20px;font-size:10px;font-family:'JetBrains Mono',monospace;background:var(--bg3);border:1px solid var(--border);white-space:nowrap;}
.dot{width:6px;height:6px;border-radius:50%;background:var(--muted);}
.dot.on{background:var(--green);box-shadow:0 0 5px var(--green);animation:blink 2s infinite;}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.sound-btn{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;font-size:10px;font-family:'JetBrains Mono',monospace;border:1px solid var(--border);background:var(--bg3);cursor:pointer;transition:all .15s;white-space:nowrap;}
.sound-btn.on{background:#10b98120;color:var(--green);border-color:#10b98140;}
.sound-btn.off{background:#ef444420;color:var(--red);border-color:#ef444440;}
.nav{display:flex;gap:2px;padding:7px 14px 0;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;overflow-x:auto;}
.nav::-webkit-scrollbar{display:none;}
.nav-tab{padding:7px 13px;border-radius:8px 8px 0 0;font-size:11px;font-weight:600;cursor:pointer;color:var(--muted2);border:1px solid transparent;border-bottom:none;white-space:nowrap;transition:all .15s;background:none;}
.nav-tab.active{color:var(--text);background:var(--bg);border-color:var(--border);border-bottom:1px solid var(--bg);margin-bottom:-1px;}
.nbadge{display:inline-block;margin-left:4px;background:var(--accent);color:#fff;font-size:9px;font-family:'JetBrains Mono',monospace;padding:1px 5px;border-radius:8px;}
.content{flex:1;overflow:hidden;}
.page{display:none;height:100%;flex-direction:column;}
.page.active{display:flex;}
.scroll{flex:1;overflow-y:auto;padding:12px 14px;}
.scroll::-webkit-scrollbar{width:3px;}
.scroll::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px;}
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px;}
.stat-box{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:10px 12px;}
.s-lbl{font-size:9px;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;font-family:'JetBrains Mono',monospace;margin-bottom:4px;}
.s-val{font-size:20px;font-weight:800;line-height:1;}
.filter-row{display:flex;gap:5px;margin-bottom:10px;flex-wrap:wrap;}
.fp{padding:4px 11px;border-radius:20px;border:1px solid var(--border);background:var(--bg2);color:var(--muted2);font-size:10px;cursor:pointer;font-family:'Syne',sans-serif;font-weight:600;transition:all .15s;white-space:nowrap;}
.fp.active{background:var(--accent);color:#fff;border-color:var(--accent);}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:10px;}
.card-hdr{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--border);}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;}
.alert-item{padding:12px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s;animation:fadeIn .3s ease;}
.alert-item:hover{background:var(--bg3);}
.alert-item:last-child{border-bottom:none;}
@keyframes fadeIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
.alert-row1{display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap;}
.ticker{font-size:17px;font-weight:800;letter-spacing:-.5px;}
.gbadge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;font-family:'JetBrains Mono',monospace;border:1px solid;}
.gAP{background:#00ff8820;color:#00ff88;border-color:#00ff8840;}
.gA{background:#10b98120;color:#10b981;border-color:#10b98140;}
.gB{background:#3b82f620;color:#3b82f6;border-color:#3b82f640;}
.gC{background:#f59e0b20;color:#f59e0b;border-color:#f59e0b40;}
.gD{background:#4a556820;color:#4a5568;border-color:#1c2030;}
.cat-tag{font-size:10px;color:var(--muted2);font-family:'JetBrains Mono',monospace;background:var(--bg3);padding:2px 7px;border-radius:4px;border:1px solid var(--border);}
.stag{font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;}
.st-premarket{background:#8b5cf615;color:#8b5cf6;}
.st-regular{background:#10b98115;color:#10b981;}
.st-afterhours{background:#f9731615;color:#f97316;}
.a-gap{margin-left:auto;font-size:14px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--green);}
.a-hl{font-size:11px;color:var(--muted2);line-height:1.5;margin-bottom:7px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.a-metrics{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
.metric{display:flex;flex-direction:column;gap:1px;}
.m-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;font-family:'JetBrains Mono',monospace;}
.m-val{font-size:11px;font-weight:600;font-family:'JetBrains Mono',monospace;}
.spike-on{font-size:9px;padding:2px 6px;border-radius:3px;font-family:'JetBrains Mono',monospace;background:#10b98120;color:#10b981;border:1px solid #10b98130;}
.spike-off{font-size:9px;padding:2px 6px;border-radius:3px;font-family:'JetBrains Mono',monospace;background:#4a556815;color:var(--muted);border:1px solid var(--border);}
.highs-on{font-size:9px;padding:2px 6px;border-radius:3px;font-family:'JetBrains Mono',monospace;background:#3b82f620;color:#3b82f6;border:1px solid #3b82f630;}
.highs-off{font-size:9px;padding:2px 6px;border-radius:3px;font-family:'JetBrains Mono',monospace;background:#4a556815;color:var(--muted);border:1px solid var(--border);}
.grade-reasons{font-size:9px;color:var(--muted2);font-family:'JetBrains Mono',monospace;margin-top:5px;line-height:1.6;}
.a-time{font-size:9px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-top:5px;}
.tv-link{font-size:10px;color:var(--accent);font-family:'JetBrains Mono',monospace;text-decoration:none;margin-left:auto;}
.wl-add{display:flex;gap:7px;padding:10px 14px;border-bottom:1px solid var(--border);}
.wl-input{flex:1;background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:7px 11px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;text-transform:uppercase;outline:none;}
.wl-input:focus{border-color:var(--accent);}
.wl-input::placeholder{color:var(--muted);text-transform:none;}
.btn{padding:7px 14px;border-radius:8px;border:none;font-family:'Syne',sans-serif;font-weight:700;font-size:11px;cursor:pointer;transition:all .15s;}
.btn-p{background:var(--accent);color:#fff;}
.btn-sm{padding:5px 10px;font-size:10px;}
.btn-g{background:var(--bg3);color:var(--muted2);border:1px solid var(--border);}
.btn-g:hover{color:var(--red);border-color:var(--red);}
.wl-item{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--border);}
.wl-item:last-child{border-bottom:none;}
.wl-tick{font-size:14px;font-weight:800;min-width:52px;}
.wl-data{display:flex;gap:9px;flex:1;flex-wrap:wrap;}
.wl-col{display:flex;flex-direction:column;gap:1px;}
.wl-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;font-family:'JetBrains Mono',monospace;}
.wl-val{font-size:11px;font-weight:600;font-family:'JetBrains Mono',monospace;}
.wl-val.up{color:var(--green);}
.wl-del{color:var(--muted);background:none;border:none;cursor:pointer;font-size:16px;padding:4px;}
.nf-row{display:flex;gap:5px;padding:8px 14px;border-bottom:1px solid var(--border);overflow-x:auto;}
.nf-row::-webkit-scrollbar{display:none;}
.nfp{padding:4px 10px;border-radius:20px;border:1px solid var(--border);background:var(--bg3);color:var(--muted2);font-size:10px;cursor:pointer;white-space:nowrap;transition:all .15s;font-family:'Syne',sans-serif;font-weight:600;}
.nfp.active{background:var(--accent);color:#fff;border-color:var(--accent);}
.ni{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s;}
.ni:hover{background:var(--bg3);}
.ni:last-child{border-bottom:none;}
.ni-top{display:flex;align-items:flex-start;gap:6px;margin-bottom:5px;}
.ni-score{font-size:10px;font-family:'JetBrains Mono',monospace;font-weight:700;padding:2px 6px;border-radius:4px;flex-shrink:0;margin-top:1px;}
.sh{background:#10b98120;color:#10b981;border:1px solid #10b98130;}
.sm{background:#3b82f620;color:#3b82f6;border:1px solid #3b82f630;}
.sl{background:#4a556820;color:#6b7a95;border:1px solid #1c2030;}
.ni-title{font-size:11px;line-height:1.5;color:var(--text);}
.ni-bot{display:flex;align-items:center;gap:6px;margin-top:5px;flex-wrap:wrap;}
.ni-src{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;}
.ni-tickers{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap;}
.ni-tick{font-size:10px;font-family:'JetBrains Mono',monospace;background:var(--bg4);border:1px solid var(--border2);padding:2px 6px;border-radius:4px;cursor:pointer;color:var(--cyan);transition:all .15s;}
.ni-tick:hover{background:var(--accent);color:#fff;}
.lkp-box{padding:14px;}
.lkp-row{display:flex;gap:8px;margin-bottom:12px;}
.lkp-res{background:var(--bg3);border:1px solid var(--border2);border-radius:10px;padding:14px;}
.lkp-tick{font-size:22px;font-weight:800;margin-bottom:10px;}
.lkp-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;}
.lkp-item{display:flex;flex-direction:column;gap:2px;}
.lkp-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;font-family:'JetBrains Mono',monospace;}
.lkp-val{font-size:13px;font-weight:700;font-family:'JetBrains Mono',monospace;}
.lkp-val.up{color:var(--green);}
.sig-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;}
.sg{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:14px;}
.sb{background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:11px;}
.sb-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;font-family:'JetBrains Mono',monospace;margin-bottom:4px;}
.sb-val{font-size:15px;font-weight:700;font-family:'JetBrains Mono',monospace;color:var(--cyan);}
.sch-row{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;margin-bottom:6px;border:1px solid var(--border);}
.sch-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.empty{padding:40px 20px;text-align:center;}
.empty-icon{font-size:32px;opacity:.3;margin-bottom:10px;}
.empty-title{font-size:12px;color:var(--muted2);margin-bottom:4px;}
.empty-sub{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%) translateY(100px);background:var(--bg4);border:1px solid var(--border2);padding:8px 16px;border-radius:20px;font-size:11px;font-family:'JetBrains Mono',monospace;transition:transform .3s;z-index:999;white-space:nowrap;box-shadow:0 4px 20px #0009;}
.toast.show{transform:translateX(-50%) translateY(0);}
.toast.ok{border-color:var(--green);color:var(--green);}
.toast.err{border-color:var(--red);color:var(--red);}
@media(max-width:600px){.stats-row{grid-template-columns:repeat(2,1fr);}}
</style>
</head>
<body>
<div class="app">
<div class="topbar">
  <div class="logo">Pro<span>Float</span></div>
  <div class="sess-badge sb-closed" id="sessBadge">LOADING</div>
  <div class="topbar-right">
    <div class="sound-btn on" id="soundBtn" onclick="toggleSound()">&#128266; Sound ON</div>
    <div class="pill"><div class="dot" id="scanDot"></div><span id="scanTxt">--</span></div>
  </div>
</div>
<div class="nav">
  <div class="nav-tab active" onclick="showPage('alerts',this)">Signals <span class="nbadge" id="alertBadge">0</span></div>
  <div class="nav-tab" onclick="showPage('watchlist',this)">Watchlist <span class="nbadge" id="wlBadge">0</span></div>
  <div class="nav-tab" onclick="showPage('news',this)">News <span class="nbadge" id="newsBadge">0</span></div>
  <div class="nav-tab" onclick="showPage('lookup',this)">Lookup</div>
  <div class="nav-tab" onclick="showPage('settings',this)">Settings</div>
</div>
<div class="content">

<div class="page active" id="page-alerts">
<div class="scroll">
<div class="stats-row">
  <div class="stat-box"><div class="s-lbl">Total</div><div class="s-val" style="color:var(--green)" id="sTot">0</div></div>
  <div class="stat-box"><div class="s-lbl">Grade A+/A</div><div class="s-val" style="color:var(--cyan)" id="sA">0</div></div>
  <div class="stat-box"><div class="s-lbl">Pre Market</div><div class="s-val" style="color:#8b5cf6" id="sPre">0</div></div>
  <div class="stat-box"><div class="s-lbl">After Hours</div><div class="s-val" style="color:var(--orange)" id="sAH">0</div></div>
</div>
<div class="filter-row">
  <div class="fp active" onclick="setSF('all',this)">All Sessions</div>
  <div class="fp" onclick="setSF('premarket',this)">Pre Market</div>
  <div class="fp" onclick="setSF('regular',this)">Regular</div>
  <div class="fp" onclick="setSF('afterhours',this)">After Hours</div>
</div>
<div class="filter-row">
  <div class="fp active" onclick="setGF('all',this)">All Grades</div>
  <div class="fp" onclick="setGF('A+',this)">A+</div>
  <div class="fp" onclick="setGF('A',this)">A</div>
  <div class="fp" onclick="setGF('B',this)">B</div>
  <div class="fp" onclick="setGF('C',this)">C</div>
</div>
<div class="card">
  <div class="card-hdr">
    <div class="card-title">Live Signals</div>
    <div style="display:flex;gap:6px;align-items:center;">
      <span style="font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;" id="lastScanTxt">--</span>
      <button class="btn btn-g btn-sm" onclick="clearAlerts()">Clear</button>
    </div>
  </div>
  <div id="alertsList"><div class="empty"><div class="empty-icon">&#128225;</div><div class="empty-title">Scanning for catalysts...</div><div class="empty-sub">FDA - M&A - Contracts - Earnings - Squeeze</div></div></div>
</div>
</div>
</div>

<div class="page" id="page-watchlist">
<div class="scroll">
<div class="card">
  <div class="card-hdr">
    <div class="card-title">My Watchlist</div>
    <button class="btn btn-g btn-sm" onclick="refreshWL()">Refresh Quotes</button>
  </div>
  <div class="wl-add">
    <input class="wl-input" id="wlInput" placeholder="Type ticker e.g. HCWB" maxlength="5" onkeydown="if(event.key==='Enter')addWL()">
    <button class="btn btn-p" onclick="addWL()">+ Add</button>
  </div>
  <div id="wlList"><div class="empty"><div class="empty-icon">&#128065;</div><div class="empty-title">Watchlist empty</div><div class="empty-sub">Signals auto-add here or type ticker above</div></div></div>
</div>
</div>
</div>

<div class="page" id="page-news">
<div style="background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;">
<div class="nf-row">
  <div class="nfp active" onclick="setNF('all',this)">All</div>
  <div class="nfp" onclick="setNF('FDA Approval',this)">FDA</div>
  <div class="nfp" onclick="setNF('Merger Acquisition',this)">M&A</div>
  <div class="nfp" onclick="setNF('Government Contract',this)">Contract</div>
  <div class="nfp" onclick="setNF('Earnings Beat',this)">Earnings</div>
  <div class="nfp" onclick="setNF('Short Squeeze',this)">Squeeze</div>
  <div class="nfp" onclick="setNF('Defence Surge',this)">Defence</div>
  <div class="nfp" onclick="setNF('Energy Surge',this)">Energy</div>
  <div class="nfp" onclick="setNF('Clinical Trial Win',this)">Trial</div>
</div>
</div>
<div class="scroll" style="padding:0;">
  <div id="newsList"><div class="empty"><div class="empty-icon">&#128240;</div><div class="empty-title">Loading news...</div></div></div>
</div>
</div>

<div class="page" id="page-lookup">
<div class="scroll">
<div class="card">
  <div class="card-hdr"><div class="card-title">Ticker Lookup</div></div>
  <div class="lkp-box">
    <div class="lkp-row">
      <input class="wl-input" id="luInput" placeholder="e.g. HCWB" maxlength="5" onkeydown="if(event.key==='Enter')doLookup()">
      <button class="btn btn-p" onclick="doLookup()">Search</button>
    </div>
    <div id="luResult" style="display:none;">
      <div class="lkp-res">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
          <div class="lkp-tick" id="luTick">--</div>
          <div id="luSess"></div>
        </div>
        <div class="lkp-grid">
          <div class="lkp-item"><div class="lkp-lbl">Price</div><div class="lkp-val" id="luPrice">--</div></div>
          <div class="lkp-item"><div class="lkp-lbl">Gap %</div><div class="lkp-val up" id="luGap">--</div></div>
          <div class="lkp-item"><div class="lkp-lbl">Float</div><div class="lkp-val" id="luFloat">--</div></div>
          <div class="lkp-item"><div class="lkp-lbl">Volume</div><div class="lkp-val" id="luVol">--</div></div>
          <div class="lkp-item"><div class="lkp-lbl">RVOL</div><div class="lkp-val" id="luRvol">--</div></div>
          <div class="lkp-item"><div class="lkp-lbl">Qualifies</div><div class="lkp-val" id="luQ">--</div></div>
        </div>
        <div class="sig-row" id="luSigs"></div>
        <div style="display:flex;gap:8px;">
          <button class="btn btn-p" style="flex:1;" onclick="addLuWL()">+ Add to Watchlist</button>
          <a id="luTV" href="#" target="_blank" class="btn btn-g" style="text-decoration:none;text-align:center;flex:1;">TradingView Chart</a>
        </div>
        <div style="margin-top:10px;padding:10px;background:var(--bg4);border-radius:6px;font-size:11px;color:var(--muted2);">
          Set in TradingView indicator Max Float Size = <b style="color:var(--cyan);" id="luFloatHint">--</b>
        </div>
      </div>
    </div>
  </div>
</div>
</div>
</div>

<div class="page" id="page-settings">
<div class="scroll">
<div class="card">
  <div class="card-hdr"><div class="card-title">Scanner Settings</div></div>
  <div class="sg">
    <div class="sb"><div class="sb-lbl">Min Score</div><div class="sb-val" id="setScore">--</div></div>
    <div class="sb"><div class="sb-lbl">Min Gap</div><div class="sb-val" id="setGap">--</div></div>
    <div class="sb"><div class="sb-lbl">Max Float</div><div class="sb-val" id="setFloat">--</div></div>
    <div class="sb"><div class="sb-lbl">Min RVOL</div><div class="sb-val" id="setRvol">--</div></div>
    <div class="sb"><div class="sb-lbl">Scan Every</div><div class="sb-val" id="setScan">--</div></div>
    <div class="sb"><div class="sb-lbl">Pushover</div><div class="sb-val" id="setPush">--</div></div>
  </div>
</div>
<div class="card">
  <div class="card-hdr"><div class="card-title">Scanning Schedule</div></div>
  <div style="padding:12px 14px;">
    <div class="sch-row" style="background:var(--bg3);"><div class="sch-dot" style="background:#8b5cf6;box-shadow:0 0 5px #8b5cf6;"></div><div style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;color:#8b5cf6;min-width:130px;">4:00AM - 9:30AM ET</div><div style="font-size:12px;">Pre Market</div></div>
    <div class="sch-row" style="background:var(--bg3);"><div class="sch-dot" style="background:#10b981;box-shadow:0 0 5px #10b981;"></div><div style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;color:#10b981;min-width:130px;">9:30AM - 4:00PM ET</div><div style="font-size:12px;">Regular Hours</div></div>
    <div class="sch-row" style="background:var(--bg3);"><div class="sch-dot" style="background:#f97316;box-shadow:0 0 5px #f97316;"></div><div style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;color:#f97316;min-width:130px;">4:00PM - 8:00PM ET</div><div style="font-size:12px;">After Hours (Watchlist)</div></div>
    <div class="sch-row"><div class="sch-dot" style="background:var(--muted);"></div><div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);min-width:130px;">8:00PM - 4:00AM ET</div><div style="font-size:12px;color:var(--muted);">Closed - Scanner sleeps</div></div>
  </div>
</div>
<div class="card">
  <div class="card-hdr"><div class="card-title">Grading - 6 Factors</div></div>
  <div style="padding:14px;font-size:11px;color:var(--muted2);line-height:2.2;">
    <div>1 - Catalyst score 6 or above</div>
    <div>2 - Gap 20% or above from prior close</div>
    <div>3 - Float 5M shares or less</div>
    <div>4 - RVOL 5x or above</div>
    <div>5 - <b style="color:var(--green);">Volume spike happening RIGHT NOW</b></div>
    <div>6 - <b style="color:var(--cyan);">Price making new highs right now</b></div>
    <div style="margin-top:8px;padding:8px;background:var(--bg3);border-radius:6px;border:1px solid var(--border);">A+ = 6pts | A = 5pts | B = 4pts | C = 3pts</div>
  </div>
</div>
<div class="card">
  <div class="card-hdr"><div class="card-title">Feed Status</div></div>
  <div id="feedStatus" style="padding:4px 0;"></div>
</div>
</div>
</div>

</div>
</div>
<div class="toast" id="toast"></div>
<script>
var allAlerts=[],allNews=[],newsFilter='all',sessFilter='all',gradeFilter='all',lastCount=0,currentLookup=null,soundEnabled=true,audioCtx=null;
var EMOJI={'FDA Approval':'FDA','Clinical Trial Win':'Trial','Merger Acquisition':'M&A','Government Contract':'Contract','Earnings Beat':'Earnings','Short Squeeze':'Squeeze','Partnership Deal':'Deal','Energy Surge':'Energy','Defence Surge':'Defence','General News':'News'};
function em(c){return EMOJI[c]||'News';}
function showPage(n,tab){
  document.querySelectorAll('.page').forEach(function(p){p.classList.remove('active');});
  document.querySelectorAll('.nav-tab').forEach(function(t){t.classList.remove('active');});
  document.getElementById('page-'+n).classList.add('active');
  if(tab)tab.classList.add('active');
  if(n==='watchlist')refreshWL();
}
function toast(msg,type){
  var t=document.getElementById('toast');
  t.textContent=msg;t.className='toast show '+(type||'');
  setTimeout(function(){t.className='toast';},2500);
}
function initAudio(){
  if(!audioCtx){try{audioCtx=new(window.AudioContext||window.webkitAudioContext)();}catch(e){}}
  if(audioCtx&&audioCtx.state==='suspended')audioCtx.resume();
}
function playTone(freq,delay,vol,dur){
  if(!audioCtx)return;
  try{
    var o=audioCtx.createOscillator(),g=audioCtx.createGain(),now=audioCtx.currentTime;
    o.connect(g);g.connect(audioCtx.destination);
    o.type='sine';o.frequency.value=freq;
    g.gain.setValueAtTime(0,now+delay);
    g.gain.linearRampToValueAtTime(vol,now+delay+0.02);
    g.gain.exponentialRampToValueAtTime(0.001,now+delay+dur);
    o.start(now+delay);o.stop(now+delay+dur);
  }catch(e){}
}
function playSound(grade){
  if(!soundEnabled)return;
  initAudio();
  if(!audioCtx)return;
  if(grade==='A+'){playTone(880,0,0.3,0.4);playTone(1100,0.4,0.3,0.4);playTone(880,0.8,0.3,0.4);playTone(1100,1.2,0.3,0.4);}
  else if(grade==='A'){playTone(880,0,0.25,0.3);playTone(880,0.35,0.25,0.3);}
  else if(grade==='B'){playTone(660,0,0.25,0.4);}
  else{playTone(440,0,0.15,0.3);}
}
function toggleSound(){
  initAudio();
  soundEnabled=!soundEnabled;
  var btn=document.getElementById('soundBtn');
  if(soundEnabled){btn.textContent='\uD83D\uDD0A Sound ON';btn.className='sound-btn on';playTone(660,0,0.2,0.3);toast('Sound ON','ok');}
  else{btn.textContent='\uD83D\uDD07 Sound OFF';btn.className='sound-btn off';toast('Sound OFF','');}
}
function sessTag(s){
  if(s==='premarket')return '<span class="stag st-premarket">PRE</span>';
  if(s==='afterhours')return '<span class="stag st-afterhours">AH</span>';
  if(s==='regular')return '<span class="stag st-regular">REG</span>';
  return '';
}
function setSF(s,btn){
  sessFilter=s;
  document.querySelectorAll('#page-alerts .filter-row:first-of-type .fp').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  renderAlertList();
}
function setGF(g,btn){
  gradeFilter=g;
  document.querySelectorAll('#page-alerts .filter-row:last-of-type .fp').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  renderAlertList();
}
function renderAlerts(alerts){
  allAlerts=alerts;
  document.getElementById('alertBadge').textContent=alerts.length;
  document.getElementById('sTot').textContent=alerts.length;
  document.getElementById('sA').textContent=alerts.filter(function(a){return a.grade==='A+'||a.grade==='A';}).length;
  document.getElementById('sPre').textContent=alerts.filter(function(a){return a.session==='premarket';}).length;
  document.getElementById('sAH').textContent=alerts.filter(function(a){return a.session==='afterhours';}).length;
  renderAlertList();
  if(alerts.length>lastCount&&lastCount>0){
    playSound(alerts[0].grade);
    if(Notification.permission==='granted'){new Notification('ProFloat '+alerts[0].grade+': '+alerts[0].ticker,{body:alerts[0].cat+' +'+alerts[0].gap+'%'});}
  }
  lastCount=alerts.length;
}
function renderAlertList(){
  var list=document.getElementById('alertsList');
  var filtered=allAlerts;
  if(sessFilter!=='all')filtered=filtered.filter(function(a){return a.session===sessFilter;});
  if(gradeFilter!=='all')filtered=filtered.filter(function(a){return a.grade===gradeFilter;});
  if(!filtered.length){list.innerHTML='<div class="empty"><div class="empty-icon">&#128225;</div><div class="empty-title">No signals match filters</div><div class="empty-sub">Try changing session or grade filter</div></div>';return;}
  list.innerHTML=filtered.map(function(a){
    var gc='g'+a.grade.replace('+','P');
    var sc=a.vol_spike?'spike-on':'spike-off';
    var st=a.vol_spike?'Vol Spike '+a.vol_spike_ratio+'x':'No spike';
    var hc=a.making_highs?'highs-on':'highs-off';
    var ht=a.making_highs?'New Highs':'Not at highs';
    var reasons=(a.reasons||[]).join(' | ');
    return '<div class="alert-item">'+
      '<div class="alert-row1">'+
        '<span class="ticker">'+a.ticker+'</span>'+
        '<span class="gbadge '+gc+'">'+a.grade+'</span>'+
        sessTag(a.session)+
        '<span class="cat-tag">'+em(a.cat)+' '+a.cat+'</span>'+
        '<span class="a-gap">+'+a.gap+'%</span>'+
      '</div>'+
      '<div class="a-hl">'+a.headline+'</div>'+
      '<div class="a-metrics">'+
        '<div class="metric"><div class="m-lbl">Price</div><div class="m-val">$'+a.price+'</div></div>'+
        '<div class="metric"><div class="m-lbl">Vol</div><div class="m-val">'+a.vol_str+'</div></div>'+
        '<div class="metric"><div class="m-lbl">Float</div><div class="m-val">'+a.fm+'M</div></div>'+
        '<div class="metric"><div class="m-lbl">RVOL</div><div class="m-val">'+a.rvol+'x</div></div>'+
        '<div class="metric"><div class="m-lbl">Score</div><div class="m-val">'+a.score+'/10</div></div>'+
        '<span class="'+sc+'">'+st+'</span>'+
        '<span class="'+hc+'">'+ht+'</span>'+
        '<a class="tv-link" href="'+a.tv_link+'" target="_blank" onclick="event.stopPropagation()">Chart</a>'+
      '</div>'+
      (reasons?'<div class="grade-reasons">'+reasons+'</div>':'')+
      '<div class="a-time">'+a.date+' - '+a.time+' - '+a.source+'</div>'+
    '</div>';
  }).join('');
}
function renderNews(news){
  allNews=news;
  document.getElementById('newsBadge').textContent=news.length;
  applyNF();
}
function applyNF(){
  var f=newsFilter==='all'?allNews:allNews.filter(function(n){return n.cat===newsFilter;});
  var list=document.getElementById('newsList');
  if(!f.length){list.innerHTML='<div class="empty"><div class="empty-icon">&#128240;</div><div class="empty-title">No news yet</div></div>';return;}
  list.innerHTML=f.slice(0,100).map(function(n){
    var sc=n.score>=8?'sh':n.score>=5?'sm':'sl';
    var tickers=(n.tickers||[]).map(function(t){return '<span class="ni-tick" onclick="event.stopPropagation();addFN(\''+t+'\',\''+(n.cat||'')+'\')">'+t+' +</span>';}).join('');
    return '<div class="ni" onclick="window.open(\''+(n.link||'#')+'\',\'_blank\')">'+'<div class="ni-top">'+(n.score>0?'<span class="ni-score '+sc+'">'+n.score+'/10</span>':'')+'<span class="ni-title">'+n.title+'</span></div>'+'<div class="ni-bot"><span class="ni-src">'+n.source+' - '+n.time+'</span>'+(tickers?'<div class="ni-tickers">'+tickers+'</div>':'')+'</div></div>';
  }).join('');
}
function setNF(cat,btn){
  newsFilter=cat;
  document.querySelectorAll('.nfp').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  applyNF();
}
function addFN(ticker,cat){
  fetch('/api/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:ticker,cat:cat})})
    .then(function(r){return r.json();}).then(function(d){d.ok?toast(ticker+' added','ok'):toast(d.error,'err');});
}
function addWL(){
  var inp=document.getElementById('wlInput');
  var t=inp.value.trim().toUpperCase();
  if(!t)return;
  fetch('/api/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:t,cat:'Manual'})})
    .then(function(r){return r.json();}).then(function(d){if(d.ok){toast(t+' added','ok');inp.value='';refreshWL();}else toast(d.error,'err');});
}
function removeWL(ticker){
  fetch('/api/watchlist/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:ticker})})
    .then(function(){refreshWL();});
}
function refreshWL(){
  fetch('/api/watchlist/refresh').then(function(r){return r.json();}).then(renderWL);
}
function renderWL(items){
  document.getElementById('wlBadge').textContent=items.length;
  var list=document.getElementById('wlList');
  if(!items.length){list.innerHTML='<div class="empty"><div class="empty-icon">&#128065;</div><div class="empty-title">Watchlist empty</div><div class="empty-sub">Signals auto-add here or type ticker above</div></div>';return;}
  list.innerHTML=items.map(function(w){
    var gc=w.grade&&w.grade!=='--'?'g'+w.grade.replace('+','P'):'';
    return '<div class="wl-item"><div><div class="wl-tick">'+w.ticker+'</div>'+(w.grade&&w.grade!=='--'?'<span class="gbadge '+gc+'" style="font-size:9px;">'+w.grade+'</span>':'')+'</div><div class="wl-data"><div class="wl-col"><div class="wl-lbl">Price</div><div class="wl-val">$'+(w.price||'--')+'</div></div><div class="wl-col"><div class="wl-lbl">Gap</div><div class="wl-val up">'+(w.gap!==undefined?(w.gap>0?'+':'')+w.gap+'%':'--')+'</div></div><div class="wl-col"><div class="wl-lbl">Float</div><div class="wl-val">'+(w.float_m||'--')+'M</div></div><div class="wl-col"><div class="wl-lbl">RVOL</div><div class="wl-val">'+(w.rvol||'--')+'x</div></div><div class="wl-col"><div class="wl-lbl">Spike</div><div class="wl-val">'+(w.vol_spike!==undefined?(w.vol_spike?'Yes':'No'):'--')+'</div></div></div><button class="wl-del" onclick="removeWL(\''+w.ticker+'\')">x</button></div>';
  }).join('');
}
function doLookup(){
  var inp=document.getElementById('luInput');
  var ticker=inp.value.trim().toUpperCase();
  if(!ticker)return;
  document.getElementById('luResult').style.display='none';
  fetch('/api/ticker/'+ticker).then(function(r){return r.json();}).then(function(d){
    if(!d.ok){toast('Not found','err');return;}
    currentLookup=d;
    document.getElementById('luTick').textContent=d.ticker;
    document.getElementById('luPrice').textContent='$'+d.price;
    document.getElementById('luGap').textContent=(d.gap>0?'+':'')+d.gap+'%';
    document.getElementById('luFloat').textContent=d.float_m+'M shares';
    document.getElementById('luVol').textContent=d.vol;
    document.getElementById('luRvol').textContent=d.rvol+'x';
    document.getElementById('luQ').textContent=d.qualifies?'Yes':'No';
    document.getElementById('luQ').style.color=d.qualifies?'var(--green)':'var(--red)';
    document.getElementById('luFloatHint').textContent=d.float_m;
    document.getElementById('luSess').innerHTML=sessTag(d.session);
    document.getElementById('luTV').href=d.tv_link||'#';
    var sigs=[];
    if(d.vol_spike)sigs.push('<span class="spike-on">Vol Spike '+d.vol_spike_ratio+'x</span>');
    else sigs.push('<span class="spike-off">No Vol Spike</span>');
    if(d.making_highs)sigs.push('<span class="highs-on">Making New Highs</span>');
    else sigs.push('<span class="highs-off">Not At Highs</span>');
    document.getElementById('luSigs').innerHTML=sigs.join('');
    document.getElementById('luResult').style.display='block';
  }).catch(function(){toast('Error','err');});
}
function addLuWL(){if(currentLookup)addFN(currentLookup.ticker,'Manual Lookup');}
function renderStatus(s){
  var sb=document.getElementById('sessBadge');
  sb.textContent=s.session_label||'LOADING';
  sb.className='sess-badge sb-'+(s.session||'closed');
  document.getElementById('scanDot').className='dot '+(s.scanning?'on':'off');
  document.getElementById('scanTxt').textContent=s.scanning?'Scanning':(s.market_open?'Idle':'Sleeping');
  var ls=document.getElementById('lastScanTxt');
  if(ls)ls.textContent=s.last_scan?'Last: '+s.last_scan:'--';
  var ps=document.getElementById('setPush');
  if(ps){ps.textContent=s.pushover_ok?'Ready':'Not set';ps.style.color=s.pushover_ok?'var(--green)':'var(--red)';}
  if(s.settings){
    document.getElementById('setScore').textContent=s.settings.MIN_SCORE+'/10';
    document.getElementById('setGap').textContent=s.settings.MIN_GAP+'%';
    document.getElementById('setFloat').textContent=s.settings.MAX_FLOAT+'M';
    document.getElementById('setRvol').textContent=s.settings.MIN_RVOL+'x';
    document.getElementById('setScan').textContent=s.settings.SCAN_SECS+'s';
  }
  var feeds=s.feeds||{};
  var names=Object.keys(feeds);
  var fs=document.getElementById('feedStatus');
  if(fs&&names.length){
    fs.innerHTML=names.map(function(n){
      var v=feeds[n],ok=v&&v.indexOf('items')>-1,er=v&&(v.indexOf('Error')>-1||v.indexOf('HTTP')>-1);
      var c=ok?'var(--green)':er?'var(--red)':'var(--muted)';
      return '<div style="display:flex;justify-content:space-between;padding:7px 14px;border-bottom:1px solid var(--border);font-size:10px;"><span style="font-family:JetBrains Mono,monospace;color:var(--muted2);">'+n+'</span><span style="font-family:JetBrains Mono,monospace;color:'+c+';">'+(v||'--')+'</span></div>';
    }).join('');
  }
}
function clearAlerts(){fetch('/api/alerts/clear',{method:'POST'}).then(function(){lastCount=0;refresh();toast('Cleared','ok');});}
function refresh(){
  fetch('/api/alerts').then(function(r){return r.json();}).then(renderAlerts).catch(function(){});
  fetch('/api/news').then(function(r){return r.json();}).then(renderNews).catch(function(){});
  fetch('/api/status').then(function(r){return r.json();}).then(renderStatus).catch(function(){});
}
if(Notification.permission==='default')Notification.requestPermission();
refresh();
setInterval(refresh,10000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
