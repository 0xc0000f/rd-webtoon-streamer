# farm_worker.py — single-thread account builder (one process = one isolated identity)
# usage: python3 farm_worker.py <state_file> <out_root> <b_id> [<b_id>...]
import sys, os, json, time
import random
import requests, uuid


def STEP(msg):
    print(f"STEP {msg} (t+{time.time()-_T0:.0f}s)", flush=True)


_T0 = time.time()
_LAST_PW = [""]

# optional egress proxy (Databay etc): set RIDI_PROXY=http://user:pass@host:port
PROXY = None
STAGGER = float(os.environ.get("STAGGER", "0") or 0)


def apply_seed():
    """fresh Databay session suffix per identity attempt → new exit IP"""
    global PROXY
    base = os.environ.get("RIDI_PROXY", "")
    if not base:
        return
    seed = os.urandom(4).hex()
    head, _, tail = base.partition("@")
    user, _, pw = head.rpartition(":")
    PROXY = f"http://{user}-session-{seed}:{pw}@{tail}"


apply_seed()

if STAGGER:          # de-burst identity creation across prewarm lanes
    import time as _t
    _t.sleep(STAGGER)

if PROXY:
    _orig_request = requests.Session.request
    NO_PROXY_HOSTS = ("api.tempmail.lol", "api.mail.tm")   # geo-free hosts go DIRECT

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
               "cookies": w.cookies.get_dict()}, open(tmp, "w"))
    os.rename(tmp, p)
    print(f"[pool] saved {p}", flush=True)


def build_account(remaining):
    """one throwaway account; returns (claims, session, dev, uid, addr) or None"""
    if REUSE_ACCT:
        STEP("reusing pooled account")
        try:
            j = json.load(open(REUSE_ACCT))
        except Exception as e:
            print("    pool read err:", str(e)[:80]); return None
        F.TMAIL_TOKEN[0] = j.get("tmail_tok") or ""
        uid, pw, addr = j["uid"], j["pw"], j["addr"]
        w = requests.Session()
        w.headers["User-Agent"] = F.WEB_UA
        ckdict = j.get("cookies") or {}
        ok = False
        if ckdict:  # cookie-direct reuse — no OTP roundtrip needed
            w.cookies.update(ckdict)
            try:
                me = w.get("https://ridibooks.com/apps/account/accounts/me", timeout=20)
                ok = '"idx"' in me.text
                if ok:
                    print("    pooled cookies valid → skip login")
            except Exception as e:
                print("    cookie check err:", str(e)[:60])
        if not ok:   # fall back to password+OTP via that identity's mailbox
            print("    cookies dead → otp re-login")
            wck = F.full_login(uid, pw)
            if not wck:
                print("    pooled login failed (mailbox likely expired)")
                return None
            w = requests.Session()
            w.cookies.update(wck)
            w.headers["User-Agent"] = F.WEB_UA
        if not F.verify_adult(w):
            print("    adult verify failed (continuing)")
        claimed = []
        for c in dict.fromkeys(F.route_claims(w, remaining)):
            if c not in claimed:
                claimed.append(c)
        print(f"    claims={len(claimed)}/{len(set(remaining))}")
        if not claimed and remaining:
            return None      # nothing claimable with this identity → next one
        return claimed, w, str(uuid.uuid4()), uid, addr
    STEP("making new email")
    addr, tok = F.tmail_new()
    F.TMAIL_TOKEN[0] = tok
    uid = pw = None
    STEP("signup")
    for attempt in range(2):
        try:
            uid, pw, ck0 = F.signup(addr)
            if uid:
                _LAST_PW[0] = pw
                break
        except Exception as e:
            print(f"    signup err {str(e)[:80]}")
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
    wck = F.full_login(uid, pw)
    if not wck:
        print("    login/otp failed")
        return None
    w = requests.Session()
    w.cookies.update(wck)
    w.headers["User-Agent"] = F.WEB_UA
    if not F.verify_adult(w):
        print("    adult verify failed (continuing)")
    STEP("claim free coupons")
    claimed = []
    for c in dict.fromkeys(F.route_claims(w, remaining)):
        if c not in claimed:
            claimed.append(c)
    return claimed, w, str(uuid.uuid4()), uid, addr


def main():
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
        print(f"[w{os.getpid() % 10000}] account #{i+1} | left={len(remaining)}", flush=True)
        signal.alarm(int(os.environ.get("STAGE_TIMEOUT", "90")))  # hard cap/identity
        try:
            res = build_account(remaining)
        except StageTO:
            print("    stage timeout (hard) — recycle identity", flush=True)
            res = None
        except Exception as e:
            print("    err:", str(e)[:120], flush=True)
            res = None
        finally:
            signal.alarm(0)
        if res is None:
            id_fail += 1
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


if __name__ == "__main__":
    main()
