"""ToonDay (toonday1.com) free provider client.

ToonDay is an ad-supported re-host of Lezhin/BomToon/Comico titles: every
episode is free after a (free) account login. Images are served as full-size
JPGs from quicksharefiles hosts with data-src lazy loading.

API contract (probed 2026-11-01):
  POST /api/login        form {email,password}  -> {"code":1} + session cookie
  POST /api/reg          form {email,nickname,password,password2}
  GET  /category/{cat}/updatedAt/{page}[?brand=lezhin]  paginated comic cards
  GET  /comic/{mid}      chapter list (/chapter/{cid} + titles, newest first)
  GET  /chapter/{cid}    <img data-src="https://...jpg"> for every page
  POST /api/read         form {mid,cid}         -> view counter
"""

import http.cookiejar
import os
import re
import threading
from pathlib import Path

import requests

BASE = "https://toonday1.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _norm(text):
    text = (text or "").lower().strip()
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", text)
    text = re.sub(r"\d+\s*(화|话|话$|ep|episode|ch)", " ", text)
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


class ToonDay:
    def __init__(self):
        self.email = os.environ.get("TOONDAY_EMAIL") or "tndt778@fldubai.com"
        self.password = os.environ.get("TOONDAY_PASSWORD") or "ToonDay78!a"
        data_dir = Path(os.environ.get("LEZHIN_DATA_DIR") or os.environ.get("DOWNLOAD_DATA_DIR") or "/app/data")
        self.auth_dir = data_dir / ".auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = self.auth_dir / "toonday_cookies.txt"
        self.lock = threading.RLock()

    def _new_session(self):
        jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.is_file():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        session = requests.Session()
        session.cookies = jar
        session.headers.update({"User-Agent": UA, "Referer": BASE + "/"})
        return session

    def _session(self):
        session = getattr(self, "_session_obj", None)
        if session is None:
            session = self._new_session()
            self._session_obj = session
        return session

    def _get(self, path):
        session = self._session()
        response = session.get(BASE + path, timeout=30)
        if response.status_code == 404:
            return response
        if "loginModal" in response.text and "/chapter/" in path and "data-src" not in response.text:
            self.login()
            response = self._session().get(BASE + path, timeout=30)
        return response

    def login(self):
        if not self.email or not self.password:
            raise RuntimeError("TOONDAY_EMAIL/TOONDAY_PASSWORD are not set")
        with self.lock:
            session = self._session()
            response = session.post(
                BASE + "/api/login",
                data={"email": self.email, "password": self.password},
                timeout=30,
            )
            payload = response.json()
            if payload.get("code") != 1:
                raise RuntimeError("toonday login failed: %s" % payload.get("info"))
            if isinstance(session.cookies, http.cookiejar.MozillaCookieJar):
                try:
                    session.cookies.save(ignore_discard=True, ignore_expires=True)
                except OSError:
                    pass
            return True

    def _catalog(self, brand=None, genre="normal", max_pages=200, refresh=False):
        import json as _json
        import time
        key = (brand or "all") + "_" + genre
        cache_f = self.auth_dir / ("catalog_%s.json" % key)
        if not refresh and cache_f.is_file() and time.time() - cache_f.stat().st_mtime < 24 * 3600:
            try:
                return _json.loads(cache_f.read_text())
            except Exception:
                pass
        found = {}
        session = self._session()
        for page in range(1, max_pages + 1):
            query = "?brand=%s" % brand if brand else ""
            response = session.get(BASE + "/category/%s/updatedAt/%d%s" % (genre, page, query), timeout=30)
            if response.status_code != 200:
                break
            html = response.text
            cards = re.findall(r'data-comic-id="(\d+)"', html)
            if not cards:
                break
            for card in re.finditer(r'data-comic-id="(\d+)"(.*?)(?=data-comic-id|\Z)', html, re.S):
                block = card.group(2)
                title = re.search(r'<h6[^>]*title="([^"]+)"', block)
                if not title:
                    continue
                found.setdefault(_norm(title.group(1)), {"id": int(card.group(1)), "title": title.group(1)})
            if not re.search(r'/updatedAt/%d\b' % (page + 1), html):
                break
        cache_f.write_text(_json.dumps(found))
        return found

    BRANDS = ["lezhin", "ridi", "toptoon", "kakao-page", "toomics", "mrblue", "peanutoon", "comico", "naver", "bomtoon"]
    GENRES = ["normal", "adult", "blgl"]

    def find_title(self, title, brand=None, genre=None):
        key = _norm(title)
        genres = [genre] if genre else list(self.GENRES)
        brands = [brand] if brand else list(self.BRANDS) + [None]
        for br in brands:
            for gen in genres:
                try:
                    catalog = self._catalog(brand=br, genre=gen)
                except Exception:
                    continue
                hit = catalog.get(key)
                if hit:
                    return dict(hit, brand=br, genre=gen)
        return None

    def search(self, query, brand=None):
        q = _norm(query)
        hits, seen = [], set()
        for br in ([brand] if brand else list(self.BRANDS)):
            for gen in self.GENRES:
                try:
                    catalog = self._catalog(brand=br, genre=gen)
                except Exception:
                    continue
                for key, item in catalog.items():
                    if q and q in key and item["id"] not in seen:
                        seen.add(item["id"])
                        hits.append(dict(item, brand=br, genre=gen))
        return hits

    def chapters(self, mid):
        response = self._get("/comic/%s" % int(mid))
        items, seen = [], set()
        for match in re.finditer(r'href="/chapter/(\d+)" title="([^"]+)"', response.text):
            cid = int(match.group(1))
            if cid in seen:
                continue
            seen.add(cid)
            items.append({"id": cid, "title": match.group(2).strip()})
        items.reverse()
        return items

    def images(self, cid):
        response = self._get("/chapter/%d" % int(cid))
        urls = re.findall(r'data-src="(https?://[^"]+)"', response.text)
        return urls

    def fetch_image(self, url):
        response = self._session().get(url, timeout=60)
        response.raise_for_status()
        return response.content


TOONDAY = ToonDay()


def find(title, brand=None):
    return TOONDAY.find_title(title, brand=brand)


def chapters(mid):
    return TOONDAY.chapters(mid)


def images(cid):
    return TOONDAY.images(cid)


def fetch(url):
    return TOONDAY.fetch_image(url)


def search(query):
    return TOONDAY.search(query)
