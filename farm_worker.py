# farm_worker.py — single-thread account builder (one process = one isolated identity)
# usage: python3 farm_worker.py <state_file> <out_root> <b_id> [<b_id>...]
import sys, os, json, time
import requests, uuid

# optional egress proxy (Databay etc): set RIDI_PROXY=http://user:pass@host:port
PROXY = os.environ.get("RIDI_PROXY")

if PROXY:
    _orig_request = requests.Session.request

    def _proxied(self, *a, **kw):
        kw.setdefault("proxies", {"http": PROXY, "https": PROXY})
        return _orig_request(self, *a, **kw)
    requests.Session.request = _proxied

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True

import farm_ridi as F

R = "/tmp/opencode/ridi"
STATE_F, OUTROOT = sys.argv[1], sys.argv[2]
TARGETS = [t for t in sys.argv[3:] if len(t) >= 8]

st_path = STATE_F


def load():
    return json.load(open(st_path))


def save(st):
    json.dump(st, open(st_path, "w"))


def build_account(remaining):
    """one throwaway account; returns (claims, session, dev, uid, addr) or None"""
    addr, tok = F.tmail_new()
    F.TMAIL_TOKEN[0] = tok
    uid = pw = None
    for attempt in range(2):
        try:
            uid, pw, ck0 = F.signup(addr)
            if uid:
                break
        except Exception as e:
            print(f"    signup err {str(e)[:80]}")
    if not uid:
        return None
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
        for a in range(2):
            if F.activate(link):
                ok = True
                break
    if not ok:
        print("    activation failed")
        return None
    wck = F.full_login(uid, pw)
    if not wck:
        print("    login/otp failed")
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
    return claimed, w, str(uuid.uuid4()), uid, addr


def main():
    st = {"done": {}, "accounts": []}
    if os.path.exists(st_path):
        try:
            st = json.load(open(st_path))
        except Exception:
            pass
    remaining = [b for b in TARGETS if b not in st["done"]]
    t0 = time.time()
    first_page_at = None
    for i in range(6):  # up to 6 attempts with fresh identities
        if not remaining:
            break
        print(f"[w{os.getpid() % 10000}] account #{i+1} | left={len(remaining)}", flush=True)
        try:
            res = build_account(remaining)
        except Exception as e:
            print("    err:", str(e)[:120], flush=True)
            res = None
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
