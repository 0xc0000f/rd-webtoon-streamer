# farm_worker.py — single-thread account builder (one process = one isolated identity)
# usage: python3 farm_worker.py <state_file> <out_root> <b_id> [<b_id>...]
import sys, os, json, time, glob, atexit, threading
import random
import requests, uuid


def STEP(msg):
    print(f"STEP {msg} (t+{time.time()-_T0:.0f}s)", flush=True)


class ClaimEmpty(Exception):
    pass


_T0 = time.time()
_LAST_PW = [""]
_PRE_BUSY = os.environ.get("PRE_BUSY", "")


def _touch_pre_busy():
    if _PRE_BUSY:
        try:
            os.utime(_PRE_BUSY, None)
        except OSError:
            pass


def _remove_pre_busy():
    if _PRE_BUSY:
        try:
            os.remove(_PRE_BUSY)
        except OSError:
            pass


def _pre_busy_beat():
    while True:
        _touch_pre_busy()
        time.sleep(15)


if _PRE_BUSY:
    atexit.register(_remove_pre_busy)
    threading.Thread(target=_pre_busy_beat, daemon=True).start()
    _touch_pre_busy()

# optional egress proxy (Databay etc): set RIDI_PROXY=http://user:pass@host:port
PROXY = None
PROXY_SEED = None
STAGGER = float(os.environ.get("STAGGER", "0") or 0)


def _proxy_for_seed(seed):
    base = os.environ.get("RIDI_PROXY", "").strip()
    for pre in ("http://", "https://"):
        if base.startswith(pre):
            base = base[len(pre):]
    if not base or "@" not in base:
        return None
    head, _, tail = base.partition("@")
    user, _, pw = head.rpartition(":")
    return f"http://{user}-session-{seed}:{pw}@{tail}"


def apply_seed(seed=None):
    """use/sticky Databay session suffix; fresh random suffix when seed is absent"""
    global PROXY, PROXY_SEED
    seed = seed or os.urandom(4).hex()
    PROXY_SEED = seed
    PROXY = _proxy_for_seed(seed)


apply_seed()

if STAGGER:          # de-burst identity creation across prewarm lanes
    import time as _t
    _t.sleep(STAGGER)

if PROXY:
    _orig_request = requests.Session.request
    NO_PROXY_HOSTS = ("api.mail.tm",)   # proxy tempmail.lol too: per-residential-IP rate limit

    def _proxied(self, *a, **kw):
        url = a[0] if a else kw.get("url", "")
        if any(h in str(url) for h in NO_PROXY_HOSTS):
            return _orig_request(self, *a, **kw)
        kw.setdefault("proxies", {"http": PROXY, "https": PROXY})
        return _orig_request(self, *a, **kw)
    requests.Session.request = _proxied

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True

import farm_ridi as F

R = "/tmp/opencode/ridi"
STATE_F, OUTROOT = sys.argv[1], sys.argv[2]
TARGETS = [t for t in sys.argv[3:] if len(t) >= 8]
POOL_DIR = os.environ.get("POOL_DIR", "")
REUSE_ACCT = os.environ.get("REUSE_ACCT", "")

st_path = STATE_F


def load():
    return json.load(open(st_path))


def save(st):
    json.dump(st, open(st_path, "w"))


def _persist_pool(w, uid, pw, addr, tok):
    p = os.path.join(POOL_DIR, f"pool_{int(time.time()*1000)}.json")
    tmp = p + ".tmp"
    json.dump({"uid": uid, "pw": pw, "addr": addr, "tmail_tok": tok,
               "proxy_seed": PROXY_SEED,
               "cookies": w.cookies.get_dict()}, open(tmp, "w"))
    os.rename(tmp, p)
    print(f"[pool] saved {p}", flush=True)


def build_account(remaining):
    """one throwaway account; returns (claims, session, dev, uid, addr) or None"""
    if REUSE_ACCT:
        STEP("reusing pooled account")
        try:
            j = json.load(open(REUSE_ACCT))
        except FileNotFoundError:
            print("    [recycle] reason=no-identity-file-left (pool empty)")
            return None
        except Exception as e:
            print("    pool read err:", str(e)[:80]); return None
        F.TMAIL_TOKEN[0] = j.get("tmail_tok") or ""
        uid, pw, addr = j["uid"], j["pw"], j["addr"]
        apply_seed(j.get("proxy_seed"))
        w = requests.Session()
        w.headers["User-Agent"] = F.WEB_UA
        ckdict = j.get("cookies") or {}
        ok = False
        if ckdict:  # cookie-direct reuse — no OTP roundtrip needed
            w.cookies.update(ckdict)
            for attempt in range(2):
                try:
                    me = w.get("https://ridibooks.com/apps/account/accounts/me", timeout=30)
                    ok = '"idx"' in me.text
                    if ok:
                        print("    pooled cookies valid → skip login")
                        break
                except Exception as e:
                    print("    cookie check err:", str(e)[:60])
                if attempt == 0:
                    time.sleep(2)
        if not ok:
            # A pooled identity with dead cookies is a user-facing dead end:
            # re-OTP can waste minutes waiting on an expired disposable inbox.
            # Discard it and let the supervisor select/produce another one.
            print("    cookies dead → recycle pooled identity")
            return None
        # 19+ titles need verified-adult on the account; RIDI gates ad-hoc cert
        # by network — rotate Databay exit until it passes (≤5 tries)
        for att in range(5):
            try:
                if F.verify_adult(w):
                    break
            except Exception as e:
                print("    adult verify err:", str(e)[:60])
            print(f"    adult verify retry {att+1}/5 (rotate exit IP)")
            apply_seed()
            time.sleep(2)
        else:
            print("    [recycle] reason=adult-verify-failed")
        claimed = []
        for c in dict.fromkeys(F.route_claims(w, remaining)):
            if c not in claimed:
                claimed.append(c)
        print(f"    claims={len(claimed)}/{len(set(remaining))}")
        if not claimed and remaining:
            print(f"    CLAIMS_EMPTY {','.join(sorted(set(remaining)))}", flush=True)
            raise ClaimEmpty()
        return claimed, w, str(uuid.uuid4()), uid, addr
    addr = tok = uid = pw = None
    for attempt in range(3):
        STEP(f"making new email ({attempt + 1}/3)")
        addr, tok = F.tmail_new()
        F.TMAIL_TOKEN[0] = tok
        STEP("signup")
        try:
            uid, pw, ck0 = F.signup(addr)
            if uid:
                _LAST_PW[0] = pw
                break
        except Exception as e:
            print(f"    signup err {str(e)[:80]}")
        if attempt < 2:
            time.sleep(3 * (attempt + 1))
    if not uid:
        return None
    STEP("waiting verification mail")
    time.sleep(6)
    link = None
    for _ in range(20):
        time.sleep(5)
        g = F.tmail_poll(tok)
        if g and g[0] == "link":
            link = g[1]
            break
    ok = False
    if link:
        STEP("verify email")
        for a in range(2):
            if F.activate(link):
                ok = True
                break
    if not ok:
        print("    activation failed")
        return None
    STEP("login + otp")
    wck = None
    for attempt in range(2):
        try:
            wck = F.full_login(uid, pw)
            if wck:
                break
        except Exception as e:
            print(f"    login err {str(e)[:80]}")
        if attempt < 1:
            time.sleep(4)
    if not wck:
        print("    login/otp failed")
        return None
    w = requests.Session()
    w.cookies.update(wck)
    w.headers["User-Agent"] = F.WEB_UA
    for att in range(5):   # RIDI gates ad-hoc cert by network → rotate exits
        try:
            if F.verify_adult(w):
                break
        except Exception as e:
            print("    adult verify err:", str(e)[:60])
        print(f"    adult verify retry {att+1}/5 (rotate exit IP)")
        apply_seed()
        time.sleep(2)
    STEP("claim free coupons")
    claimed = []
    for c in dict.fromkeys(F.route_claims(w, remaining)):
        if c not in claimed:
            claimed.append(c)
    return claimed, w, str(uuid.uuid4()), uid, addr


def _pop_identity(prefer="active"):
    """atomically grab next identity file: 'active' tier = recycled cookie-valid
    (good for FREE eps), 'pool' tier = virgin signup w/ untouched coupons (needed
    for PAID claims). Returns (dict, consumed_path) or (None, None)."""
    pats = {"active": ("active_*.json", "pool_*.json"),
            "pool": ("pool_*.json", "active_*.json")}[prefer]
    for pat in pats:
        for f in sorted(glob.glob(os.path.join(POOL_DIR, pat))):
            dst = os.path.join(POOL_DIR,
                               f"inuse_{int(time.time()*1000)}_{os.getpid()%997}.json")
            try:
                os.rename(f, dst)
                with open(dst) as fh:
                    return json.load(fh), dst
            except Exception:
                continue
    return None, None


def main():
    global REUSE_ACCT
    import signal

    class StageTO(Exception):
        pass

    def _alarm(sig, fr):
        raise StageTO()

    signal.signal(signal.SIGALRM, _alarm)

    st = {"done": {}, "accounts": []}
    if os.path.exists(st_path):
        try:
            st = json.load(open(st_path))
        except Exception:
            pass

    if os.environ.get("DEBUG_URI") == "1" and REUSE_ACCT:
        out = {}
        try:
            ident = json.load(open(REUSE_ACCT))
            apply_seed(ident.get("proxy_seed"))
            w = requests.Session()
            w.headers["User-Agent"] = F.WEB_UA
            w.cookies.update(ident.get("cookies") or {})
            me = w.get("https://ridibooks.com/apps/account/accounts/me", timeout=25)
            out["me_status"] = me.status_code
            variants = [
                ("plain", {}),
                ("ajax", {"Accept": "application/json",
                           "X-Requested-With": "XMLHttpRequest",
                           "Referer": "https://ridibooks.com/library"}),
                ("app_ua", {"Accept": "application/json",
                             "X-Requested-With": "XMLHttpRequest",
                             "User-Agent": F.APP_UA}),
            ]
            for name, hdrs in variants:
                try:
                    rr = w.get("https://ridibooks.com/library/get-app-purchased-list-uri-scheme",
                               headers=hdrs, timeout=25)
                    out[name] = {"status": rr.status_code,
                                 "ctype": rr.headers.get("content-type", ""),
                                 "head": (rr.text or "")[:300]}
                except Exception as e:
                    out[name] = {"error": str(e)[:160]}
        except Exception as e:
            out["error"] = str(e)[:200]
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return

    if os.environ.get("PREWARM") and POOL_DIR:  # identity-only run → pool file
        print(f"[w{os.getpid()%10000}] prewarm account", flush=True)
        try:
            res = build_account([])
        except Exception as e:
            print("    err:", str(e)[:120], flush=True); res = None
        if res:
            claimed, w, dev, uid, acc_addr = res
            _persist_pool(w, uid, _LAST_PW[0] or "", acc_addr, F.TMAIL_TOKEN[0])
        return

    remaining = [b for b in TARGETS if b not in st["done"]]
    t0 = time.time()
    first_page_at = None
    id_fail = 0
    for i in range(6):  # up to 6 attempts with fresh identities
        if not remaining:
            break
        apply_seed()   # rotate Databay session per attempt (bad exit IPs happen)
        prefer = "pool" if i >= 1 else "active"   # virgin coupons on retry — PAID needs them
        if (not REUSE_ACCT or not os.path.exists(REUSE_ACCT)) and POOL_DIR:
            acct_j, newpth = _pop_identity(prefer)
            if acct_j:
                os.environ["REUSE_ACCT"] = REUSE_ACCT = newpth
                F.TMAIL_TOKEN[0] = acct_j.get("tmail_tok") or ""
                print(f"    [identity] popped {os.path.basename(newpth)} ({prefer}-tier)",
                      flush=True)
            elif i == 0:
                pass   # no pool at all → fresh-signup path below handles it
        print(f"[w{os.getpid() % 10000}] account #{i+1} | left={len(remaining)}", flush=True)
        signal.alarm(int(os.environ.get("STAGE_TIMEOUT", "90")))  # hard cap/identity
        ce_hits = 0
        def retire_reused_identity():
            global REUSE_ACCT
            if REUSE_ACCT:
                try:
                    os.remove(REUSE_ACCT)
                except OSError:
                    pass
                print("    [identity] retired unusable pooled identity", flush=True)
                REUSE_ACCT = ""

        try:
            res = build_account(remaining)
        except ClaimEmpty as _ce:
            print("    [recycle] reason=claims-empty (no coupon/free path on this identity)")
            retire_reused_identity()
            ce_hits += 1
            if ce_hits >= 2 or os.environ.get("FAST_NOCLAIM") == "1":
                break
            continue
        except StageTO:
            print("    [recycle] reason=stage-timeout (hard)")
            retire_reused_identity()
            res = None
        except Exception as e:
            print("    err:", str(e)[:120], flush=True)
            retire_reused_identity()
            res = None
        finally:
            signal.alarm(0)
        if res is None:
            id_fail += 1
            retire_reused_identity()
            if id_fail >= 3:      # provider/proxy likely down → cool off
                print("    cooling 45s (mail/prox circuit)", flush=True)
                time.sleep(45)
        else:
            id_fail = 0
        if not res:
            continue
        claimed, w, dev, uid, acc_addr = res
        if claimed:
            got = F.app_download(w, claimed, outroot=OUTROOT, dev=dev)
            if got and first_page_at is None:
                first_page_at = time.time() - t0
            for g in got:
                st["done"][str(g["b_id"])] = {"pages": g["pages"], "acct": uid}
            st["accounts"].append({"uid": uid, "email": acc_addr,
                                   "claims": list(claimed)})
        save(st)
        remaining = [b for b in TARGETS if b not in st["done"]]
        print(f"    done={len(st['done'])} left={len(remaining)} "
              f"(t+{time.time()-t0:.0f}s)", flush=True)
    dt = time.time() - t0
    print(f"[w] FINISHED {TARGETS} in {dt:.1f}s (first page at +{first_page_at})", flush=True)
    try:   # recycle the still-valid pooled identity so free eps don't burn new ones
        if REUSE_ACCT and not remaining and TARGETS \
                and all(str(t) in st["done"] for t in TARGETS):
            nsrc = os.path.join(POOL_DIR,
                                f"active_{int(time.time()*1000)}.json")
            os.rename(REUSE_ACCT, nsrc)
            print(f"[pool] recycled identity", flush=True)
    except OSError:
        pass


if __name__ == "__main__":
    main()
