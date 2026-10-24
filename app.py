#!/usr/bin/env python3
# app.py — LEZHIN DL webtoon downloader site
# Flow: search by title -> grid pick -> episode list -> select & download
# (live progress + AIMD workers) -> read online or export .zip
import os, re, json, glob, time, threading, subprocess, sys, tempfile, zipfile, html, urllib.parse, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import lezhin_dl as LZ

def _env_int(name, default):
    raw = (os.environ.get(name) or "").strip().strip("'\"")
    try:
        return int(raw)
    except ValueError:
        return default

PORT = _env_int("PORT", 3000)
MAX_WORKERS = _env_int("MAX_WORKERS", 16)
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DOWNLOAD_DATA_DIR", APP_ROOT)
DL_DIRS = [os.path.join(DATA_DIR, "w%02d" % i) for i in range(48)]
for _d in DL_DIRS:
    try: os.makedirs(_d, exist_ok=True)
    except Exception: pass
LIB_DIR = os.path.join(DATA_DIR, "library")
os.makedirs(LIB_DIR, exist_ok=True)
SETTINGS_F = os.path.join(DATA_DIR, "settings.json")
WORKER = os.path.join(APP_ROOT, "farm_worker.py")
CACHE_DIR = os.path.join(DATA_DIR, "cache_series")
TITLES_CACHE = os.path.join(CACHE_DIR, "ep_titles.json")
LEGACY_CACHE_DIR = os.path.join(APP_ROOT, "cache_series")
os.makedirs(CACHE_DIR, exist_ok=True)
if os.path.isdir(LEGACY_CACHE_DIR):
    shutil.copytree(LEGACY_CACHE_DIR, CACHE_DIR, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.tmp"))


def disk_pct_of(path):
    u = shutil.disk_usage(path)
    return round(u.used * 100 / max(u.total, 1), 1)


def purge_transient_downloads():
    """Default mode delivers zips to the browser; stale lane/result copies must
    not accumulate on the server across restarts."""
    for d in DL_DIRS + [os.path.join(DATA_DIR, "dl"), os.path.join(DATA_DIR, "zips")]:
        shutil.rmtree(d, ignore_errors=True)
    for d in DL_DIRS:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


purge_transient_downloads()

# ---------------- server library (/app/data/library/<series>/<NNN>-<slug>) --------
def _page_sort_key(f):
    m = re.search(r"(\d+)", os.path.basename(f))
    return int(m.group(1)) if m else 0


def load_settings():
    d = {"ttl_days": 0, "disk_pct": 80}
    try:
        d.update(json.load(open(SETTINGS_F)))
    except Exception:
        pass
    return d


def save_settings(s):
    json.dump(s, open(SETTINGS_F, "w"))


def lib_scan():
    """index every library item from real files (spec: directory scan)"""
    out = {}
    for sj in glob.glob(os.path.join(LIB_DIR, "*", "*", ".meta.json")):
        try:
            m = json.load(open(sj))
            b = str(m.get("b_id") or "")
            if not b:
                continue
            d = os.path.dirname(sj)
            imgs = [f for f in os.listdir(d)
                    if re.match(r"^page_\d+\.(jpg|webp)$", f)]
            if not imgs:
                continue
            out[b] = {
                "path": d,
                "series": m.get("series") or "",
                "title": m.get("title") or "",
                "no": int(m.get("no") or 0),
                "slug": m.get("slug") or "",
                "pages": len(imgs),
                "bytes": sum(os.path.getsize(os.path.join(d, f)) for f in imgs),
                "created": m.get("created") or "",
            }
        except Exception:
            pass
    return out


_LIB_CACHE = {"t": 0.0, "idx": {}}


def lib_index():
    now = time.time()
    if now - _LIB_CACHE["t"] > 5:
        _LIB_CACHE["idx"] = lib_scan()
        _LIB_CACHE["t"] = now
    return _LIB_CACHE["idx"]


def find_pages(bid):
    e = lib_index().get(str(bid))
    if e:
        pats = [os.path.join(e["path"], "page_*.jpg"),
                os.path.join(e["path"], "page_*.webp")]
        fl = sorted(sum((glob.glob(p) for p in pats), []), key=_page_sort_key)
        if fl:
            return fl
    for d in DL_DIRS:
        p = os.path.join(APP_ROOT, d, bid)
        if os.path.isdir(p) and any(f.endswith((".webp", ".jpg")) for f in os.listdir(p)):
            return sorted(glob.glob(os.path.join(p, "*.webp")) +
                          glob.glob(os.path.join(p, "*.jpg")))
    return None


def migrate_to_library(bid):
    """move a finished lane output into the library layout + write .meta.json"""
    try:
        src = None
        for d in DL_DIRS:
            p = os.path.join(APP_ROOT, d, str(bid))
            if os.path.isdir(p) and any(
                    f.endswith((".webp", ".jpg")) for f in os.listdir(p)):
                src = p; break
        if not src:
            return
        m0 = load_meta()
        root = (m0.get("root") or {}).get(str(bid)) or bid
        ttl_series = (m0.get("title") or {}).get(root) or ""
        no = int((m0.get("no") or {}).get(str(bid)) or 1)
        name = (load_titles().get(str(bid)) or "").strip() or ("ep_" + str(bid))
        slug_raw = re.sub(r"^" + re.escape(ttl_series) + r"\s*", "", name).strip() or name
        series_dir = sanitize(ttl_series or "series-" + str(root), 80)
        item_name = f"{no:03d}-{sanitize(slug_raw, 60)}"
        dst = os.path.join(LIB_DIR, series_dir, item_name)
        os.makedirs(dst, exist_ok=True)
        imgs = sorted(glob.glob(os.path.join(src, "*.webp")) +
                      glob.glob(os.path.join(src, "*.jpg")),
                      key=lambda f: (os.path.dirname(f), _page_sort_key(f)))
        n = 0
        for f in imgs:
            ext = ".jpg" if f.endswith(".jpg") else ".webp"
            n += 1
            os.replace(f, os.path.join(dst, f"page_{n:03d}{ext}"))
        json.dump({"b_id": bid, "title": name, "slug": sanitize(slug_raw, 60),
                   "series": ttl_series, "root": root, "no": no,
                   "pages": n, "created": time.strftime("%Y-%m-%dT%H:%M:%S")},
                  open(os.path.join(dst, ".meta.json"), "w"))
        shutil.rmtree(src, ignore_errors=True)
    except Exception as e:
        print("[lib] migrate err", bid, str(e)[:100])


def delete_library(bids=None, series=None):
    idx = lib_scan()
    targets = {}
    for b, e in idx.items():
        if bids and str(b) in {str(x) for x in bids}:
            targets[e["path"]] = b
        if series and e.get("series") == series:
            targets[e["path"]] = b
    n = 0
    for path, b in targets.items():
        try:
            shutil.rmtree(path); n += 1
        except OSError:
            pass
    # also purge legacy lane copies so disk frees up
    for d in DL_DIRS:
        for b in (bids or [x[1] for x in targets.values()]):
            try:
                shutil.rmtree(os.path.join(APP_ROOT, d, str(b)), ignore_errors=True)
            except Exception:
                pass
    _LIB_CACHE["t"] = 0.0
    return n


def sweep_loop():
    while True:
        time.sleep(3600)
        s = load_settings()
        ttl = int(s.get("ttl_days") or 0)
        dkmax = int(s.get("disk_pct") or 80)
        idx = lib_scan()
        if ttl > 0:
            cut = time.time() - ttl * 86400
            old = []
            for b, e in idx.items():
                try:
                    ctm = os.path.getctime(e["path"])
                except OSError:
                    continue
                if ctm < cut:
                    old.append((e["created"], b))
            if old:
                delete_library(bids=[b for _, b in sorted(old)])
                print(f"[sweep] TTL purged {len(old)} items")
        p = disk_pct_of(DATA_DIR)
        if p > dkmax and idx:   # LRU purge oldest first until under threshold
            order = sorted(idx.items(), key=lambda kv: kv[1].get("created") or "")
            for _, (b, e) in enumerate(order):
                delete_library(bids=[b])
                p = disk_pct_of(DATA_DIR)
                if p <= dkmax:
                    break
            print(f"[sweep] disk {p:.0f}% → LRU purge done")


threading.Thread(target=sweep_loop, daemon=True).start()


# ---------------- upstream clients ----------------
import requests

_UA = ("Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
       "Chrome/126 Mobile Safari/537.36")
SEARCH_API = "https://search-api.ridibooks.com/search"
BOOK_API = "https://book-api.ridibooks.com/books/"

_UPX = {"v": None}


def _uproxies():
    if _UPX["v"] is None:
        p = os.environ.get("RIDI_PROXY")
        _UPX["v"] = {"http": p, "https": p} if p else False
    return _UPX["v"] or None


def sget(url, timeout=25, **kw):
    """thread-safe direct first; CF-block/network error → retry via Databay proxy"""
    headers = {"User-Agent": _UA}
    try:
        r = requests.get(url, headers=headers, timeout=timeout, **kw)
        if r.status_code == 403 and _uproxies():
            raise RuntimeError("cf-403")
        return r
    except Exception:
        px = _uproxies()
        if not px:
            raise
        kw.pop("headers", None)
        kw.pop("proxies", None)
        return requests.get(url, headers=headers, timeout=timeout,
                            proxies=px, **kw)


def search_titles(q):
    r = sget(SEARCH_API, params={"keyword": q, "limit": 24})
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
    r = sget(BOOK_API + str(bid))
    return r.json()


def neighbors(j):
    """returns [(bid_str, is_opened_or_None), ...] — RIDI catalogs unreleased eps
    with property.is_open == False; those must be hidden from the ep list."""
    out = []
    prop = (j.get("series") or {}).get("property") or {}
    for d in ("prev_books", "next_books"):
        blk = prop.get(d) or {}
        vals = list(blk.values()) if isinstance(blk, dict) else list(blk)
        for x in vals:
            bid = None
            opened = None
            if isinstance(x, dict):
                for k in ("b_id", "id"):
                    if x.get(k):
                        bid = str(x[k]); break
                iv = x.get("is_opened")
                if iv is not None:
                    opened = bool(iv)
            elif isinstance(x, (int, str)):
                bid = str(x)
            if bid:
                out.append((bid, opened))
    return out


def _ep_flags(j):
    """flat {bid: is_opened} map from one book_meta response."""
    return {b: o for b, o in neighbors(j) if o is not None}


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
    # strip duplicated series title from the ep name: '<series> 1화' → '1화'
    short = re.sub(r"^" + re.escape(series or "") + r"\s*", "", name).strip()
    base = sanitize(f"{head}{short or name}")
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


def _book_meta_safe(b):
    try:
        return b, book_meta(b)
    except Exception:
        return b, None


def _collect_series_ids(root, expect):
    ids = {root}
    flags = {}
    titles = {}
    pending = {root}
    failures = {}
    guard = 0
    limit = max(expect * 4 + 40, 60)
    with ThreadPoolExecutor(max_workers=12) as ex:
        while pending and guard < limit:
            batch = list(pending)[:30]
            pending.clear()
            guard += len(batch)
            futures = {ex.submit(_book_meta_safe, b): b for b in batch}
            for fut in as_completed(futures):
                b, jn = fut.result()
                if jn is None:
                    failures[b] = failures.get(b, 0) + 1
                    if failures[b] < 2:
                        pending.add(b)
                    continue
                s = jn.get("series") or {}
                prop = s.get("property") or {}
                t = (prop.get("title") or jn.get("title") or "").strip()
                if t and re.search(r"\d+\s*(?:화|회|권)", t):
                    titles[b] = t
                for n, opened in neighbors(jn):
                    flags.setdefault(n, opened)
                    if n not in ids:
                        ids.add(n)
                        pending.add(n)
                if expect and len(ids) >= expect:
                    break
            if expect and len(ids) >= expect:
                break
    return sorted(ids, key=int), flags, titles


def _resolve_cache_read(root, expect):
    """Return (order, titles) from a compatible cache, or (None, None)."""
    for name, expected_list in (
            (root + ".v4.json", False),
            (root + ".v3.json", True),
            (root + ".v2.json", True)):
        fp = os.path.join(CACHE_DIR, name)
        if not os.path.exists(fp):
            continue
        try:
            j = json.load(open(fp))
            order = j if expected_list else (j.get("order") or [])
            titles = {} if expected_list else (j.get("titles") or {})
            if len(order) >= max(expect, 1):
                return order, titles
        except Exception:
            pass
        try:
            os.remove(fp)
        except OSError:
            pass
    return None, None


def resolve(query):
    m = re.search(r"(\d{6,})", str(query))
    if not m:
        return []
    start = m.group(1)
    root, expect = start, 0
    try:
        j = book_meta(start)
        s = j.get("series") or {}
        prop = s.get("property") or {}
        root = str(s.get("id") or start)
        expect = int(prop.get("total_book_count")
                     or prop.get("opened_book_count")
                     or s.get("volume") or 0)
    except Exception:
        pass
    cached_order, cached_titles = _resolve_cache_read(root, expect)
    if cached_order is not None:
        if cached_titles:
            merged = dict(load_titles())
            merged.update({k: v for k, v in cached_titles.items() if v})
            save_titles(merged)
        return cached_order
    order, flags, ep_titles = _collect_series_ids(root, expect)
    # hide unreleased eps (property.is_open == False) — RIDI payment route 400s them
    hidden = [b for b, o in flags.items() if o is False and b != root]
    order = [b for b in order if flags.get(b, True)]
    if hidden:
        print(f"    [resolve] hid {len(hidden)} unreleased ep(s): {hidden[:6]}",
              flush=True)
    merged = dict(load_titles())
    merged.update({k: v for k, v in ep_titles.items() if v})
    save_titles(merged)
    if expect > 2 and len(order) < expect:
        return order                          # do NOT cache truncated result
    cf = os.path.join(CACHE_DIR, root + ".v4.json")
    tmp = cf + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"order": order, "titles": ep_titles}, fh)
    os.replace(tmp, cf)
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
    def __init__(self, bids, cache=False):
        import uuid as _u
        self.all = list(bids)
        self.cache = bool(cache)
        self.stream_tok = _u.uuid4().hex[:12]
        self.stream_url = None
        pool_n = len(glob.glob(os.path.join(POOL_DIR, "pool_*.json")))
        if pool_n >= max(3, len(bids)):
            cap = min(MAX_WORKERS, 10)
        elif pool_n >= 3:
            cap = min(MAX_WORKERS, 6)
        else:
            cap = min(MAX_WORKERS, 2)
        self.workers_target = max(1, min(max(len(bids), 1), cap))
        self.pending = list(self.all)
        self.done = {}
        self.failed = []
        self.tries = {}
        self.paid = set()
        self.claims_empty = {}
        self.bid_stage = {}
        self.lock_reason = {}   # bid -> human reason for paid_list entries
        self.log = []
        self.lock = threading.RLock()
        self.lanes = []
        self.max_workers = MAX_WORKERS
        self.initial_workers = self.workers_target
        self.min_workers = max(1, min(2, self.initial_workers))
        self.history = []
        self.alive = True
        self.packaged = False
        self.supervisor_error = ""

    def logf(self, s):
        with self.lock:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] {s}")
            if len(self.log) > 300:
                del self.log[:-300]


JOB = None
LANE_IDX = [0]

# ---- prewarmed identity pool: ready accounts so downloads skip email/signup ----
POOL_DIR = os.path.join(DATA_DIR, "pool")
# unbounded identity farming (user order 2026-10-23): farm forever, staggered.
# POOL_TARGET=0 means no ceiling; a positive value is only an optional floor.
POOL_TARGET = _env_int("POOL_TARGET", 0)
POOL_UNBOUNDED = POOL_TARGET <= 0 or POOL_TARGET >= 100
POOL_LIMIT = POOL_TARGET if POOL_TARGET > 0 else None
PREWARM_CONC = _env_int("PREWARM_CONC", 6)


def rotate_logs():
    """keep lane/pre/dl logs bounded: >8MB → shift to .1 (single history)"""
    for pth in glob.glob(os.path.join(DATA_DIR, "*", "*.log")) + \
            [os.path.join(POOL_DIR, "pre.log")]:
        try:
            if os.path.getsize(pth) > 8 * 1024 * 1024:
                old = pth + ".1"
                os.replace(pth, old)
                open(pth, "w").close()
        except OSError:
            pass


def take_pool():
    """consume one ready identity: prefer still-warm 'active' accounts, else fresh pool.
    Atomic rename prevents two lanes grabbing the same file."""
    try:
        cands = sorted(glob.glob(os.path.join(POOL_DIR, "active_*.json"))) \
            or sorted(glob.glob(os.path.join(POOL_DIR, "pool_*.json")))
        if not cands:
            # fall back to a warmed inuse_ leftover from an interrupted run
            cands = sorted(glob.glob(os.path.join(POOL_DIR, "inuse_*.json")))
        if not cands:
            return None
        src = cands[0]
        base = os.path.basename(src).split("_", 1)[0]     # active/pool/inuse
        dst = os.path.join(POOL_DIR, f"inuse_{int(time.time()*1000)}.json")
        if src != dst:
            os.rename(src, dst)
        return dst
    except Exception:
        return None


# recycle a consumed-but-still-valid identity so free-eps don't burn new ones
def release_active(reuse_path):
    try:
        if reuse_path and os.path.exists(reuse_path) and "pool" in POOL_DIR:
            dst = os.path.join(POOL_DIR,
                               f"active_{int(time.time()*1000)}.json")
            os.rename(reuse_path, dst)
            return True
    except OSError:
        pass
    return False


def spawn_pre(delay=0):   # legacy shim — prewarm_loop owns refill now
    pass


PREWARM_ERR_PATTERNS = {
    "signup": ("signup err", "create-account failed", "validation failed"),
    "activation": ("activation failed", "activation request failed"),
    "login": ("login err", "login/otp failed", "login network error",
              "OTP exchange failed"),
    "adult": ("adult verify failed", "adult verify err", "adult-verify-failed"),
    "network": ("Response ended prematurely", "Temporary failure",
                "Connection reset"),
    "other": ("err:", "account error:"),
}


def prewarm_health(window_bytes=400_000):
    """summarize recent prewarm outcomes from POOL_DIR/pre.log (no secrets)."""
    path = os.path.join(POOL_DIR, "pre.log")
    errors = {k: 0 for k in PREWARM_ERR_PATTERNS}
    out = {"ready": len(glob.glob(os.path.join(POOL_DIR, "pool_*.json"))),
           "building": len([f for f in glob.glob(os.path.join(POOL_DIR, "*.busy"))
                            if time.time() - os.path.getmtime(f) < 240]),
           "target": POOL_LIMIT, "conc": PREWARM_CONC,
           "log_bytes": 0, "log_age_s": None, "scanned_bytes": 0,
           "success_recent": 0, "stall_retries": 0, "errors_recent": errors,
           "error_rate": None}
    try:
        size = os.path.getsize(path)
        out.update(log_bytes=size,
                   log_age_s=round(time.time() - os.path.getmtime(path), 1))
        with open(path, "rb") as fh:
            fh.seek(max(0, size - window_bytes))
            raw = fh.read()
        out["scanned_bytes"] = len(raw)
        for line in raw.decode("utf8", "ignore").splitlines():
            if "[pool] saved" in line:
                out["success_recent"] += 1
                continue
            if "adult verify retry" in line:
                out["stall_retries"] += 1
            for kind, pats in PREWARM_ERR_PATTERNS.items():
                if any(pat in line for pat in pats):
                    errors[kind] += 1
                    break
        attempts = out["success_recent"] + sum(errors.values())
        if attempts:
            out["error_rate"] = round(sum(errors.values()) / attempts, 3)
    except OSError as e:
        out["error"] = str(e)[:120]
    return out


def pool_status():
    ready = len(glob.glob(os.path.join(POOL_DIR, "pool_*.json")))
    busy = len([f for f in glob.glob(os.path.join(POOL_DIR, "*.busy"))
                if time.time() - os.path.getmtime(f) < 240])
    per = 75
    eta = None if POOL_LIMIT is None else (
        ((POOL_LIMIT - ready) * per) // max(busy, 1) if ready < POOL_LIMIT else 0)
    return {"ready": ready, "refilling": busy, "target": POOL_LIMIT,
            "eta_s": eta, "unbounded": POOL_UNBOUNDED}


def prewarm_loop():
    """continuous staggered identity farming — keep ≥POOL_TARGET ready 24/7,
    ≤PREWARM_CONC builds at once, each on its own Databay session/IP, jittered start"""
    while True:
        try:
            n = len(glob.glob(os.path.join(POOL_DIR, "pool_*.json")))
            busy = [f for f in glob.glob(os.path.join(POOL_DIR, "*.busy"))
                    if time.time() - os.path.getmtime(f) < 120]
            for f in glob.glob(os.path.join(POOL_DIR, "*.busy")):
                if time.time() - os.path.getmtime(f) > 180:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            # adaptive cap: while pool is half-empty (or unlimited), allow
            # extra parallel builders so refill catches up fast again
            cap = PREWARM_CONC + (4 if POOL_LIMIT is None or n < POOL_LIMIT * 0.5 else 0)
            if (POOL_LIMIT is None or n < POOL_LIMIT) and len(busy) < cap:
                env = dict(os.environ, PREWARM="1", POOL_DIR=POOL_DIR,
                           PROXY_SEED=os.urandom(4).hex(), STAGE_TIMEOUT="180",
                           STAGGER=str(random.randint(0, 35)))
                try:
                    os.makedirs(POOL_DIR, exist_ok=True)
                    busy_path = os.path.join(
                        POOL_DIR, f"pre_{int(time.time()*1000)}.busy")
                    open(busy_path, "w").close()
                    env["PRE_BUSY"] = busy_path
                    logf = open(os.path.join(POOL_DIR, "pre.log"), "ab")
                    try:
                        subprocess.Popen(
                            [sys.executable, WORKER,
                             os.path.join(DATA_DIR, "pre.state.json"), DATA_DIR],
                            stdout=logf, stderr=subprocess.STDOUT,
                            cwd=APP_ROOT, env=env)
                    finally:
                        logf.close()
                except Exception:
                    try:
                        os.remove(busy_path)
                    except OSError:
                        pass
        except Exception:
            pass
        time.sleep(18)


import random


threading.Thread(target=prewarm_loop, daemon=True).start()


def job_progress(j):
    """live step / percent / eta from lane logs + decrypted file counts"""
    try:
        files, steps, ppe_vals = 0, [], []
        acc_n = None
        retry_pending = False
        for l in j.lanes:
            try:
                tail = open(os.path.join(l["outroot"], "lane.log"), "rb").read()[-1200:]
                a_i, s_i = -1, -1
                ln = tail.decode(errors="ignore").splitlines()
                for k, line in enumerate(ln):
                    macc = re.search(r"account #(\d+)", line)
                    if macc:
                        acc_n = max(acc_n or 1, int(macc.group(1)))
                        a_i = k
                    if line.startswith("STEP"):
                        steps.append(line.strip())
                        s_i = k
                if a_i > s_i:      # last identity recycle is newest event
                    retry_pending = True
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
        if acc_n and acc_n > 1 and retry_pending:
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
        ce_cnt = {}
        lock_here = set()
        try:
            full_log = open(logtail, "rb").read()[-4000:].decode(errors="ignore")
            for line in full_log.splitlines():
                mpl = re.search(r"PURCHASE_LOCK (\d+)", line)
                if mpl:
                    lock_here.add(mpl.group(1))
                mpaid = re.search(r"CLAIMS_EMPTY ([\d,]+)", line)
                if mpaid:
                    for x in mpaid.group(1).split(","):
                        if x.isdigit():
                            ce_cnt[x] = ce_cnt.get(x, 0) + 1
        except Exception:
            pass
        with job.lock:   # job-lifetime counter (survives lane log rotation)
            for bb, cc in ce_cnt.items():
                job.claims_empty[bb] = min(9, job.claims_empty.get(bb, 0) + cc)
        paid_here = {b for b, c in ce_cnt.items()
                     if c >= 2 or job.claims_empty.get(b, 0) >= 2} | lock_here
        with job.lock:
            job.done.update(got)
            newly_paid = ((paid_here | lock_here)
                          & {str(b) for b in bids}) - set(job.done)
            for lb in lock_here & newly_paid:
                job.lock_reason[lb] = "RIDI purchase-lock (Selection-only ep)"
            for lb in paid_here & newly_paid:
                job.lock_reason.setdefault(
                    lb, "pool identity ไม่มี coupon ว่าง / no free claim path")
            job.paid |= newly_paid
            miss = [b for b in bids if str(b) not in got and str(b) not in job.paid]
            for b in miss:
                job.tries[b] = job.tries.get(b, 0) + 1
            if miss:
                job.last_fail_t = time.time()
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
                sb = str(b)
                if sb not in job.done and sb not in job.paid and b not in job.pending:
                    job.pending.append(b)


def aimd_supervisor(job):
    import traceback as _tb
    last_scale = time.time()
    last_spawn = time.time()
    while True:
        try:
            with job.lock:
                job.pending = [b for b in job.pending if str(b) not in job.done]
                n_lanes = len(job.lanes)
                target = job.workers_target
                have_work = bool(job.pending)
            recent = list(job.history[-4:])
            now = time.time()
            failures = sum(1 for x in recent if not x)
            if len(recent) == 4 and failures == 0 and now - last_scale > 20:
                increased = False
                with job.lock:
                    if job.workers_target < job.max_workers:
                        job.workers_target += 1
                        increased = True
                if increased:
                    job.logf(f"AIMD additive increase → workers={job.workers_target}")
                last_scale = now
            elif failures >= 2 and now - last_scale > 45:
                new_target = None
                with job.lock:
                    nt = max(job.min_workers, int(job.workers_target * 0.7) or 1)
                    if nt != job.workers_target:
                        job.workers_target = nt
                        new_target = nt
                if new_target is not None:
                    job.logf(f"AIMD multiplicative decrease → workers={new_target}")
                last_scale = now

            i = 0
            while have_work and (n_lanes + i) < target and i < 4:
                with job.lock:
                    take = max(1, len(job.pending) // max(target - n_lanes, 1))
                    bids = []
                    for b in list(job.pending)[:take]:
                        if str(b) not in job.done:
                            if str(b) in job.paid:
                                job.pending.remove(b); continue
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
                pool_file = take_pool()
                if pool_file:
                    env["REUSE_ACCT"] = pool_file
                    job.logf(f"pooled account → instant start ({os.path.basename(pool_file)})")
                spawn_pre(0)
                lf = getattr(job, "last_fail_t", 0)
                if time.time() - lf < 25:
                    time.sleep(max(0, 26 - (time.time() - lf)))
                pr = subprocess.Popen([sys.executable, WORKER, sf, out] + bids,
                                      stdout=open(os.path.join(out, "lane.log"), "ab"),
                                      stderr=subprocess.STDOUT, cwd=APP_ROOT, env=env)
                lane = {"proc": pr, "statefile": sf, "outroot": out, "bids": bids}
                with job.lock:
                    job.lanes.append(lane)
                threading.Thread(target=lane_run, args=(job, lane), daemon=True).start()
                last_spawn = time.time()
                i += 1

            time.sleep(4)
            need_pkg = False
            with job.lock:
                need_pkg = not job.pending and len(job.lanes) == 0
                if need_pkg:
                    job.alive = False
            if need_pkg:
                job.logf("ALL DONE")
                titles = load_titles(); dirty = False
                for b in list(job.done):
                    if b not in titles:
                        try:
                            name = (book_meta(b).get("title") or "").strip()
                            if name:
                                titles[b] = name; dirty = True
                        except Exception:
                            pass
                if dirty:
                    save_titles(titles)
                package_on_finish(job)
                return

            with job.lock:
                stalled = (bool(job.pending) and not job.lanes
                           and job.alive
                           and time.time() - last_spawn > 120)
            if stalled:
                job.last_fail_t = 0
                job.history.clear()
                job.logf("stalled job: pending>0 lanes=0 → force respawn")
        except Exception:
            err = _tb.format_exc()
            job.supervisor_error = err[-500:]
            print("[supervisor] " + err[-500:], flush=True)
            job.logf("supervisor error → auto-restarting loop")
            time.sleep(5)


STAGE_LABELS = (
    ("reusing pooled account", "popping identity from pool"),
    ("making new email", "building identity · tempmail"),
    ("signup", "building identity · signup"),
    ("waiting verification mail", "identity · waiting mail (tempmail)"),
    ("verify email", "identity · activating"),
    ("login + otp", "identity · login+otp"),
    ("claim free coupons", "checking claim route / claiming"),
    ("downloading pages", "downloading pages"),
)


def _bid_stage(j):
    """map pending bids → the stage their lane is actually at right now"""
    out = {}
    try:
        for l in j.lanes:
            tail = open(os.path.join(l["outroot"], "lane.log"), "rb").read()[-1200:]
            last = ""
            cooling = False
            for line in tail.decode(errors="ignore").splitlines():
                if line.startswith("STEP"):
                    last = line.strip()[5:].strip(); cooling = False
                elif "cooling 45s" in line or "[tmail retry]" in line \
                        or "Max retries" in line:
                    cooling = True
            if not cooling:
                for pat, label in STAGE_LABELS:
                    if pat in last:
                        last = label; break
            else:
                last += " · provider/proxy flake — auto-retrying"
            for b in l.get("bids") or []:
                sb = str(b)
                if sb in j.claims_empty and j.claims_empty[sb] >= 1 \
                        and "claims" not in last.lower():
                    out[sb] = "no free path on identity — confirming…"
                else:
                    out.setdefault(sb, last or "queued")
    except Exception:
        pass
    return out


def package_on_finish(job):
    """cache → library layout; default (no-cache) → stage stream zip for clicker"""
    with job.lock:
        if job.packaged:
            return
        job.packaged = True

    def _w():
        try:
            done_bids = list(job.done)   # GIL-safe snapshot, no lock needed
            print(f"[pkg] start bids={done_bids} cache={job.cache}", flush=True)
        except Exception as e:
            print(f"[pkg] snapshot err {e}", flush=True); return
        try:
            if job.cache:
                for b in done_bids:
                    migrate_to_library(b)
                job.stream_url = None
                job.logf(f"library updated ({len(done_bids)} eps cached)")
                return
            d = os.path.join(DATA_DIR, "dl", job.stream_tok)
            os.makedirs(d, exist_ok=True)
            if len(done_bids) == 1:
                b = str(done_bids[0])
                _, folder = ep_label(b)
                fname = sanitize(folder, 70) or ("EP" + b)
                arc_base = folder
            else:
                m0 = load_meta()
                root0 = (m0.get("root") or {}).get(str(done_bids[0]), "")
                sname = sanitize((m0.get("title") or {}).get(root0, "") or "export", 60)
                fname = f"{sname}_selected"
                arc_base = None       # per-ep folders inside
            zfpath = os.path.join(d, fname + ".zip")
            with zipfile.ZipFile(zfpath, "w", zipfile.ZIP_STORED) as z:
                for b in sorted(done_bids, key=int):
                    pages = find_pages(b)
                    if not pages:
                        continue
                    _, folder = ep_label(b)
                    for f in pages:
                        z.write(f, arcname=(arc_base and f"{folder}/{os.path.basename(f)}"
                                            or f"{folder}/{os.path.basename(f)}"))
            job.stream_url = f"/dl/{job.stream_tok}/{urllib.parse.quote(fname)}.zip"
            job.logf(f"stream zip ready: {os.path.basename(zfpath)}")
            if not job.cache:   # no-library mode → drop working copies
                for b in done_bids:
                    for dd in DL_DIRS:
                        shutil.rmtree(os.path.join(APP_ROOT, dd, str(b)),
                                      ignore_errors=True)
        except Exception as e:
            import traceback as _tb
            print("[pkg] ERR", _tb.format_exc()[-400:], flush=True)
            job.logf(f"package err {str(e)[:90]}")

    threading.Thread(target=_w, daemon=True).start()


RESULT_MAX_AGE_HOURS = float(os.environ.get("RESULT_MAX_AGE_HOURS", "0.5"))

_T0_APP = time.time()


def _recycle_inuse(now):
    """inuse_*.json leak self-healing: identities consumed by workers whose lane
    died mid-job never got recycled back; anything older than 15 min goes to the
    active_* tier again (cookie probably still valid), hard-delete beyond 4000."""
    pool_files = sorted(glob.glob(os.path.join(POOL_DIR, "inuse_*.json")),
                        key=os.path.getmtime)
    back = 0
    for f in pool_files:
        age = now - os.path.getmtime(f)
        dst = os.path.join(POOL_DIR,
                           "active_%d%d.json" % (int(now), back * 7))
        if age > 900 and not os.path.exists(dst):
            try:
                os.rename(f, dst)
                back += 1
            except OSError:
                pass
    stale = sorted(glob.glob(os.path.join(POOL_DIR, "inuse_*.json")),
                   key=os.path.getmtime)
    for f in stale[:max(0, len(stale) - 4000)]:
        try:
            os.remove(f)
        except OSError:
            pass


def dl_sweeper():
    """transient result store: mtime-based purge after RESULT_MAX_AGE_HOURS (default 48h);
    within window re-download serves instantly"""
    while True:
        time.sleep(300)
        rotate_logs()
        try:
            root = os.path.join(DATA_DIR, "dl")
            now = time.time()
            cut = now - RESULT_MAX_AGE_HOURS * 3600
            for d in glob.glob(os.path.join(root, "*")):
                try:
                    newest = max(os.path.getmtime(p) for p in glob.glob(
                        os.path.join(d, "*")))
                    if newest < cut:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
        except Exception:
            pass
        _recycle_inuse(time.time())


threading.Thread(target=dl_sweeper, daemon=True).start()


def start_job(bids, cache=False):
    global JOB
    if not bids:
        return None
    job = Job(bids, cache=cache)
    JOB = job
    threading.Thread(target=aimd_supervisor, args=(job,), daemon=True).start()
    return job


# ---------------- UI ----------------
PAGE = r"""<!doctype html><html lang=th><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>RIDI DL</title><style>
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
.cachewrap{margin-top:30px}.chead{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.cep{display:flex;gap:10px;align-items:center;border-bottom:1px solid var(--line);padding:8px 4px;font-size:13px}
.cep img{width:34px;height:46px;object-fit:cover;border-radius:5px;flex:none;background:#222}
.cep a{color:#9cf;text-decoration:none;font-size:12px;white-space:nowrap}
.dbtn{background:#3a2f35;border:1px solid #55404a;color:#ffb3c8;padding:6px 10px;border-radius:8px;
font-size:12px;cursor:pointer}
#resultbox{background:#172a1d;border:1px solid #2e5838}
#resultbox a{background:#244a2e;border:1px solid #487b55;color:#d8ffd8;padding:7px 12px;border-radius:8px;
text-decoration:none;font-weight:700}
select,input[type=number]{background:var(--card);border:1px solid var(--line);color:#eee;
padding:7px 10px;border-radius:8px;font-size:13px}
</style></head><body>
<div id=pbar></div>
<header><div class=logo>🔷 RIDI DL</div>
<input id=q type=text placeholder="พิมพ์ชื่อเรื่อง หรือวาง URL/รหัสตอน">
<button onclick=search()>ค้นหา</button><span class=pill id=wlab>idle</span>
<span class=pill id=poolpill style=color:#ffd479>identity pool …</span>__CROSS_LINK__</header>
<main id=out></main>
<section class=cachewrap id=resultbox style=display:none></section>
<section class=cachewrap id=cache></section>
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
 if(j.error)return $('out').innerHTML='<p class=muted>'+esc(j.error)+'</p>';EPS=j.eps;CUR=Object.assign({q},j);
 SEL=new Set();renderEps()}
function openTitle(sid){openByQuery(sid)}
function renderEps(){
 let h=`<div class=back onclick=home()>‹ เปลี่ยนเรื่อง</div>
 <div class=serieshead><img src='${CUR.cover||''}' onerror="this.style.opacity=.15">
 <div><h3 style=margin:0>${esc(CUR.title||'')}</h3>
 <span class=muted>${EPS.length} episodes · โหลดแล้ว ${EPS.filter(e=>e.pages).length}</span></div></div>`;
 let ck=`<div class=muted style="margin:2px 0 8px">💡 ตอนฟรี = เร็ว (~60-90s) · ตอนเสียตัง = ใช้ free-selection coupon ของ identity (ETA ~2min); ถ้า coupon หมดจะ fail-fast เป็น 💸 no-free-path แทนการค้าง</div>
 <label style="display:flex;gap:6px;align-items:center;font-size:12px;color:#aaa">
  <input type=checkbox id=cacheck> cache ไว้บน server</label>`;
 EPS.forEach((e,i)=>{const tag=e.paid?`<span class=muted title='${e.reason||''}'>💸 no free path</span>`
   :(e.pages?`<span><a href='/ep/${e.b}'>cached — read now (${e.pages}p)</a> <a href='/ep/${e.b}.zip'>zip ⬇</a></span>`
    :'<span class=muted>—</span>');
  h+=`<div class=eprow id=r${i}><label style=display:flex;gap:10px;align-items:center;flex:1>
  <input type=checkbox class=sel data-b='${e.b}' ${(e.pages||e.paid?'disabled':'')} onchange=tog(this)>
  <span class=no>${String(i+1).padStart(2,'0')}</span> <span style=flex:1 title='no-cache mode: โหลดซ้ำ = identity ใหม่ทุกครั้ง'>${esc(e.t)}</span></label>`+
   tag+'</div>'});
 if(EPS.some(e=>!e.pages&&!e.paid)){h+=''}
 h+=`<div style="margin-top:8px">${ck}</div>`;
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
let DL_BIDS=[],STREAMED=false,ZIP_WAIT=0;
function clickA(href){const a=document.createElement('a');a.href=href;a.download='';document.body.appendChild(a);a.click();setTimeout(()=>a.remove(),400)}
function showResult(j){
 const b=$('resultbox');if(!b)return;
 if(!j.stream_url){b.style.display='none';b.innerHTML='';return}
 const note=j.cache?'เก็บเข้า server cache แล้ว':'ไม่เก็บไฟล์บน server · ZIP พร้อมโหลดภายใน 30 นาที';
 b.innerHTML=`<h3>✅ โหลดเสร็จแล้ว <span class=muted>(${j.done_n} ตอน)</span></h3>
  <p><a href='${j.stream_url}' download>📦 โหลด zip เข้าเครื่อง</a> <span class=muted>${note}</span></p>`;
 b.style.display='block'}
const CK=()=>$('cacheck')?$('cacheck').checked:false;
async function dlSelected(){
 const missing=[...SEL].filter(b=>!EPS.find(e=>e.b===b&&e.pages));
 if(!missing.length)return alert('เลือกตอนที่ยังไม่ได้โหลดก่อน');
 DL_BIDS=missing.slice();STREAMED=false;ZIP_WAIT=0;
 const r=await fetch('/api/download',{method:'POST',body:JSON.stringify({bids:missing,cache:CK()})});
 await r.json();
 statusShow({progress:{step:'queued — picking pooled identity…',pct:0},workers_now:0,max_workers:16,done_n:0,pending_n:missing.length,fail_n:0});
 poll(true)}
function bundleZip(){const done=EPS.filter(e=>e.pages).map(e=>e.b);location.href='/bundle.zip?bids='+done.join(',')}
function home(){$('out').innerHTML='<h3>…</h3>';$('actionbar').style.display='none';$('q').value='';
 clearInterval(POLLT);POLLT=null;$('pbar').style.display='none';$('wlab').textContent='idle'}
function status(t){const s=$('status')||null}
function poll(force){
 if(POLLT&&!force)return; if(!POLLT)POLLT=setInterval(()=>poll(true),4000);
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
  const PR=j.paid_reasons||{};
  (j.paid_list||[]).forEach(b=>{const e=EPS.find(x=>x.b===b);
   if(e&&!e.paid){e.paid=true;e.reason=(PR[b]||'no free claim path');changed=true;
    statusShow(j)}});
  EPS.forEach((e,i)=>{
   if(j.done_list.includes(e.b)&&!e.pages){
    e.pages=(j.pages||{})[e.b]||1;changed=true;
    const row=$('r'+i);if(row)row.outerHTML=rowHTML(e,i,true)}});
   if(j.pool_ready!=null)$('poolpill').textContent=j.pool_target==null?`identity farm: ${j.pool_ready}+`:`identity pool ${j.pool_ready}/${j.pool_target}`;
   showResult(j);
   if(changed&&CUR)renderEps();
   if(!j.active&&tot){
    if(!j.stream_url&&!j.cache&&ZIP_WAIT++<10)return;
    clearInterval(POLLT);POLLT=null;statusHide();
    if((DL_BIDS||[]).length&&!STREAMED&&j.stream_url){STREAMED=true;clickA(j.stream_url)}
    DL_BIDS=[];loadLib();if(!j.cache&&CUR&&CUR.q)openByQuery(CUR.q)}
 }).catch(()=>{})}
async function poolTick(){
 try{const r=await fetch('/api/pool');const j=await r.json();
  if(j.unbounded){
   let t=`identity farm: ${j.ready} ready`;
   if(j.refilling)t+=` · farming ${j.refilling}`;
   $('poolpill').textContent=t;
  }else{
   let t=`identity pool ${j.ready}/${j.target}`;
   if(j.ready<j.target)t+=` · refilling ${j.refilling} (ETA ~${Math.max(1,Math.round(j.eta_s/60))}m)`;
   $('poolpill').textContent=t;}
 }catch(e){}}
setInterval(poolTick,6000);poolTick();
async function loadLib(){
 const c=$('cache');
 try{const r=await fetch('/api/library');const j=await r.json();
  if(!j.series||!j.series.length){c.innerHTML='<h3>🗄 Cache on server</h3><p class=muted>ว่าง — default mode ส่ง zip เข้าเครื่องคุณตรงๆ ไม่เก็บไฟล์บน server (tick “cache” ตอนโหลดถ้าจะเก็บ)</p>';return}
  let h=`<h3>🗄 Cache on server <span class=muted>(storage on server · total ${(j.total_mb||0).toFixed?j.total_mb:j.total_mb} MB · disk ${Math.round(j.disk_pct)}%)</span></h3>`;
  j.series.forEach(s=>{
   h+=`<div style="margin:12px 0 2px;font-weight:700">${esc(s.name)}
    <button class=dbtn onclick=delSeries('${encodeURIComponent(s.name)}')>🗑 delete series</button></div>`;
   s.eps.forEach(e=>{h+=`<div class=cep><img src='/page/${e.b}/page_001.jpg' onerror="this.style.opacity=.15">
    <span class=no>${String(e.no??'').padStart(2,'0')}</span><span style=flex:1>${esc(e.slug)}</span>
    <span class=muted>${e.pages}p · ${e.mb}MB${e.created?' · '+e.created:''}</span>
    <a href=/ep/${e.b}>📖 read</a><a href='/ep/${e.b}.zip'>⬇ zip</a>
    <button class=dbtn onclick=delEps('${e.b}')>🗑</button></div>`})});
  h+='<div style="margin-top:14px" class=muted>TTL auto-purge: <select id=ttlsel><option value=0>off</option><option value=7>7 days</option><option value=30>30 days</option></select> disk max <input type=number id=dknum value=80 style=width:60px>% <button class=dbtn onclick=saveSet()>save settings</button></div>';
  c.innerHTML=h;
  const r2=await fetch('/api/settings',{method:'POST',body:JSON.stringify({op:'get'})});const s2=await r2.json();
  if($('ttlsel'))$('ttlsel').value=(s2.ttl_days||0);if($('dknum'))$('dknum').value=(s2.disk_pct||80);
 }catch(e){c.innerHTML=''}}
async function saveSet(){
 await fetch('/api/settings',{method:'POST',body:JSON.stringify({ttl_days:+$('ttlsel').value,disk_pct:+$('dknum').value})});
 loadLib()}
function delEps(b){if(!confirm('ลบตอนนี้จาก server cache?'))return;
 fetch('/api/delete',{method:'POST',body:JSON.stringify({bids:[b]})}).then(loadLib)}
function delSeries(n){if(!confirm('ลบทุกตอนของซีรีส์นี้จาก server cache?'))return;
 fetch('/api/delete',{method:'POST',body:JSON.stringify({series:n})}).then(loadLib)}
function rowHTML(e,i,fresh){
 const tag=e.paid?`<span class=muted title='${e.reason||''}'>💸 no free path</span>`
   :(e.pages?`<span><a href='/ep/${e.b}'>cached — read now (${e.pages}p)</a> <a href='/ep/${e.b}.zip'>zip ⬇</a></span>`
    :'<span class=muted>⏳</span>');
 return `<div class=eprow id=r${i}><label style=display:flex;gap:10px;align-items:center;flex:1>
 <input type=checkbox class=sel data-b='${e.b}' ${(e.pages||e.paid?'disabled':'')} onchange=tog(this)>
 <span class=no>${String(i+1).padStart(2,'0')}</span> <span style=flex:1>${esc(e.t)}</span></label>`+
 tag+'</div>'}
function statusShow(j){
 let s=$('status');
 if(!s){s=document.createElement('pre');s.id='status';$('out').before(s)}
 s.style.display='block';
 const pr=j.progress||{};
 s.textContent=`⏬ ${pr.step||'…'} · ${pr.pct??''}${pr.eta_min!=null?('· ETA ~'+pr.eta_min+'m'):''}\\nworkers=${j.workers_now}/${j.max_workers||16} ✅${j.done_n} ⏳${j.pending_n} ❌${j.fail_n}`;
 const bs=j.bid_status||{};
 const pend=EPS.filter(e=>!e.pages&&!e.paid&&bs[e.b]).slice(0,4);
 if(pend.length)s.textContent+='\\n'+pend.map(e=>`· ${(e.t||'').slice(0,28)} — ${bs[e.b]}`).join('\\n')}
function statusHide(){const s=$('status');if(s)s.style.display='none'}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')search()});
loadLib();poolTick();poll();
</script></body></html>"""


LZ_PAGE = r"""<!doctype html><html lang=th><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>LEZHIN DL</title>
<meta property=og:title content="LEZHIN DL">
<meta name=description content="Lezhin webtoon episode ZIP downloader">
<style>
:root{--pink:#34d399;--bg:#0c1013;--card:#151a1f;--line:#263038}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eee;
font-family:'Noto Sans',system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:rgba(12,16,19,.96);backdrop-filter:blur(6px);z-index:5;flex-wrap:wrap}
.logo{font-weight:900;font-size:19px;color:var(--pink);letter-spacing:.04em}
.sub{font-size:12px;color:#8996a3;white-space:nowrap}
input[type=text]{flex:1;min-width:210px;background:var(--card);border:1px solid var(--line);color:#eee;
padding:11px 14px;border-radius:10px;font-size:14px}
button{background:linear-gradient(135deg,#10b981,#047857);color:#fff;border:0;padding:11px 18px;
border-radius:10px;font-weight:700;cursor:pointer}
button:disabled{filter:grayscale(.5);opacity:.65;cursor:wait}
a.link{color:#8dd3b6;font-size:13px;text-decoration:none}.back{color:#888;cursor:pointer;font-size:13px}
main{max-width:920px;margin:0 auto;padding:18px 16px 80px}
.count{float:right;color:#8996a3;font-size:12px;font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:12px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer;transition:.15s}
.card:hover{transform:translateY(-2px);border-color:var(--pink)}
.card img{width:100%;aspect-ratio:3/4;object-fit:cover;display:block;background:#182027}
.card b{display:block;font-size:13px;padding:8px 10px 2px;line-height:1.35}
.card span{display:block;font-size:11px;color:#8996a3;padding:0 10px 10px}
.age{display:inline-block;background:#5a1426;color:#ff94a9;border-radius:4px;padding:1px 5px;font-weight:700}
.head{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:14px 0}
.head img{width:110px;aspect-ratio:3/4;object-fit:cover;border-radius:10px;flex:none;background:#182027}
.head h2{margin:0 0 6px;font-size:20px}.artist{color:#8dd3b6;font-size:13px;margin:6px 0}
.genre{color:#9bb0c1;font-size:12px}.synopsis{color:#aab5bf;font-size:12px;line-height:1.5;margin-top:9px}
.ep{display:grid;grid-template:1fr/48px 54px 1fr auto auto;gap:10px;align-items:center;border-top:1px solid var(--line);padding:10px 2px}
.ep .n{font-weight:700;color:var(--pink);text-align:right}.ep img{width:54px;aspect-ratio:3/4;object-fit:cover;border-radius:5px}
.ep .t{font-size:14px}.ep .coin{font-size:12px;color:#ffd166;min-width:64px;text-align:right}
.ep .owned{font-size:11px;color:#7bd88f}.zip{padding:7px 11px;font-size:12px;border-radius:8px}
.err{color:#ffb4ab;background:#2a1317;border:1px solid #5b2730;border-radius:10px;padding:10px 12px}
</style></head><body>
<header><div class=logo>LEZHIN DL</div><span class=sub>Episode ZIP Downloader</span>
<input id=q type=text placeholder="พิมพ์ชื่อเรื่อง Lezhin…"><button onclick=search()>ค้นหา</button>
__CROSS_LINK__</header>
<main id=out><h3>ค้นหาเรื่องจาก Lezhin แล้วเลือกเพื่อโหลด ZIP เดี่ยวหรือหลายตอน</h3></main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>(s+'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp'}[c]));
async function search(){const q=$('q').value.trim();if(!q)return;const out=$('out');out.innerHTML='<h3>กำลังค้นหา…</h3>';
 const r=await fetch('/api/lz/search?'+new URLSearchParams({q,limit:30}));const j=await r.json();
 if(!r.ok||j.error){out.innerHTML=`<div class=err>${esc(j.error||'ค้นหาไม่สำเร็จ')}</div>`;return}
 let h=`<h3>${esc(q)} <span class=count>${j.count} เรื่อง</span></h3><div class=grid>`;
 (j.items||[]).forEach(x=>{h+=`<div class=card onclick="openComic('${esc(x.alias)}')"><img loading=lazy src="${esc(x.cover)}" onerror="this.hidden=true"><b>${esc(x.title)}</b><span>${esc(x.artists||'—')}<br><span class=age>${esc(x.rating)}</span></span></div>`});
 out.innerHTML=h+'</div>'}
let LZ_ALIAS='',LZ_TITLE='',LZ_EPISODES=[],LZ_SEL=new Set();
function lzSeqs(){return [...LZ_SEL].sort((a,b)=>a-b)}
function lzUpdate(){const n=lzSeqs().length;const btn=$('lzdl');if(btn)btn.textContent=n?`ดาวน์โหลด ${n} ตอน`:'เลือกตอนก่อน';
 document.querySelectorAll('.lzep').forEach(x=>{LZ_SEL.has(+x.value)?x.checked=true:x.checked=false})}
function lzSelect(fn){LZ_SEL.clear();LZ_EPISODES.filter(fn).forEach(e=>LZ_SEL.add(e.seq));lzUpdate()}
function lzClear(){LZ_SEL.clear();lzUpdate()}
function lzZipName(prefix){return (prefix+': '+LZ_TITLE).replace(/[\\/:*?"<>|]+/g,' ').replace(/\s+/g,' ').trim()+'.zip'}
async function openComic(alias){const out=$('out');out.innerHTML='<h3>กำลังโหลดรายละเอียด…</h3>';
 const r=await fetch('/api/lz/detail?'+new URLSearchParams({alias}));const j=await r.json();
 if(!r.ok||j.error){out.innerHTML=`<div class=err>${esc(j.error||'โหลดไม่สำเร็จ')}</div>`;return}
 LZ_ALIAS=alias;LZ_TITLE=j.title||alias;LZ_EPISODES=j.episodes||[];LZ_SEL.clear();
 let h=`<span class=back onclick=search()>← กลับผลการค้นหา</span><div class=head><img src="${esc(j.cover)}" onerror="this.hidden=true"><div><h2>${esc(j.title)}</h2><div class=artist>${esc(j.artists||'—')}</div><div class=genre>${esc((j.genres||[]).join(', '))} · <span class=age>${esc(j.rating)}</span></div><div class=synopsis>${esc(j.synopsis)}</div></div></div><h3>ตอนทั้งหมด <span class=count>${LZ_EPISODES.length}</span></h3>`;
 h+=`<div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0"><button onclick="lzSelect(e=>e.purchased||e.coin==0)">เลือกซื้อแล้ว/ฟรี</button><button onclick="lzSelect(()=>true)">เลือกทั้งหมด</button><button onclick="lzClear()">ล้าง</button><button id=lzdl onclick="lzDl(this)" disabled>เลือกตอนก่อน</button><label style="font-size:13px;color:#8996a3;display:flex;align-items:center;gap:5px"><input id=lzbuy type=checkbox>อนุญาตซื้อตอนที่เลือก</label></div>`;
 LZ_EPISODES.forEach(e=>{h+=`<div class=ep><label class=n><input class=lzep type=checkbox value="${e.seq}" onchange="LZ_SEL.has(+this.value)?LZ_SEL.delete(+this.value):LZ_SEL.add(+this.value);lzUpdate()">${String(e.seq).padStart(2,'0')}</label><img loading=lazy src="${esc(e.cover)}" onerror="this.hidden=true"><span class=t>${esc(e.title)}</span><span>${e.purchased?'<span class=owned>ซื้อแล้ว</span>':(e.coin?'<span class=coin>'+e.coin+' 코인</span>':'<span class=coin>ฟรี</span>')}</span><button class=zip onclick="download('${esc(alias)}',${e.seq},this)">ZIP</button></div>`});
 out.innerHTML=h;lzUpdate()}
async function download(alias,seq,btn){btn.disabled=true;btn.textContent='กำลังซื้อ/โหลด…';
 try{const r=await fetch('/api/lz/download?'+new URLSearchParams({alias,seq}));
  if(!r.ok){const e=await r.json().catch(()=>({error:'โหลดไม่สำเร็จ'}));alert(e.error||'โหลดไม่สำเร็จ');return}
  const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');
  a.href=url;a.download=alias+'-'+seq+'.zip';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),120000);
 }finally{btn.disabled=false;btn.textContent='ZIP'}}
async function lzDl(btn){const seqs=lzSeqs();if(!seqs.length)return alert('เลือกตอนก่อน');
 btn.disabled=true;const old=btn.textContent;btn.textContent='กำลังโหลด…';
 try{const r=await fetch('/api/lz/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alias:LZ_ALIAS,seqs,buy:$('lzbuy').checked})});
  if(!r.ok){const e=await r.json().catch(()=>({error:'โหลดไม่สำเร็จ'}));alert(e.error||'โหลดไม่สำเร็จ');return}
  let meta={};try{meta=JSON.parse(decodeURIComponent(r.headers.get('X-LZ-Result')||''))}catch(_){}
  const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');
  a.href=url;a.download=lzZipName(seqs.length>1?seqs[0]+'-'+seqs[seqs.length-1]:String(seqs[0]));document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),120000);
  const errs=meta.errors||{};const missing=meta.missing||[];
  if(missing.length||Object.keys(errs).length){const first=Object.values(errs)[0]||'';alert(`โหลดสำเร็จ ${meta.done?meta.done.length:0}/${seqs.length} ตอน${first?' · '+first:''}`)}
 }finally{btn.disabled=false;btn.textContent=old;lzUpdate()}}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')search()});
</script></body></html>"""


AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASS = os.environ.get("AUTH_PASS", "")   # set via Coolify .env only

SITE_MODE = (os.environ.get("SITE_MODE") or "lezhin").strip().lower()
RIDI_ONLY = SITE_MODE in {"ridi", "r", "r"}
CROSS_URL = (os.environ.get("LEZHIN_URL" if RIDI_ONLY else "RIDI_URL") or "").strip()
CROSS_LABEL = "ใช้ Lezhin DL" if RIDI_ONLY else "ใช้ RIDI เดิม"
CROSS_LINK = (
    f'<a class=link href="{html.escape(CROSS_URL)}" style="color:#9cf;font-size:13px;text-decoration:none;margin-left:auto">{CROSS_LABEL}</a>'
    if CROSS_URL else ""
)


def _site_page(template):
    return template.replace("__CROSS_LINK__", CROSS_LINK)


def check_auth(handler):
    """optional Basic Auth — enabled when both AUTH_USER/AUTH_PASS envs are set"""
    if not (AUTH_USER and AUTH_PASS):
        return True
    h = handler.headers.get("Authorization") or ""
    import base64 as _b64
    try:
        u, _, p = _b64.b64decode(h.split(None, 1)[1]).decode().partition(":")
    except Exception:
        u = p = ""
    if not (u == AUTH_USER and p == AUTH_PASS):
        handler.send_response(401)
        handler.send_header("WWW-Authenticate", 'Basic realm="rididl"')
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return False
    return True


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle(self):
        try:
            BaseHTTPRequestHandler.handle(self)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not check_auth(self):
            return
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
            cache = bool(data.get("cache"))
            missing = sorted({b for b in bids if not find_pages(b)}, key=int)
            job = start_job(missing, cache=cache)
            self._json({"started": True, "missing": missing,
                        "token": getattr(job, "stream_tok", "")})
        elif p == "/api/lz/download":
            alias = str(data.get("alias") or "").strip().strip("/")
            raw = data.get("seqs") or []
            if isinstance(raw, str):
                raw = re.split(r"[,\s]+", raw)
            try:
                seqs = [int(x) for x in raw if str(x).strip()]
            except (TypeError, ValueError):
                return self._json({"error": "invalid episode list"}, 400)
            try:
                info, blob, done, errors, missing = LZ.download_many(alias, seqs, bool(data.get("buy")))
                title = info.get("title") or alias
                label = title if len(done) == 1 else f"{title} {done[0]}-{done[-1]}"
                ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "", label)[:70].strip() or alias
                fn = urllib.parse.quote(label + ".zip")
                meta = urllib.parse.quote(json.dumps(
                    {"done": done, "errors": errors, "missing": missing}, ensure_ascii=False))
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{ascii_name}.zip"; filename*=UTF-8\'\'{fn}')
                self.send_header("X-LZ-Result", meta[:4000])
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            except Exception as error:
                return self._json({"error": str(error)}, 500)
        elif p == "/api/library":
            idx = lib_scan()
            series = {}
            for b, e in sorted(idx.items(), key=lambda kv: (kv[1].get("no") or 0)):
                g = series.setdefault(e.get("series") or "?", [])
                g.append({"b": b, "no": e.get("no"), "slug": e.get("slug"),
                          "pages": e["pages"], "mb": round(e["bytes"] / 1048576, 1),
                          "created": e.get("created")[:16] if e.get("created") else ""})
            total = sum(x["mb"] for s in series.values() for x in s)
            return self._json({
                "series": [{"name": k, "eps": v} for k, v in series.items()],
                "total_mb": round(total, 1),
                "disk_pct": disk_pct_of(DATA_DIR)})
        elif p == "/api/pool":
            return self._json(pool_status())
        elif p == "/api/settings":
            if data.get("op") == "get":
                return self._json(load_settings())
            s = load_settings()
            try:
                s["ttl_days"] = int(data.get("ttl_days") or 0)
                s["disk_pct"] = min(95, max(50, int(data.get("disk_pct") or 80)))
                save_settings(s)
            except Exception:
                pass
            return self._json({"ok": True, "settings": s})
        elif p == "/api/delete":
            n = delete_library(bids=data.get("bids"), series=data.get("series"))
            self._json({"deleted": n})
        elif p == "/api/clean":
            if JOB and JOB.alive:
                return self._json({"ok": False, "error": "download job still running"}, 409)
            purge_transient_downloads()
            self._json({"ok": True})
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

    def do_HEAD(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/healthz":
            b = json.dumps({"ok": True,
                            "uptime_s": round(time.time() - _T0_APP),
                            "pool_ready": len(glob.glob(os.path.join(
                                POOL_DIR, "pool_*.json")))}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); return
        if not check_auth(self):
            return
        m = re.match(r"^/ep/(\d+)\.zip$", p)
        if m:
            pages = find_pages(m.group(1))
            if not pages:
                b = b"not downloaded yet"
                self.send_response(404); self.send_header("Content-Length", str(len(b))); self.end_headers(); return
            name, folder = ep_label(m.group(1))
            slug = re.sub(r"[^A-Za-z0-9가-힣ㄱ-ㅎㅏ-ㅣ._ -]", "", folder)[:80].strip() or ("EP" + m.group(1))
            fn = urllib.parse.quote(slug + ".zip")
            ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "", folder)[:70].strip() or ("EP" + m.group(1))
            zp = os.path.join(DATA_DIR, "zips"); os.makedirs(zp, exist_ok=True)
            zf = os.path.join(zp, fn[:60] + "_" + m.group(1) + ".zip")
            with zipfile.ZipFile(zf, "w") as z:
                for f in pages:
                    z.write(f, arcname=f"{folder}/{os.path.basename(f)}")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{ascii_name}.zip"; filename*=UTF-8\'\'{fn}')
            self.send_header("Content-Length", str(os.path.getsize(zf)))
            self.end_headers(); return
        self.send_response(405); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        p0 = urllib.parse.urlparse(self.path).path
        if p0 == "/healthz":      # container self-run health probe (agent-free ops)
            b = json.dumps({"ok": True,
                            "uptime_s": round(time.time() - _T0_APP),
                            "pool_ready": len(glob.glob(os.path.join(
                                POOL_DIR, "pool_*.json")))}) .encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
            return
        if not check_auth(self):
            return
        p = urllib.parse.urlparse(self.path).path
        mst = re.match(r"^/dl/([0-9a-f]+)/(?P<n>.+\.zip)$", p)
        if mst:
            fp = os.path.join(DATA_DIR, "dl", mst.group(1))
            real_name = urllib.parse.unquote(os.path.basename(mst.group("n")))
            cand = os.path.join(fp, real_name)
            if not os.path.exists(cand):
                return self._json({"error": "expired or unknown token"}, 404)
            fn = urllib.parse.quote(real_name)
            ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "", real_name)[:70].strip() or "export.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{ascii_name}"; '
                             f"filename*=UTF-8''{fn}")
            self.send_header("Content-Length", str(os.path.getsize(cand)))
            self.end_headers()
            try:
                with open(cand, "rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        mz = re.match(r"^/ep/(\d+)\.zip$", p)
        mr = re.match(r"^/ep/(\d+)/?$", p)
        def _html(data):
            b = data.encode("utf-8") if isinstance(data, str) else data
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        if p == "/":
            return _html(_site_page(PAGE if RIDI_ONLY else LZ_PAGE))
        if p in ("/lezhin", "/lz"):
            if RIDI_ONLY:
                return self._redirect(CROSS_URL or "/")
            return _html(_site_page(LZ_PAGE))
        if p == "/ridi":
            if not RIDI_ONLY:
                return self._redirect(CROSS_URL or "/")
            return _html(_site_page(PAGE))
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if p == "/api/lz/search":
            try:
                return self._json(LZ.search((qs.get("q") or [""])[0].strip(),
                                            int((qs.get("limit") or ["30"])[0])))
            except Exception as error:
                return self._json({"error": str(error)}, 500)
        if p == "/api/lz/detail":
            alias = (qs.get("alias") or [""])[0].strip("/")
            try:
                return self._json(LZ.detail(alias))
            except Exception as error:
                return self._json({"error": str(error)}, 500)
        if p == "/api/lz/balance":
            try:
                return self._json(LZ.balance())
            except Exception as error:
                return self._json({"error": str(error)}, 500)
        if p == "/api/lz/status":
            force = (qs.get("force") or ["0"])[0].lower() in {"1", "true", "yes"}
            try:
                return self._json(LZ.auth_status(force=force))
            except Exception as error:
                return self._json({"ok": False, "error": str(error)}, 500)
        if p == "/api/lz/download":
            alias = (qs.get("alias") or [""])[0].strip("/")
            seq = (qs.get("seq") or ["0"])[0]
            try:
                info, episode, filename, data = LZ.download(alias, seq)
                fn = urllib.parse.quote(filename + ".zip")
                ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "", filename)[:70].strip() or (alias + "-" + str(seq))
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{ascii_name}.zip"; filename*=UTF-8\'\'{fn}')
                self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data); return
            except Exception as error:
                return self._json({"error": str(error)}, 500)
        if p == "/api/debug/uri":
            if os.environ.get("DEBUG_URI_ENDPOINT") != "1":
                return self._json({"ok": False, "error": "debug endpoint disabled"}, 403)
            identities = (sorted(glob.glob(os.path.join(POOL_DIR, "active_*.json"))) +
                          sorted(glob.glob(os.path.join(POOL_DIR, "pool_*.json"))))
            if not identities:
                return self._json({"error": "no pooled identity"})
            ident = identities[0]
            env = os.environ.copy()
            env.update({"REUSE_ACCT": ident, "DEBUG_URI": "1"})
            try:
                proc = subprocess.run(
                    [sys.executable, WORKER, os.path.join(DATA_DIR, "debug_uri.json"),
                     DATA_DIR], env=env, timeout=90,
                    capture_output=True, text=True)
            except subprocess.TimeoutExpired:
                return self._json({"error": "timeout"})
            try:
                payload = json.loads(proc.stdout.strip().splitlines()[-1])
            except Exception:
                payload = {"stdout": proc.stdout[-500:], "stderr": proc.stderr[-500:]}
            payload["identity"] = os.path.basename(ident)
            return self._json(payload)
        if p.startswith("/api/job"):
            j = JOB
            if not j:
                return self._json({"active": False})
            # belt & braces: ensure result packaging fires even if supervisor missed it
            if (not j.alive and not getattr(j, "packaged", False)
                    and j.done and not j.pending):
                j.packaged = True
                package_on_finish(j)
            done_list = sorted(j.done, key=int)
            pend = len(j.pending); fail = len(j.failed)
            self._json({"active": j.alive, "finished": not j.alive,
                        "workers_now": len(j.lanes),
                        "workers_target": j.workers_target,
                        "max_workers": j.max_workers,
                        "done_n": len(done_list), "pages": {k: v.get("pages") for k, v in j.done.items()},
                        "paid_list": sorted(j.paid),
                        "bid_status": _bid_stage(j),
                        "paid_reasons": getattr(j, "lock_reason", {}) or {},
                        "pending_n": max(0, pend + fail), "fail_n": fail,
                        "progress": job_progress(j), "cache": j.cache,
                        "stream_url": j.stream_url,
"pool_ready": len(glob.glob(
                             os.path.join(POOL_DIR, "pool_*.json"))),
                          "pool_target": POOL_LIMIT,
                         "pending": j.pending[:20],
                         "supervisor_error": getattr(
                             j, "supervisor_error", "")[-300:],
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
        if p == "/api/lanes":
            def tail_file(path, size):
                try:
                    with open(path, "rb") as fh:
                        fh.seek(max(0, os.path.getsize(path) - size))
                        text = fh.read().decode(errors="ignore")
                    lines = text.splitlines()[-30:]
                    return {"age_s": round(time.time() - os.path.getmtime(path)),
                            "bytes": os.path.getsize(path),
                            "tail": lines}
                except OSError as e:
                    return {"error": str(e)[:160]}
            lanes = []
            if JOB:
                with JOB.lock:
                    live = list(JOB.lanes)
            else:
                live = []
            for l in live:
                out = l.get("outroot") or ""
                lanes.append({"pid": l.get("pid"), "outroot": out,
                              "log": tail_file(os.path.join(out, "lane.log"), 8000)})
            return self._json({"lanes": lanes,
                               "prewarm": tail_file(os.path.join(POOL_DIR, "pre.log"), 12000)})
        if p == "/api/pool":
            return self._json(pool_status())
        if p == "/api/pool/health":
            return self._json(prewarm_health())
        if p == "/api/library":
            idx = lib_scan()
            series = {}
            for b, e in sorted(idx.items(), key=lambda kv: (kv[1].get("no") or 0)):
                g = series.setdefault(e.get("series") or "?", [])
                g.append({"b": b, "no": e.get("no"), "slug": e.get("slug"),
                          "pages": e["pages"], "mb": round(e["bytes"] / 1048576, 1),
                          "created": e.get("created")[:16] if e.get("created") else ""})
            total = sum(x["mb"] for s in series.values() for x in s)
            return self._json({
                "series": [{"name": k, "eps": v} for k, v in series.items()],
                "total_mb": round(total, 1),
                "disk_pct": disk_pct_of(DATA_DIR)})
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
            m3 = re.match(r"^/page/(\d+)/([\w.-]+)$", p)
            if not m3:
                return self._json({}, 404)
            fp = None
            e = lib_index().get(m3.group(1))
            if e:
                c = os.path.join(e["path"], m3.group(2))
                if os.path.exists(c):
                    fp = c
            if not fp:
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
    print(f"{('RIDI' if RIDI_ONLY else 'LEZHIN')} DL listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
