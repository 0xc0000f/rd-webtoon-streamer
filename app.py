#!/usr/bin/env python3
# app.py — Bomtoon-DL-style RIDI webtoon downloader site
# Flow: search by title -> grid pick -> episode list -> select & download
# (live progress + AIMD workers) -> read online or export .zip
import os, re, json, glob, time, threading, subprocess, sys, tempfile, zipfile, html, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 3000))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "16"))
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DOWNLOAD_DATA_DIR", APP_ROOT)
DL_DIRS = [os.path.join(DATA_DIR, "w%02d" % i) for i in range(48)]
for _d in DL_DIRS:
    try: os.makedirs(_d, exist_ok=True)
    except Exception: pass
WORKER = os.path.join(APP_ROOT, "farm_worker.py")
CACHE_DIR = os.path.join(APP_ROOT, "cache_series")
TITLES_CACHE = os.path.join(CACHE_DIR, "ep_titles.json")
os.makedirs(CACHE_DIR, exist_ok=True)


def find_pages(bid):
    for d in DL_DIRS:
        p = os.path.join(APP_ROOT, d, bid)
        if os.path.isdir(p) and any(f.endswith((".webp", ".jpg")) for f in os.listdir(p)):
            return sorted(glob.glob(os.path.join(p, "*.webp")) +
                          glob.glob(os.path.join(p, "*.jpg")))
    return None


# ---------------- upstream clients ----------------
import requests

SREQ = requests.Session()
SREQ.headers["User-Agent"] = "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"
SEARCH_API = "https://search-api.ridibooks.com/search"
BOOK_API = "https://book-api.ridibooks.com/books/"


def search_titles(q):
    r = SREQ.get(SEARCH_API, params={"keyword": q, "limit": 24}, timeout=25)
    books = list((r.json() or {}).get("books") or [])
    # grouped/series nodes may ride alongside flat entries: they carry
    # series_id + book_count + series_info instead of a single b_id
    grouped = []
    for b in books:
        si = b.get("series_info") or {}
        if b.get("series_id") and not str(b.get("b_id") or "").isdigit():
            grouped.append({"b_id": b.get("series_id"),
                            "title": si.get("title") or b.get("title"),
                            "web_title": None,
                            "author": si.get("author") or "",
                            "age_limit": si.get("age_limit") or b.get("age_limit"),
                            "book_count": b.get("book_count") or si.get("book_count"),
                            "is_serial": True})
    items = books + grouped
    out = []
    seen = {}
    for b in sorted(items, key=lambda x: -int(x.get("book_count") or 0)):
        bid = str(b.get("series_id") or b.get("b_id") or "")
        if not bid.isdigit():
            continue
        title = ((b.get("web_title") or "") + "").strip() or (b.get("title") or "").strip()
        if not title or title.startswith("["):
            title = re.sub(r"^\[[^\]]*\]\s*", "", title) or title
        norm = re.sub(r"\(.*?\)", "", title).strip().lower()
        if norm and norm in seen:
            continue  # one card per series
        if norm:
            seen[norm] = True
        out.append({
            "sid": bid,
            "title": title,
            "author": (b.get("author") or ""),
            "adult": int(b.get("age_limit") or 0) == 19,   # never filtered out
            "serial": bool(b.get("is_serial")),
            "complete": bool(b.get("is_series_complete")),
            "count": int(b.get("book_count") or 0),
            "cover": f"https://img.ridicdn.net/cover/{bid}/large#1",
        })
    return out


def book_meta(bid):
    r = SREQ.get(BOOK_API + str(bid), timeout=25)
    return r.json()


def neighbors(j):
    out = []
    prop = (j.get("series") or {}).get("property") or {}
    for d in ("prev_books", "next_books"):
        blk = prop.get(d) or {}
        vals = list(blk.values()) if isinstance(blk, dict) else list(blk)
        for x in vals:
            if isinstance(x, dict):
                for k in ("b_id", "id"):
                    if x.get(k):
                        out.append(str(x[k])); break
            elif isinstance(x, (int, str)):
                out.append(str(x))
    return out


_EP_TITLES = None
_EP_LOAD = [0]


def load_titles():
    global _EP_TITLES
    now = time.time()
    if _EP_TITLES is not None and now - _EP_LOAD[0] < 60:
        return _EP_TITLES
    try:
        _EP_TITLES = json.load(open(TITLES_CACHE))
    except Exception:
        seed = os.path.join(APP_ROOT, "titles.json")
        try:
            _EP_TITLES = json.load(open(seed))
        except Exception:
            _EP_TITLES = {}
    _EP_LOAD[0] = now
    return _EP_TITLES


def save_titles(t):
    global _EP_TITLES
    _EP_TITLES = t; _EP_LOAD[0] = time.time()
    json.dump(t, open(TITLES_CACHE, "w"))


META_F = os.path.join(DATA_DIR, "series_meta.json")


def load_meta():
    try:
        return json.load(open(META_F))
    except Exception:
        return {"root": {}, "title": {}, "no": {}}


def save_meta(m):
    json.dump(m, open(META_F, "w"))


def sanitize(s, maxlen=60):
    s = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (s or "").strip())
    return s[:maxlen].strip("_") or "untitled"


def ep_label(bid):
    """(nn, folder_name) for a downloaded ep — enrich from RIDI API when cold"""
    m = load_meta()
    root = m["root"].get(bid)
    if not root or bid not in m["no"]:
        try:  # cold container / meta wiped → refill via book api
            jinfo = book_meta(bid)
            s = jinfo.get("series") or {}
            prop = s.get("property") or {}
            root = str(s.get("id") or "") or root or bid
            m["root"][bid] = root
            m["no"][bid] = int(prop.get("volume") or m["no"].get(bid) or 0) or \
                int(m["no"].get(root) or 1)
            ttl = (prop.get("title") or "").strip() or (jinfo.get("title") or "").strip()
            if ttl:
                m.setdefault("title", {})[str(root)] = re.sub(
                    r"^\[[^\]]*\]\s*", "", ttl)
            save_meta(m)
            root = m["root"].get(bid)
        except Exception:
            pass
    series = (m.get("title") or {}).get(str(root)) if root else None
    name = (load_titles().get(bid) or "").strip() or ("ep_" + bid)
    no = m["no"].get(bid)
    head = f"{int(no):02d} " if no else ""
    base = sanitize(f"{head}{name}")
    if series and series != name:
        return base, sanitize(f"{series}_{base}", 90)
    return base, base


def ep_title(bid, meta=None):
    t = load_titles().get(bid)
    if t:
        return t
    try:
        j = meta or book_meta(bid)
        name = (j.get("title") or "").strip() or ""
        if name:
            titles = dict(load_titles())
            titles[bid] = name
            save_titles(titles)
            return name
    except Exception:
        pass
    return "ep " + bid


def resolve(query):
    m = re.search(r"(\d{6,})", str(query))
    if not m:
        return []
    start = m.group(1)
    root, volume = start, 0
    try:
        j = book_meta(start)
        s = j.get("series") or {}
        root = str(s.get("id") or start)
        volume = int(s.get("volume") or 0)
    except Exception:
        pass
    cf = os.path.join(CACHE_DIR, root + ".json")
    if os.path.exists(cf):
        return json.load(open(cf))
    ids = {root}
    frontier = [root]
    while frontier and (not volume or len(ids) < volume):
        b = frontier.pop()
        try:
            nb = neighbors(book_meta(b))
        except Exception:
            continue
        for n in nb:
            if n not in ids:
                ids.add(n); frontier.append(n)
        time.sleep(0.12)
    order = sorted(ids, key=int)
    json.dump(order, open(cf, "w"))
    return order


def series_overview(root_bid, eps):
    title, cover = "", ""
    try:
        j = book_meta(root_bid)
        s = j.get("series") or {}
        prop = s.get("property") or {}
        stitle = (prop.get("title") or (s.get("title") or "")).strip()
        if not stitle and eps:
            b0 = ep_title(min(eps, key=int))
            stitle = re.sub(r"\s*\d+[화회권]*\s*$", "", b0).strip()
        title = stitle
        cover = ((j.get("serial_thumbnail") or {}).get("large")
                 or f"https://img.ridicdn.net/thumbnail/{root_bid}/large")
    except Exception:
        pass
    return {"title": title, "cover": cover}


# ---------------- job manager with AIMD autoscaling ----------------
class Job:
    def __init__(self, bids):
        self.all = list(bids)
        self.pending = list(self.all)
        self.done = {}
        self.failed = []
        self.tries = {}
        self.log = []
        self.lock = threading.Lock()
        self.lanes = []
        self.workers_target = 2
        self.max_workers = MAX_WORKERS
        self.min_workers = 1
        self.history = []
        self.alive = True

    def logf(self, s):
        with self.lock:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] {s}")


JOB = None
LANE_IDX = [0]

# ---- prewarmed identity pool: ready accounts so downloads skip email/signup ----
POOL_DIR = os.path.join(DATA_DIR, "pool")
POOL_TARGET = int(os.environ.get("POOL_TARGET", "2"))


def take_pool():
    """atomically consume one prewarmed account file (None if empty)"""
    try:
        fs = sorted(glob.glob(os.path.join(POOL_DIR, "pool_*.json")))
        if not fs:
            return None
        dst = fs[0].replace("pool_", "inuse_")
        os.rename(fs[0], dst)
        return dst
    except Exception:
        return None


def spawn_pre(delay=0):
    def _w():
        time.sleep(delay)
        while len(glob.glob(os.path.join(POOL_DIR, "pool_*.json"))) < POOL_TARGET \
                and len(glob.glob(os.path.join(POOL_DIR, "*.busy"))) < 2:
            try:
                os.makedirs(POOL_DIR, exist_ok=True)
                busy = os.path.join(POOL_DIR,
                                    f"pre_{int(time.time()*1000)}.busy")
                open(busy, "w").close()
                env = dict(os.environ, PREWARM="1", POOL_DIR=POOL_DIR)
                subprocess.Popen(
                    [sys.executable, WORKER,
                     os.path.join(DATA_DIR, "pre.state.json"), DATA_DIR],
                    stdout=open(os.path.join(POOL_DIR, "pre.log"), "ab"),
                    stderr=subprocess.STDOUT, cwd=APP_ROOT, env=env)
                # one at a time; loop revisits every cycle
                break
            except Exception:
                return

    threading.Thread(target=_w, daemon=True).start()


def prewarm_loop():
    while True:
        if True:
            n = len(glob.glob(os.path.join(POOL_DIR, "pool_*.json")))
            b = len([f for f in glob.glob(os.path.join(POOL_DIR, "*.busy"))
                     if time.time() - os.path.getmtime(f) < 240])
            if n < POOL_TARGET and b == 0:
                spawn_pre()
            for f in glob.glob(os.path.join(POOL_DIR, "*.busy")):
                if time.time() - os.path.getmtime(f) > 300:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        time.sleep(20)


threading.Thread(target=prewarm_loop, daemon=True).start()


def job_progress(j):
    """live step / percent / eta from lane logs + decrypted file counts"""
    try:
        files, steps, ppe_vals = 0, [], []
        acc_n = None
        for l in j.lanes:
            try:
                tail = open(os.path.join(l["outroot"], "lane.log"), "rb").read()[-1200:]
                for line in tail.decode(errors="ignore").splitlines():
                    macc = re.search(r"account #(\d+)", line)
                    if macc:
                        acc_n = max(acc_n or 1, int(macc.group(1)))
                    if line.startswith("STEP"):
                        steps.append(line.strip())
            except Exception:
                pass
            try:
                stf = json.load(open(l["statefile"]))
                for v in (stf.get("done") or {}).values():
                    if (v or {}).get("pages"):
                        ppe_vals.append(int(v["pages"]))
            except Exception:
                pass
            for b in l["bids"]:
                d = os.path.join(l["outroot"], str(b))
                if os.path.isdir(d):
                    files += sum(1 for f in os.listdir(d)
                                 if f.endswith((".webp", ".jpg")))
        done_pages = [int((v or {}).get("pages") or 0) for v in j.done.values()]
        ppe_vals += done_pages
        est_ppe = max(ppe_vals) if ppe_vals else 120
        total_eps = len(j.all)
        est_total = max(est_ppe * total_eps, files + 1)
        pct = min(99, int(files * 100 / est_total)) if j.alive else 100
        now = time.time()
        prev_files, prev_t = getattr(j, "_prog_prev", (None, None))
        eta_min = None
        if files > 2 and prev_files is not None and files > prev_files and now - prev_t > 4:
            rate = (files - prev_files) / max(now - prev_t, 5)
            eta_min = round(max(0.3, (est_total - files) / max(rate, .01)) / 60)
        j._prog_prev = (files, now)
        step = steps[-1] if steps else ("downloading pages" if files else "preparing")
        step = step.replace("STEP ", "", 1)
        if acc_n and acc_n > 1:
            step = f"retrying (attempt {acc_n}/6) · {step}"
        return {"step": step, "pct": pct,
                "eta_min": eta_min, "pages_now": files}
    except Exception:
        return {}


def lane_run(job, lane):
    p = lane["proc"]
    sf = lane["statefile"]; out = lane["outroot"]; bids = lane["bids"]
    try:
        logtail = os.path.join(out, "lane.log")
        last_sz = -1; stall_t = time.time(); born = time.time()
        while p.poll() is None:
            time.sleep(4)
            try:
                sz = os.path.getsize(logtail)
                if sz != last_sz:
                    last_sz = sz; stall_t = time.time()
            except OSError:
                pass
            # hard watchdog: stalled output >150s → kill so bids get requeued
            if time.time() - stall_t > 150 and time.time() - born > 60:
                job.logf(f"lane stalled >150s → kill + respawn")
                try:
                    p.kill()
                except Exception:
                    pass
                break
        st = {}
        try:
            st = json.load(open(sf))
        except Exception:
            pass
        got = {k: v for k, v in (st.get("done") or {}).items()
               if int((v or {}).get("pages") or 0) > 0}
        with job.lock:
            job.done.update(got)
            miss = [b for b in bids if str(b) not in got]
            for b in miss:
                job.tries[b] += 1
            job.failed = [b for b in dict.fromkeys(job.failed + miss)
                          if job.tries.get(b, 0) >= 2 and str(b) not in job.done]
            frac = len(got) / max(len(bids), 1)
            job.history.append(True if frac >= 0.9 else False)
            # titles of finished eps become linkable immediately
            for b in got:
                pass
        try:
            tail = open(logtail, "rb").read()[-4000:].decode(errors="ignore")
            if "30초" in tail or "429" in tail:
                with job.lock:
                    job.history.append(False)
                job.logf("throttle detected → AIMD decrease")
        except Exception:
            pass
    finally:
        with job.lock:
            if lane in job.lanes:
                job.lanes.remove(lane)
            for b in bids:
                if str(b) not in job.done and b not in job.pending:
                    job.pending.append(b)


def aimd_supervisor(job):
    pass
    last_scale = time.time()
    while True:
        with job.lock:
            pend = [b for b in job.pending if str(b) not in job.done]
            job.pending = pend
            n_lanes = len(job.lanes)
            target = job.workers_target
            have_work = bool(pend)
        recent = job.history[-3:]
        now = time.time()
        if len(recent) == 3 and all(recent) and now - last_scale > 20:
            with job.lock:
                if job.workers_target < job.max_workers:
                    job.workers_target += 1
                    job.logf(f"AIMD additive increase → workers={job.workers_target}")
            last_scale = now
        elif any(not x for x in recent) and now - last_scale > 30:
            with job.lock:
                nt = max(job.min_workers, int(job.workers_target * 0.5))
                if nt != job.workers_target:
                    job.logf(f"AIMD multiplicative decrease → workers={nt}")
                    job.workers_target = nt
            last_scale = now
        i = 0
        while have_work and (n_lanes + i) < target and i < 4:
            with job.lock:
                take = max(1, len(job.pending) // max(target - n_lanes, 1))
                bids = []
                for b in list(job.pending)[:take]:
                    if str(b) not in job.done:
                        bids.append(b); job.pending.remove(b)
            if not bids:
                break
            with job.lock:
                LANE_IDX[0] = (LANE_IDX[0] + 1) % len(DL_DIRS)
                idx = LANE_IDX[0]
            out = DL_DIRS[idx]
            os.makedirs(out, exist_ok=True)
            sf = os.path.join(DATA_DIR, f"w{idx:02d}.state.json")
            json.dump({"done": {}, "accounts": []}, open(sf, "w"))
            env = dict(os.environ)
            pool_file = take_pool()          # reuse prewarmed identity if any
            if pool_file:
                env["REUSE_ACCT"] = pool_file
                job.logf(f"pooled account → instant start ({os.path.basename(pool_file)})")
            spawn_pre(0)                     # keep the pool topped up
            pr = subprocess.Popen([sys.executable, WORKER, sf, out] + bids,
                                  stdout=open(os.path.join(out, "lane.log"), "ab"),
                                  stderr=subprocess.STDOUT, cwd=APP_ROOT, env=env)
            lane = {"proc": pr, "statefile": sf, "outroot": out, "bids": bids}
            with job.lock:
                job.lanes.append(lane)
            threading.Thread(target=lane_run, args=(job, lane), daemon=True).start()
            i += 1
        time.sleep(4)
        with job.lock:
            if not job.pending and len(job.lanes) == 0:
                job.alive = False
                # final titles update for done eps
                titles = load_titles()
                dirty = False
                for b in list(job.done):
                    if b not in titles:
                        try:
                            j2 = book_meta(b)
                            name = (j2.get("title") or "").strip()
                            if name:
                                titles[b] = name; dirty = True
                        except Exception:
                            pass
                if dirty:
                    save_titles(titles)
                job.logf("ALL DONE")
                return


def start_job(bids):
    global JOB
    if not bids:
        return None
    job = Job(bids)
    JOB = job
    threading.Thread(target=aimd_supervisor, args=(job,), daemon=True).start()
    return job


# ---------------- UI ----------------
PAGE = r"""<!doctype html><html lang=th><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>RIDI Webtoon DL</title><style>
:root{--pink:#ff4081;--bg:#101014;--card:#191922;--line:#272733}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eee;
font-family:'Noto Sans',system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:rgba(16,16,20,.95);backdrop-filter:blur(6px);z-index:5;flex-wrap:wrap}
.logo{font-weight:800;font-size:18px;color:var(--pink)}
input[type=text]{flex:1;min-width:200px;background:var(--card);border:1px solid var(--line);color:#eee;
padding:11px 14px;border-radius:10px;font-size:14px}
button{background:linear-gradient(135deg,#ff4081,#d81b60);color:#fff;border:0;
padding:11px 18px;border-radius:10px;font-weight:700;cursor:pointer}
button.gray{background:#333;border:1px solid #444}
main{max-width:900px;margin:0 auto;padding:18px 16px 130px}
.pill{font-size:11px;padding:3px 8px;border-radius:99px;background:#26262f;color:#9cf;margin-left:auto}
#pbar{position:fixed;top:0;left:0;height:3px;background:var(--pink);width:0%;z-index:9;transition:width .6s;display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;transition:.15s}
.card:hover{transform:translateY(-2px);border-color:var(--pink)}
.card img{width:100%;aspect-ratio:3/4;object-fit:cover;display:block;background:#222}
.card b{display:block;font-size:13px;padding:8px 10px 2px;line-height:1.35}
.card span{display:block;font-size:11px;color:#888;padding:0 10px 10px}
.serieshead{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);
border-radius:14px;padding:14px;margin:14px 0;align-items:center}
.serieshead img{width:96px;border-radius:10px;flex:none}
.eprow{display:flex;gap:10px;align-items:center;border-bottom:1px solid var(--line);
padding:10px 6px;font-size:14px}
.no{color:#ff9db5;font-weight:700;min-width:30px;text-align:right;font-variant-numeric:tabular-nums}
.eprow a{color:#9cf;text-decoration:none;font-size:13px;white-space:nowrap}
.eprow .ok{color:#7bd88f;font-size:12px}
.sel{accent-color:var(--pink);width:16px;height:16px}
.back{color:#888;cursor:pointer;font-size:13px;display:inline-block;margin-top:4px}
h3{margin:14px 0 8px}.muted{color:#888;font-size:12px}.tag19{color:#ff6a81;font-weight:700}
#actionbar{position:fixed;left:0;right:0;bottom:0;background:#101014ee;border-top:1px solid var(--line);
backdrop-filter:blur(6px);z-index:8;display:none;padding:10px 16px;
display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
#status{font-family:monospace;font-size:12px;color:#9fe;background:#0b0b0f;border:1px solid var(--line);
border-radius:10px;padding:8px 12px;margin:10px auto;max-width:900px;display:none;white-space:pre-wrap}
</style></head><body>
<div id=pbar></div>
<header><div class=logo>📕 RIDI Webtoon DL</div>
<input id=q type=text placeholder="พิมพ์ชื่อเรื่อง หรือวาง URL/รหัสตอน">
<button onclick=search()>ค้นหา</button><span class=pill id=wlab>idle</span></header>
<main id=out></main>
<div id=actionbar></div>
<script>
let SERIES=[],EPS=[],SEL=new Set(),CUR=null,POLLT=null;
const $=id=>document.getElementById(id);
function esc(s){return (s+'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp'}[c]))}
async function search(){
 const q=$('q').value.trim();if(!q)return;
 if(/https?:|\d{6,}/.test(q)&&/[/.]|\b\d{8,}\b/.test(q)){openByQuery(q);return}
 $('out').innerHTML='<h3>กำลังค้นหา…</h3>';
 const r=await fetch('/api/search',{method:'POST',body:JSON.stringify({q})});const j=await r.json();
 SERIES=j.titles||[];renderGrid(j.q||q)}
function renderGrid(q){
 let h=`<h3>เลือกเรื่อง “${esc(q)}” <span class=muted>${SERIES.length} เรื่อง</span></h3><div class=grid>`;
 SERIES.forEach(t=>{
  h+=`<div class=card onclick=openTitle('${t.sid}')><img loading=lazy src='${t.cover}' onerror="this.style.opacity=.2">
   <b>${esc(t.title)}</b><span>${t.count} eps ${t.adult?'· <span class=tag19>19+</span>':''}</span></div>`});
 $('out').innerHTML=h+'</div>'+(SERIES.length?'':'<p class=muted>ไม่เจอ — ลองพิมพ์ชื่อภาษาเกาหลี หรือวาง URL ของตอน</p>');
 $('actionbar').style.display='none'}
async function openByQuery(q){$('out').innerHTML='<h3>โหลดรายการตอน…</h3>';
 const r=await fetch('/api/fetch',{method:'POST',body:JSON.stringify({q})});const j=await r.json();
 if(j.error)return $('out').innerHTML='<p class=muted>'+esc(j.error)+'</p>';EPS=j.eps;CUR=j;
 SEL=new Set();renderEps()}
function openTitle(sid){openByQuery(sid)}
function renderEps(){
 let h=`<div class=back onclick=home()>‹ เปลี่ยนเรื่อง</div>
 <div class=serieshead><img src='${CUR.cover||''}' onerror="this.style.opacity=.15">
 <div><h3 style=margin:0>${esc(CUR.title||'')}</h3>
 <span class=muted>${EPS.length} episodes · โหลดแล้ว ${EPS.filter(e=>e.pages).length}</span></div></div>`;
 EPS.forEach((e,i)=>{h+=`<div class=eprow id=r${i}><label style=display:flex;gap:10px;align-items:center;flex:1>
  <input type=checkbox class=sel data-b='${e.b}' ${(e.pages?'disabled':'')} onchange=tog(this)>
  <span class=no>${String(i+1).padStart(2,'0')}</span> <span style=flex:1>${esc(e.t)}</span></label>`+
  (e.pages?`<span><a href='/ep/${e.b}'>read ${e.pages}p</a> <a href='/ep/${e.b}.zip'>zip</a></span>`
   :'<span class=muted>—</span>')+'</div>'});
 $('out').innerHTML=h;
 const bar=$('actionbar');
 const nDL=EPS.filter(e=>!e.pages).length, nDone=EPS.filter(e=>e.pages).length;
 let b=`<button onclick=dlSelected()>⬇ โหลดตอนที่เลือก (${SEL.size})</button>`;
 if(nDL)b+=`<button class=gray onclick=selAll(1)>เลือกทั้งหมด</button><button class=gray onclick=selAll(0)>เคลียร์</button>`;
 if(nDone)b+=`<button class=gray onclick=bundleZip()>📦 zip ไฟล์ที่โหลดแล้ว</button>`;
 bar.innerHTML=b;bar.style.display='flex'}
function tog(el){el.checked?SEL.add(el.dataset.b):SEL.delete(el.dataset.b);
 const btn=document.querySelector('#actionbar button');if(btn)btn.textContent=`⬇ โหลดตอนที่เลือก (${SEL.size})`}
function selAll(v){document.querySelectorAll('.sel:not([disabled])').forEach(c=>{c.checked=v;v?SEL.add(c.dataset.b):SEL.delete(c.dataset.b)});renderEps()}
let DL_BIDS=[];
function clickA(href){const a=document.createElement('a');a.href=href;a.download='';document.body.appendChild(a);a.click();setTimeout(()=>a.remove(),400)}
async function dlSelected(){
 const missing=[...SEL].filter(b=>!EPS.find(e=>e.b===b&&e.pages));
 if(!missing.length)return alert('เลือกตอนที่ยังไม่ได้โหลดก่อน');
 DL_BIDS=missing.slice();
 const r=await fetch('/api/download',{method:'POST',body:JSON.stringify({bids:missing})});const j=await r.json();
 poll(true)}
function bundleZip(){const done=EPS.filter(e=>e.pages).map(e=>e.b);location.href='/bundle.zip?bids='+done.join(',')}
function home(){$('out').innerHTML='<h3>…</h3>';$('actionbar').style.display='none';$('q').value='';
 clearInterval(POLLT);POLLT=null;$('pbar').style.display='none';$('wlab').textContent='idle'}
function status(t){const s=$('status')||null}
function poll(force){
 if(POLLT&&!force)return; if(!POLLT)POLLT=setInterval(()=>poll(),4000);
 fetch('/api/job').then(r=>r.json()).then(j=>{
  const tot=j.done_n+j.pending_n;
  const pr=j.progress||{};
  const pctTxt=pr.pct!=null?`${pr.pct}%`:'';
  const etaTxt=pr.eta_min!=null?`· ETA ~${pr.eta_min}m `:'';
  $('wlab').textContent=(j.active||tot)?
    `${pctTxt}${etaTxt}· workers ${j.workers_now}/${j.max_workers||16} · ✅${j.done_n}/${tot}`:'idle';
  const pb=$('pbar');pb.style.display=tot?'block':'none';pb.style.width=(pr.pct||0)+'%';
  if(j.active&&pr.step)statusShow(j);
  let changed=false;
  (j.log&&j.log.length)&&statusShow(j);
  EPS.forEach((e,i)=>{
   if(j.done_list.includes(e.b)&&!e.pages){
    e.pages=(j.pages||{})[e.b]||1;changed=true;
    const row=$('r'+i);if(row)row.outerHTML=rowHTML(e,i,true)}});
  if(changed&&CUR)renderEps();
  if(!j.active&&tot){
   clearInterval(POLLT);POLLT=null;if(CUR)renderEps();statusHide();
   const ready=(DL_BIDS||[]).filter(b=>EPS.find(e=>e.b===b&&e.pages));
   DL_BIDS=[];
   if(ready.length===1)clickA(`/ep/${ready[0]}.zip`);
   else if(ready.length>1)clickA('/bundle.zip?bids='+ready.join(','))}
 }).catch(()=>{})}
function rowHTML(e,i,fresh){
 return `<div class=eprow id=r${i}><label style=display:flex;gap:10px;align-items:center;flex:1>
 <input type=checkbox class=sel data-b='${e.b}' ${(e.pages?'disabled':'')} onchange=tog(this)>
 <span class=no>${String(i+1).padStart(2,'0')}</span> <span style=flex:1>${esc(e.t)}</span></label>`+
 (e.pages?`<span><a href='/ep/${e.b}'>read ${e.pages}p</a> <a href='/ep/${e.b}.zip'>zip ⬇</a></span>`
  :'<span class=muted>⏳</span>')+'</div>'}
function statusShow(j){
 let s=$('status');
 if(!s){s=document.createElement('pre');s.id='status';$('out').before(s)}
 s.style.display='block';
 const pr=j.progress||{};
 s.textContent=`⏬ ${pr.step||'…'} · ${pr.pct??''}${pr.eta_min!=null?('· ETA ~'+pr.eta_min+'m'):''}\\nworkers=${j.workers_now}/${j.max_workers||16} ✅${j.done_n} ⏳${j.pending_n} ❌${j.fail_n}`}
function statusHide(){const s=$('status');if(s)s.style.display='none'}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')search()});
poll();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b""
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/search":
            q = (str(data.get("q") or "") or "").strip()
            if re.search(r"\d{6,}", q):
                # paste-url / direct id fallback -> treat as title jump
                return self._json({"titles": [], "jump": q})
            titles = search_titles(q)
            self._json({"titles": titles, "q": q})
        elif p == "/api/fetch":
            q = str(data.get("q") or "")
            eps = resolve(q)
            if not eps:
                return self._json({"error": "ไม่พบรหัสเรื่องจาก input"}, 400)
            root_bid = min(eps, key=int)
            info = series_overview(root_bid, eps)
            try:
                m = load_meta()
                (m.setdefault("root", {}))[root_bid] = ""
                for i, b in enumerate(sorted(eps, key=int)):
                    m["root"][b] = root_bid
                    m["no"][b] = i + 1
                if info.get("title"):
                    m.setdefault("title", {})[root_bid] = info["title"]
                save_meta(m)
            except Exception:
                pass
            out = []
            for b in eps:
                pg = find_pages(b)
                n = len(pg) if pg else 0
                t = ep_title(b)
                out.append({"b": b, "t": t, "pages": n})
            self._json({"title": info["title"], "cover": info["cover"],
                        "root": root_bid, "eps": out})
        elif p == "/api/download":
            bids = [str(x) for x in (data.get("bids") or [])]
            missing = sorted({b for b in bids if not find_pages(b)}, key=int)
            start_job(missing)
            self._json({"started": True, "missing": missing})
        elif p == "/api/stop":
            j = JOB
            if j and j.alive:
                with j.lock:
                    for l in list(j.lanes):
                        try:
                            l["proc"].terminate()
                        except Exception:
                            pass
                    job_stop_pending = None
                j.logf("STOP requested")
            self._json({"ok": True})
        else:
            self._json({}, 404)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        mz = re.match(r"^/ep/(\d+)\.zip$", p)
        mr = re.match(r"^/ep/(\d+)/?$", p)
        if p.startswith("/api/job"):
            j = JOB
            if not j:
                return self._json({"active": False})
            done_list = sorted(j.done, key=int)
            pend = len(j.pending); fail = len(j.failed)
            self._json({"active": j.alive, "finished": not j.alive,
                        "workers_now": len(j.lanes),
                        "workers_target": j.workers_target,
                        "max_workers": j.max_workers,
                        "done_n": len(done_list), "pages": {k: v.get("pages") for k, v in j.done.items()},
                        "pending_n": pend + fail, "fail_n": fail,
                        "progress": job_progress(j),
                        "log": j.log[-8:], "done_list": done_list})
        elif mz:
            bid = mz.group(1)
            pages = find_pages(bid)
            if not pages:
                b = "not downloaded yet".encode()
                self.send_response(404); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            name, folder = ep_label(bid)
            slug = re.sub(r"[^A-Za-z0-9가-힣ㄱ-ㅎㅏ-ㅣ._ -]", "", folder)[:80].strip() or ("EP" + bid)
            fn = urllib.parse.quote(slug + ".zip")
            ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "", folder)[:70].strip() or ("EP" + bid)
            zp = os.path.join(DATA_DIR, "zips"); os.makedirs(zp, exist_ok=True)
            zf = os.path.join(zp, fn[:60] + "_" + bid + ".zip")
            with zipfile.ZipFile(zf, "w") as z:
                for f in pages:
                    z.write(f, arcname=f"{folder}/{os.path.basename(f)}")
            data = open(zf, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{ascii_name}.zip"; filename*=UTF-8\'\'{fn}')
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            return
        elif p == "/bundle.zip":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bids = [b for b in (qs.get("bids") or [""])[0].split(",") if b.isdigit()]
            parts = []
            zfpath = None
            import io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                any_ = False
                for b in bids:
                    pages = find_pages(b)
                    if not pages:
                        continue
                    any_ = True
                    _, folder = ep_label(b)
                    for f in pages:
                        z.write(f, arcname=f"{folder}/{os.path.basename(f)}")
            if not any_:
                return self._json({"error": "nothing downloaded"}, 404)
            data = buf.getvalue()
            slug = sanitize((load_meta()["title"].get(
                load_meta()["root"].get(bids[0], ""), "") or "export"), 60) + "_selected"
            fn = urllib.parse.quote(slug + ".zip")
            ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "", slug)[:70].strip() or "export_selected"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{ascii_name}.zip"; filename*=UTF-8\'\'{fn}')
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        elif mr:
            bid = mr.group(1)
            pages = find_pages(bid)
            if not pages:
                b = "not downloaded yet".encode()
                self.send_response(404); self.send_header("Content-Length", str(len(b)))
                self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers()
                self.wfile.write(b); return
            no, folder = ep_label(bid)
            m = load_meta()
            series = m["title"].get(m["root"].get(bid, ""), "")
            head = html.escape(f"{no} · {folder}") if no else html.escape(folder)
            sub = html.escape(series or "")
            imgs = "".join(
                f"<img loading=lazy src=/page/{bid}/{os.path.basename(f)}>" for f in pages)
            css = ("body{background:#0b0b0f;margin:0;color:#ddd;font-family:sans-serif}"
                   ".top{position:sticky;top:0;background:#0b0b0fee;padding:10px 14px;"
                   "border-bottom:1px solid #272733;display:flex;gap:10px;align-items:baseline;z-index:3}"
                   ".n{font-size:15px;font-weight:700}.s{color:#888;font-size:12px}"
                   "img{display:block;width:min(100vw,820px);margin:6px auto;border-radius:4px}")
            b = (f"<html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                 f"<style>{css}</style></head><body>"
                 f"<div class=top><a href='/' style=color:#9cf>←</a>"
                 f"<span class=n>EP {head}</span>{('<span class=s>'+sub+'</span>') if sub else ''}</div>" + imgs +
                 "</body></html>").encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif p.startswith("/page/"):
            m3 = re.match(r"^/page/(\d+)/([\w.]+)$", p)
            if not m3:
                return self._json({}, 404)
            fp = None
            for d in DL_DIRS:
                c = os.path.join(APP_ROOT, d, m3.group(1), m3.group(2))
                if os.path.exists(c):
                    fp = c; break
            if not fp:
                return self._json({}, 404)
            ct = "image/webp" if fp.endswith(".webp") else "image/jpeg"
            data = open(fp, "rb").read()
            self.send_response(200); self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        else:
            b = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)


if __name__ == "__main__":
    print(f"RIDI Webtoon DL listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
