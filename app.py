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
    books = (r.json() or {}).get("books") or []
    out = []
    seen = {}
    for b in sorted(books, key=lambda x: -int(x.get("book_count") or 0)):
        bid = str(b.get("b_id") or "")
        if not bid.isdigit():
            continue
        title = ((b.get("web_title") or "") + "").strip() or (b.get("title") or "").strip()
        if not title or title.startswith("["):
            title = re.sub(r"^\[[^\]]*\]\s*", "", (b.get("title") or "").strip()) or title
        norm = re.sub(r"\(.*?\)", "", title).strip().lower()
        if norm and norm in seen:
            continue  # one card per series
        if norm:
            seen[norm] = True
        out.append({
            "sid": bid,
            "title": title,
            "author": (b.get("author") or ""),
            "adult": int(b.get("age_limit") or 0) == 19,
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


def lane_run(job, lane):
    p = lane["proc"]
    sf = lane["statefile"]; out = lane["outroot"]; bids = lane["bids"]
    try:
        logtail = os.path.join(out, "lane.log")
        while p.poll() is None:
            time.sleep(4)
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
            job.failed += miss
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
                LANE_IDX += 1
                idx = LANE_IDX % len(DL_DIRS)
            out = DL_DIRS[idx]
            os.makedirs(out, exist_ok=True)
            sf = os.path.join(DATA_DIR, f"w{idx:02d}.state.json")
            json.dump({"done": {}, "accounts": []}, open(sf, "w"))
            env = dict(os.environ)
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
position:sticky;top:0;background:rgba(16,16,20,.94);backdrop-filter:blur(6px);z-index:5;flex-wrap:wrap}
.logo{font-weight:800;font-size:18px;color:var(--pink)}
input[type=text]{flex:1;min-width:220px;background:var(--card);border:1px solid var(--line);color:#eee;
padding:11px 14px;border-radius:10px;font-size:14px}
button{background:linear-gradient(135deg,#ff4081,#d81b60);color:#fff;border:0;
padding:11px 18px;border-radius:10px;font-weight:700;cursor:pointer}
button.gray{background:#333;border:1px solid #444}
main{max-width:1000px;margin:0 auto;padding:20px}
.pill{font-size:11px;padding:3px 8px;border-radius:99px;background:#26262f;color:#9cf;margin-left:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;transition:.15s}
.card:hover{transform:translateY(-2px);border-color:var(--pink)}
.card img{width:100%;aspect-ratio:3/4;object-fit:cover;display:block;background:#222}
.card b{display:block;font-size:13px;padding:8px 10px 2px;height:36px;overflow:hidden}
.card span{display:block;font-size:11px;color:#888;padding:0 10px 10px}
.bar{height:7px;background:#26262f;border-radius:5px;overflow:hidden;margin:8px 0 2px}
.bar>i{display:block;height:100%;background:var(--pink);width:0%}
#log{position:fixed;right:16px;left:16px;bottom:74px;background:#0b0b0f;border:1px solid var(--line);
border-radius:10px;padding:10px;font-family:monospace;font-size:12px;height:120px;
overflow-y:auto;white-space:pre-wrap;color:#9fe;z-index:6}
.row{display:flex;gap:8px;align-items:center;margin:10px 0;flex-wrap:wrap}
.eprow{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid var(--line);
padding:9px 4px;font-size:14px;align-items:center}
.eprow a{color:#9cf;text-decoration:none;font-size:13px}
.sel{accent-color:var(--pink)}
.back{color:#888;cursor:pointer;font-size:13px}
h3{margin:18px 0 6px}.muted{color:#888;font-size:12px}
.tag19{color:#ff6a81;font-weight:700}
footer{position:fixed;left:16px;right:16px;bottom:8px;display:flex;gap:8px;z-index:7}
</style></head><body>
<header><div class=logo>📕 RIDI Webtoon DL</div>
<input id=q type=text placeholder="พิมพ์ชื่อเรื่อง (เช่น 프렛리스) หรือวาง URL/รหัสตอน">
<button onclick=search()>ค้นหา</button>
<span class=pill id=wlab>workers —</span></header>
<main id=out></main>
<div id=log style=display:none></div>
<footer><button onclick=dlSelected()>⬇ DOWNLOAD SELECTED</button>
<button class=gray onclick=selAll(1)>SELECT ALL</button>
<button class=gray onclick=selAll(0)>CLEAR</button>
<button class=gray onclick=backHome()>🏠 HOME</button></footer>
<script>
let VIEW='search',SERIES=[],EPS=[],SEL=new Set(),CUR=null;
const $=id=>document.getElementById(id);
function esc(s){return (s+'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp'}[c]))}
async function search(){
 const q=$('q').value.trim();if(!q)return;
 if(/https?:|\d{6,}/.test(q)&&/[/.]|\b\d{8,}\b/.test(q)){openByQuery(q);return}
 $('out').innerHTML='<h3>กำลังค้นหา…</h3>';
 const r=await fetch('/api/search',{method:'POST',body:JSON.stringify({q})});const j=await r.json();
 SERIES=j.titles||[];VIEW='grid';renderGrid(j.q||q)}
function renderGrid(q){
 let h=`<h3>ผลการค้นหา “${esc(q)}” <span class=muted>${SERIES.length} เรื่อง</span></h3><div class=grid>`;
 SERIES.forEach(t=>{
  h+=`<div class=card onclick=openTitle('${t.sid}')><img loading=lazy src='${t.cover}' onerror="this.style.opacity=.2">
   <b>${esc(t.title)}</b><span>${esc(t.author||'')}${t.adult?' <span class=tag19>19+</span>':''}</span></div>`});
 $('out').innerHTML=h+'</div>'+(SERIES.length?'':'<p class=muted>ไม่เจอเรื่องนี้ — ลองพิมพ์ชื่อภาษาเกาหลี หรือวาง URL ของตอน</p>')}
async function openByQuery(q){$('out').innerHTML='<h3>…</h3>';
 const r=await fetch('/api/fetch',{method:'POST',body:JSON.stringify({q})});const j=await r.json();
 if(j.error)return $('out').innerHTML='<p class=muted>'+esc(j.error)+'</p>';EPS=j.eps;CUR=j;j_=j;
 VIEW='eps';SEL=new Set();renderEps()}
function openTitle(sid){
 $('q').value=sid;openByQuery(sid)}
function renderEps(){
 let h=`<div class=back onclick=backHome()>‹ home</div>
 <h3 style="margin-top:6px">${esc(CUR.title||'')}</h3>`;
 if(CUR.cover)h+=`<img src='${CUR.cover}' style='width:110px;border-radius:8px'>`;
 h+=`<div class=row><span class=muted>${EPS.length} episodes · โหลดแล้ว ${EPS.filter(e=>e.pages).length}</span></div><div id=eplist>`;
 EPS.forEach((e,i)=>{h+=`<div class=eprow><label><input type=checkbox class=sel data-b='${e.b}'
  ${(e.pages?'disabled':'')} onchange=tog(this)> ${esc(e.t)}</label>`+
  (e.pages?`<span><a href='/ep/${e.b}'>read · ${e.pages}p</a> <a href='/ep/${e.b}.zip'>zip</a></span>`
   :`<span class=muted>—</span>`)+'</div>'});
 $('out').innerHTML=h+'</div>'}
function tog(el){el.checked?SEL.add(el.dataset.b):SEL.delete(el.dataset.b)}
function selAll(v){document.querySelectorAll('.sel:not([disabled])').forEach(c=>{c.checked=v;v?SEL.add(c.dataset.b):SEL.delete(c.dataset.b)})}
async function dlSelected(){
 const missing=[...SEL].filter(b=>!EPS.find(e=>e.b===b&&e.pages));
 if(!missing.length)return alert('เลือกตอนที่ยังไม่ได้โหลด');
 const r=await fetch('/api/download',{method:'POST',body:JSON.stringify({bids:missing})});const j=await r.json();
 log('started '+j.missing.length+' eps');poll()}
let timer=setInterval(poll,5000);
async function poll(){const r=await fetch('/api/job');if(!r.ok)return;const j=await r.json();
 if(j.active||j.finished){
  const tot=j.done_n+j.pending_n;
  document.getElementById('wlab').textContent=`workers ${j.workers_now}/${j.workers_target} · done ${j.done_n}/${tot}`;
 }
 if(!j.active)return;
 const tot=j.done_n+j.pending_n;
 let s=`workers=${j.workers_now}/${j.workers_target} | ✅${j.done_n} ⏳${j.pending_n-failAdj(j)} ❌${j.fail_n}`;
 function failAdj(){return 0}
 log(s+'\n'+(j.log.slice(-3).join('\n')));
 EPS.forEach((e,i)=>{
  if(j.done_list.includes(e.b)&&!e.pages){
   e.pages=(j.pages||{})[e.b]||1;
   const row=document.querySelectorAll('.eprow')[i];
   if(row&&CUR&&EPS[i])row.lastElementChild.innerHTML=
    `<span><a href='/ep/${e.b}'>read · ${e.pages}p</a> <a href='/ep/${e.b}.zip'>zip</a></span>`;}});
 SEL=new Set([...SEL].filter(b=>{const e=EPS.find(x=>x.b===b);return e&&!e.pages}));
 if(j.finished)log('✔ ALL DONE — กด zip/read ในรายการได้เลย')}
async function stopJob(){}
function backHome(){$('out').innerHTML='';$('q').value='';VIEW='search'}
function log(t){const l=$('log');l.style.display='';
 l.textContent=(t+'\n'+l.textContent).split('\n').slice(0,60).join('\n')}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')search()});
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
        m2 = re.match(r"^/(ep|zip)/(\d+)(\.zip)?$", p)
        if p.startswith("/api/job"):
            j = JOB
            if not j:
                return self._json({"active": False})
            done_list = sorted(j.done, key=int)
            pend = len(j.pending); fail = len(j.failed)
            self._json({"active": j.alive, "finished": not j.alive,
                        "workers_now": len(j.lanes),
                        "workers_target": j.workers_target,
                        "done_n": len(done_list), "pages": {k: v.get("pages") for k, v in j.done.items()},
                        "pending_n": pend + fail, "fail_n": fail,
                        "log": j.log[-8:], "done_list": done_list})
        elif m2:
            bid, zipped = m2.group(2), bool(m2.group(3))
            pages = find_pages(bid)
            if not pages:
                b = "not downloaded yet".encode()
                self.send_response(404); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            if zipped:
                zp = os.path.join(APP_ROOT, "zips"); os.makedirs(zp, exist_ok=True)
                zf = os.path.join(zp, bid + ".zip")
                with zipfile.ZipFile(zf, "w") as z:
                    for f in pages:
                        z.write(f, arcname=os.path.basename(f))
                data = open(zf, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{bid}.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
                return
            title = html.escape(load_titles().get(bid) or "")
            imgs = "".join(
                f"<img loading=lazy src=/page/{bid}/{os.path.basename(f)}>" for f in pages)
            css = ("body{background:#000;margin:0}img{display:block;width:min(100vw,900px);margin:0 auto}"
                   "a{color:#9cf;font-family:sans-serif;padding:12px;display:inline-block}")
            b = (f"<html><style>{css}</style><a href='/'>← back</a>"
                 f"<h3 style='font-family:sans-serif;color:#eee'>{title}</h3>" + imgs + "</html>").encode()
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
