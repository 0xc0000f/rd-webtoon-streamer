import base64
import http.cookiejar
import io
import json
from io import StringIO
import math
import os
import re
import secrets
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

API_ROOT = "https://www.lezhin.com"
CDN_IMAGE_ROOT = "https://rcdn.lezhin.com"
CDN_PUBLIC_ROOT = "https://ccdn.lezhin.com"
PAYVERSE_ROOT = "https://lezhin-web.lezhin.com"
MASK64 = (1 << 64) - 1
CONTENT_RE = re.compile(r'"content":\{')
EPISODES_RE = re.compile(r'"episodes":\[')


class LezhinError(RuntimeError):
    pass


def _decode_jwt_payload(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _proxy_with_session(proxy_url, session_id):
    if not proxy_url or "@" not in proxy_url:
        return proxy_url or None
    scheme, _, body = proxy_url.partition("://")
    scheme = scheme or "http"
    credentials, _, host = body.rpartition("@")
    if not host:
        return proxy_url
    user, _, password = credentials.rpartition(":")
    if not user or "-session-" in user:
        return proxy_url
    return f"{scheme}://{user}-session-{session_id}:{password}@{host}"


class Randomizer:
    def __init__(self, seed, grid):
        self.state = int(seed) & MASK64
        self.order = list(range(grid * grid))
        for index in range(len(self.order)):
            selected = self._random(grid * grid)
            self.order[index], self.order[selected] = self.order[selected], self.order[index]

    def _random(self, modulus):
        value = self.state
        value ^= value >> 12
        value ^= (value << 25) & MASK64
        value ^= value >> 27
        self.state = value & MASK64
        return (value >> 32) % modulus


def _tile_rect(index, grid, width, height):
    cells = grid * grid
    if index < cells:
        tile_width = width // grid
        tile_height = height // grid
        return (
            (index % grid) * tile_width,
            (index // grid) * tile_height,
            tile_width,
            tile_height,
        )
    if index == cells:
        remainder = width % grid
        return (width - remainder, 0, remainder, height) if remainder else None
    remainder = height % grid
    return (0, height - remainder, width - width % grid, remainder) if remainder else None


def _crop_box(rect):
    left, top, width, height = rect
    return (left, top, left + width, top + height)


def descramble_image(data, episode_id):
    source = Image.open(io.BytesIO(data))
    source.load()
    output = source.copy()
    order = Randomizer(episode_id, 5).order
    order.extend([len(order), len(order) + 1])
    for destination_index, source_index in enumerate(order):
        destination = _tile_rect(destination_index, 5, source.width, source.height)
        origin = _tile_rect(int(source_index), 5, source.width, source.height)
        if not destination or not origin:
            continue
        output.paste(source.crop(_crop_box(origin)), (destination[0], destination[1]))
    buffer = io.BytesIO()
    output.save(buffer, format="WEBP", quality=92)
    return buffer.getvalue()


class _ZipStream:
    def __init__(self):
        self._buffer = bytearray()
        self._position = 0

    def write(self, data):
        if isinstance(data, memoryview):
            data = bytes(data)
        self._buffer.extend(data)
        self._position += len(data)

    def tell(self):
        return self._position

    def flush(self):
        pass

    def seekable(self):
        return False

    def take(self):
        if not self._buffer:
            return b""
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


class LezhinWeb:
    def __init__(self):
        self.data_dir = Path(os.environ.get("LEZHIN_DATA_DIR") or os.environ.get("DOWNLOAD_DATA_DIR") or "/app/data")
        self.auth_dir = self.data_dir / ".auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.token_file = self.auth_dir / "lezhin_token"
        self.cookie_file = self.auth_dir / "lezhin_cookies.txt"
        self.email = os.environ.get("LEZHIN_EMAIL", "")
        self.password = os.environ.get("LEZHIN_PASSWORD", "")
        self.user_id = int(os.environ.get("LEZHIN_USER_ID") or 0)
        self.adult = int(os.environ.get("LEZHIN_ADULT") or 2)
        self.country = os.environ.get("LEZHIN_COUNTRY") or "th"
        self.coin_product_id = int(os.environ.get("LEZHIN_COIN_PRODUCT_ID") or 5163012002480128)
        self.coin_price = int(os.environ.get("LEZHIN_COIN_PRICE") or 5000)
        self.coin_size = int(os.environ.get("LEZHIN_COIN_SIZE") or 25)
        self.lock = threading.RLock()
        self.token = None
        self._load_token()
        self.session = requests.Session()
        self._load_cookies()
        proxy_url = os.environ.get("LEZHIN_PROXY") or os.environ.get("RIDI_PROXY")
        session_seed = os.environ.get("LEZHIN_PROXY_SESSION")
        if not session_seed and self.email:
            import hashlib

            session_seed = hashlib.sha256(self.email.encode("utf-8")).hexdigest()[:16]
        proxy_url = _proxy_with_session(proxy_url, session_seed or "default")
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}
        self._genres = {}

    def _load_token(self):
        if os.environ.get("LEZHIN_TOKEN"):
            self.token = os.environ["LEZHIN_TOKEN"].strip()
        if not self.token and self.token_file.is_file():
            self.token = self.token_file.read_text(encoding="utf-8").strip() or None

    def _save_token(self):
        self.token_file.write_text(self.token or "", encoding="utf-8")

    def _load_cookies(self):
        jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        encoded = os.environ.get("LEZHIN_COOKIES_B64")
        if encoded:
            try:
                self.cookie_file.write_bytes(base64.b64decode(encoded))
            except Exception:
                pass
        if self.cookie_file.is_file():
            jar.load(ignore_discard=True, ignore_expires=True)
        self.session.cookies = requests.cookies.RequestsCookieJar()
        for cookie in jar:
            self.session.cookies.set_cookie(cookie)

    def _save_cookies(self):
        jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        for cookie in self.session.cookies:
            jar.set_cookie(cookie)
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        jar.save(ignore_discard=True, ignore_expires=True)

    def _token_expiry(self):
        return _decode_jwt_payload(self.token or "").get("exp")

    def _token_valid(self, margin=120):
        expiry = self._token_expiry()
        return isinstance(expiry, int) and time.time() < expiry - margin

    def _headers(self, json_body=False):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "x-lz-adult": str(self.adult),
            "x-lz-allowadult": "true",
            "x-lz-country": self.country,
            "x-lz-locale": "ko-KR",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if json_body:
            headers["content-type"] = "application/json"
        return headers

    def _snapshot_cookie(self, name):
        return next((cookie for cookie in self.session.cookies if cookie.name == name), None)

    def _restore_cookie(self, cookie):
        if cookie is None:
            return
        self.session.cookies.set_cookie(cookie)

    def login(self, force=False):
        with self.lock:
            if not force and self._token_valid():
                return self.token or ""
            adult_cookie = self._snapshot_cookie("_LZ_FS")
            response = self.session.post(
                f"{API_ROOT}/api/authentication/login",
                headers=self._headers(json_body=True),
                json={
                    "email": self.email,
                    "password": self.password,
                    "remember": True,
                    "language": "ko",
                    "provider": "email",
                },
                timeout=30,
            )
            response.raise_for_status()
            token = response.json().get("appConfig", {}).get("accessToken")
            if not token:
                raise LezhinError("login response missing accessToken")
            self.token = token
            payload = _decode_jwt_payload(token)
            if not self.user_id:
                self.user_id = int(payload.get("sub") or 0)
            self._restore_cookie(adult_cookie)
            self._save_token()
            self._save_cookies()
            return token

    def _request(self, method, url, *, auth=True, **kwargs):
        self.login()
        for attempt in range(5):
            try:
                headers = self._headers(json_body="json" in kwargs)
                headers.update(kwargs.pop("headers", {}))
                response = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
                if auth and response.status_code == 401:
                    self.login(force=True)
                    headers["Authorization"] = f"Bearer {self.token}"
                    response = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
                return response
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 4:
                    raise
                time.sleep(0.4 * (attempt + 1))
        raise LezhinError("unreachable")

    def get_balance(self):
        response = self._request("GET", f"{API_ROOT}/lz-api/voucher/v3/balance")
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise LezhinError(f"balance failed: {payload}")
        return payload["data"]

    def auth_status(self, force=False):
        self.login(force=force)
        alias = (os.environ.get("LEZHIN_STATUS_ALIAS") or "favoriteboy").strip("/")
        web_error = ""
        try:
            body = self._rsc_body(alias)
            web_ok = bool(CONTENT_RE.search(body) and EPISODES_RE.search(body))
            if not web_ok:
                web_error = "comic page did not contain content or episodes"
        except Exception as error:
            web_ok = False
            web_error = str(error)
        token_payload = _decode_jwt_payload(self.token or "")
        data = self.get_balance()
        return {
            "ok": web_ok,
            "token_valid": self._token_valid(),
            "token_exp": token_payload.get("exp"),
            "user_id": token_payload.get("sub"),
            "cookies": len(self.session.cookies),
            "has_jsessionid": any(cookie.name == "JSESSIONID" for cookie in self.session.cookies),
            "web_ok": web_ok,
            "web_error": web_error,
            "status_alias": alias,
            "balance": data,
            "usable": self.usable(data),
            "auth_dir": str(self.auth_dir),
        }

    @staticmethod
    def usable(balance):
        return sum(int(balance.get(key) or 0) for key in ("coin", "bonusCoin", "paidBonusCoin"))

    def _load_genres(self):
        if self._genres:
            return self._genres
        response = self._request("GET", f"{API_ROOT}/lz-api/v2/genres")
        response.raise_for_status()
        for item in response.json().get("data", []):
            names = item.get("genre") or {}
            self._genres[item["id"]] = names.get("ko-KR") or names.get("en-US") or item["id"]
        return self._genres

    def _advanced_search_items(self, query, limit):
        response = self._request(
            "GET",
            f"{API_ROOT}/lz-api/v2/advanced-search",
            params={
                "q": query,
                "t": "all",
                "order": "popular",
                "offset": 0,
                "limit": max(1, min(int(limit), 60)),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            return 0, []
        return int(payload.get("count") or 0), [
            self._search_item(item)
            for item in payload.get("data", [])
            if item.get("id") and item.get("alias")
        ]

    @staticmethod
    def _search_item(item):
        artists = ", ".join(x.get("name", "") for x in item.get("artists", []) if x.get("name"))
        cover = f"{CDN_PUBLIC_ROOT}/v2/comics/{item['id']}/images/tall.jpg?updated={item.get('updatedAt', 0)}&width=420"
        return {
            "id": int(item["id"]),
            "alias": item.get("alias"),
            "title": item.get("title", "").replace(" [연재]", ""),
            "artists": artists,
            "genres": item.get("genres") or [],
            "cover": cover,
            "rating": "19+" if item.get("adult") else "ALL",
            "updated": int(item.get("updatedAt") or 0),
        }

    def search(self, query, limit=30):
        self.login()
        query = (query or "").strip()
        if not query:
            return {"q": query, "count": 0, "items": []}
        try:
            total, items = self._advanced_search_items(query, limit)
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code < 400 or error.response.status_code >= 500:
                raise
            total, items = 0, []
        except (requests.ConnectionError, requests.Timeout):
            raise
        if total == 0 and len(query.split()) > 1:
            merged = {}
            for token in query.split():
                token = token.strip()
                if len(token) < 2:
                    continue
                try:
                    _, found = self._advanced_search_items(token, limit)
                except (requests.HTTPError, requests.ConnectionError, requests.Timeout):
                    continue
                for item in found:
                    key = item["alias"]
                    if key not in merged:
                        merged[key] = item
            fallback = sorted(merged.values(), key=lambda item: item["updated"], reverse=True)[:limit]
            return {"q": query, "count": len(fallback), "items": fallback}
        return {"q": query, "count": total, "items": items}

    def _rsc_body(self, alias):
        response = self._request(
            "GET",
            f"{API_ROOT}/ko/comic/{alias}",
            params={"_rsc": secrets.token_hex(3)},
            headers={"RSC": "1"},
        )
        if response.status_code == 500:
            self.login(force=True)
            response = self._request(
                "GET",
                f"{API_ROOT}/ko/comic/{alias}",
                params={"_rsc": secrets.token_hex(3)},
                headers={"RSC": "1"},
            )
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text

    def detail(self, alias):
        self.login()
        body = self._rsc_body(alias)
        content_match = CONTENT_RE.search(body)
        episodes_match = EPISODES_RE.search(body)
        if not content_match or not episodes_match:
            raise LezhinError("comic page is missing content or episodes")
        decoder = json.JSONDecoder()
        content = decoder.raw_decode(body[content_match.end() - 1 :])[0]
        episodes = decoder.raw_decode(body[episodes_match.end() - 1 :])[0]
        genre_names = self._load_genres()
        metadata = content.get("metadata") or {}
        comic_id = int(content["id"])
        sorted_episodes = sorted(episodes, key=lambda item: int(item.get("seq") or 0))
        with ThreadPoolExecutor(max_workers=8) as pool:
            owned = list(pool.map(lambda episode: self.is_purchased(alias, episode["name"]), sorted_episodes))
        result_episodes = []
        for episode, purchased in zip(sorted_episodes, owned):
            result_episodes.append(
                {
                    "id": int(episode["id"]),
                    "name": episode["name"],
                    "seq": int(episode["seq"]),
                    "title": (episode.get("display") or {}).get("title") or episode["name"],
                    "coin": int(episode.get("coin") or 0),
                    "not_for_sale": bool((episode.get("properties") or {}).get("notForSale")),
                    "purchased": purchased,
                    "cover": f"{CDN_PUBLIC_ROOT}/v2/comics/{comic_id}/episodes/{episode['id']}/images/cover.jpg?updated={episode.get('updatedAt', 0)}&width=280",
                }
            )
        return {
            "alias": alias,
            "comic_id": comic_id,
            "title": (content.get("display") or {}).get("title") or alias,
            "synopsis": (content.get("display") or {}).get("synopsis") or "",
            "artists": ", ".join(x.get("name", "") for x in content.get("artists") or [] if x.get("name")),
            "genres": [genre_names.get(x, x) for x in content.get("genres") or []],
            "rating": "19+" if content.get("rating") == 19 or content.get("isAdult") else "ALL",
            "image_shuffle": bool(metadata.get("imageShuffle")),
            "cover": f"{CDN_PUBLIC_ROOT}/v2/comics/{comic_id}/images/tall.jpg?updated={content.get('updatedAt', 0)}&width=420",
            "episodes": result_episodes,
        }

    def is_purchased(self, alias, episode_name):
        self.login()
        paths = [f"{API_ROOT}/lz-api/v2/viewer/comics/{alias}/episodes/{episode_name}/me"]
        digits = re.sub(r"\D", "", str(episode_name))
        if digits and digits.lstrip("0") != digits:
            paths.append(f"{API_ROOT}/lz-api/v2/viewer/comics/{alias}/episodes/{digits.lstrip('0')}/me")
        for path in paths:
            payload = self._request("GET", path).json()
            if payload.get("code") == 0:
                return bool((payload.get("data") or {}).get("isPurchased"))
        return False

    def _payverse_reserve(self):
        response = self._request(
            "POST",
            f"{PAYVERSE_ROOT}/payment/payverse/reserve",
            json={
                "store": "web",
                "idCoinProduct": self.coin_product_id,
                "currency": "USD",
                "amount": self.coin_price,
                "pointAmount": 0,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise LezhinError(f"coin reserve failed: {payload}")
        return payload["data"]

    def _payverse_webhook(self, reserved):
        amount = float(reserved["requestAmount"])
        now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        body = {
            "type": "PAYMENT",
            "mid": reserved["mid"],
            "tid": f"t_{reserved['orderId']}",
            "schemeGroup": "card",
            "schemeCode": "0000",
            "orderId": reserved["orderId"],
            "productName": reserved["productName"],
            "requestCurrency": reserved["requestCurrency"],
            "requestAmount": amount,
            "installmentMonths": "0",
            "resultStatus": "SUCCESS",
            "resultCode": "0000",
            "resultMessage": "success",
            "approvalNo": f"AP{reserved['orderId']}",
            "approvalCurrency": reserved["requestCurrency"],
            "approvalAmount": amount,
            "approvalDay": now[:8],
            "approvalTime": now[8:],
            "mallReserved": "",
            "billKey": "",
            "sign": reserved["sign"],
        }
        response = self._request(
            "POST",
            f"{PAYVERSE_ROOT}/payment/payverse/webhook/{self.user_id}",
            json=body,
            auth=False,
        )
        response.raise_for_status()
        return response.json()

    def add_coins(self, packages=1):
        with self.lock:
            balance = self.get_balance()
            expected = self.usable(balance) + self.coin_size
            for _ in range(max(1, int(packages))):
                completed = False
                for _ in range(4):
                    reserved = self._payverse_reserve()
                    webhook = self._payverse_webhook(reserved)
                    if webhook.get("receiveResult") == "SUCCESS":
                        for _ in range(8):
                            time.sleep(0.25)
                            balance = self.get_balance()
                            if self.usable(balance) >= expected:
                                expected += self.coin_size
                                completed = True
                                break
                    if completed:
                        break
                    time.sleep(0.35)
                if not completed:
                    raise LezhinError("coin webhook did not credit coins")
            return balance

    def ensure_coins(self, required):
        balance = self.get_balance()
        deficit = int(required) - self.usable(balance)
        if deficit <= 0:
            return balance
        return self.add_coins(math.ceil(deficit / self.coin_size))

    def purchase_episode(self, episode_id, coin):
        response = self._request(
            "POST",
            f"{API_ROOT}/lz-api/v2/users/{self.user_id}/purchases",
            json={
                "coin": int(coin),
                "items": [int(episode_id)],
                "preorderItems": [],
                "type": "P",
                "platform": "web",
            },
        )
        payload = response.json()
        if payload.get("code") != 0:
            raise LezhinError(f"episode purchase failed: {payload}")
        self._save_cookies()
        return payload

    def _signed_episode(self, comic_id, episode, purchased):
        response = self._request(
            "GET",
            f"{API_ROOT}/lz-api/v2/cloudfront/signed-url/generate",
            params={
                "contentId": comic_id,
                "episodeId": episode["id"],
                "purchased": "true" if purchased else "false",
                "q": 40,
                "firstCheckType": "P",
            },
            headers={"x-referer": API_ROOT},
        )
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("data"):
            raise LezhinError(f"image sign failed: {payload}")
        signed = payload["data"]
        query = {
            "purchased": "true" if purchased else "false",
            "q": 40,
            "updated": episode.get("updatedAt") or 0,
            "Policy": signed["Policy"],
            "Signature": signed["Signature"],
            "Key-Pair-Id": signed["Key-Pair-Id"],
        }
        base = f"{CDN_IMAGE_ROOT}/v2/comics/{comic_id}/episodes/{episode['id']}/contents/scrolls"
        return base, query

    def _prepared_episode(self, info, episode, buy=True):
        if episode.get("not_for_sale"):
            raise LezhinError("episode is not sold separately")
        purchased = bool(episode.get("purchased"))
        if not purchased and int(episode.get("coin") or 0) > 0:
            if not buy:
                raise LezhinError("episode is not purchased")
            self.ensure_coins(episode["coin"])
            self.purchase_episode(episode["id"], episode["coin"])
            purchased = self.is_purchased(info["alias"], episode["name"])
        if not purchased and int(episode.get("coin") or 0) > 0:
            raise LezhinError("episode is not purchased")
        base, query = self._signed_episode(info["comic_id"], episode, purchased)
        filename = re.sub(r'[\\/:*?"<>|]+', " ", f"{info['title']} {episode['title']}").strip()
        seq = int(episode.get("seq") or 0)
        if not filename:
            filename = f"{info['alias']} {seq}"

        def fetch_page(number):
            headers = self._headers()
            headers["x-referer"] = API_ROOT
            for attempt in range(5):
                try:
                    response = self.session.get(f"{base}/{number}.webp", params=query, headers=headers, timeout=60)
                    if response.status_code == 404:
                        return None
                    response.raise_for_status()
                    data = response.content
                    if info["image_shuffle"]:
                        data = descramble_image(data, episode["id"])
                    return data
                except requests.RequestException as error:
                    status = getattr(error.response, "status_code", 0)
                    transient = isinstance(
                        error,
                        (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError),
                    ) or status >= 500
                    if not transient or attempt == 4:
                        raise
                    time.sleep(0.4 * (attempt + 1))

        def pages():
            number = 1
            workers = max(1, min(32, int(os.environ.get("LEZHIN_PAGE_WORKERS") or 16)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                while True:
                    batch = list(range(number, number + workers * 2))
                    for page, data in zip(batch, pool.map(fetch_page, batch)):
                        if data is None:
                            return
                        yield data
                    number = batch[-1] + 1

        return filename, pages()

    def download_episode(self, alias, seq, buy=True):
        info = self.detail(alias)
        episode = next((x for x in info["episodes"] if x["seq"] == int(seq)), None)
        if not episode:
            raise LezhinError("episode not found")
        filename, pages = self._prepared_episode(info, episode, buy=buy)
        return info, episode, filename, pages

    @staticmethod
    def _ordered_seq_selection(info, seqs):
        by_seq = {int(x.get("seq") or 0): x for x in info.get("episodes", [])}
        ordered = []
        missing = []
        seen = set()
        for seq in seqs:
            try:
                value = int(seq)
            except (TypeError, ValueError):
                missing.append(str(seq))
                continue
            if value in seen:
                continue
            seen.add(value)
            if value not in by_seq:
                missing.append(value)
                continue
            ordered.append(value)
        return by_seq, ordered, missing

    def download_episodes_stream(self, alias, seqs, buy=False):
        info = self.detail(alias)
        by_seq, ordered, missing = self._ordered_seq_selection(info, seqs)
        meta = {
            "done": [],
            "errors": {},
            "missing": missing,
            "partial": {},
            "pages_downloaded": 0,
        }
        if not ordered:
            return info, meta, None

        def chunks():
            stream = _ZipStream()
            archive = zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
            try:
                for seq in ordered:
                    written = 0
                    try:
                        filename, pages = self._prepared_episode(info, by_seq[seq], buy=buy)
                        prefix = f"{seq:03d} {filename}"[:120]
                        for index, data in enumerate(pages, start=1):
                            archive.writestr(f"{prefix}/page_{index:03d}.webp", data)
                            written += 1
                            meta["pages_downloaded"] += 1
                            part = stream.take()
                            if part:
                                yield part
                        if not written:
                            raise ValueError("no pages")
                        meta["done"].append(seq)
                    except Exception as error:
                        meta["errors"][seq] = str(error)
                        if written:
                            meta["partial"][seq] = written
                        part = stream.take()
                        if part:
                            yield part
            finally:
                archive.writestr(
                    "_download_report.json",
                    json.dumps(meta, ensure_ascii=False, indent=2),
                )
                archive.close()
                part = stream.take()
                if part:
                    yield part

        return info, meta, chunks()

    def download_episodes(self, alias, seqs, buy=False):
        info, meta, chunks = self.download_episodes_stream(alias, seqs, buy=buy)
        if chunks is None:
            raise LezhinError("no valid episodes selected")
        blob = b"".join(chunks)
        if not meta["done"]:
            raise LezhinError(next(iter(meta["errors"].values()), "no episodes downloaded"))
        return info, blob, meta["done"], meta["errors"], meta["missing"]

    @staticmethod
    def stream_zip(info, episode, filename, pages):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for index, data in enumerate(pages, start=1):
                archive.writestr(f"{filename}/page_{index:03d}.webp", data)
        stream.seek(0)
        return stream.read()


APP = LezhinWeb()


def search(query, limit=30):
    return APP.search(query, limit)


def detail(alias):
    return APP.detail(alias)


def balance():
    data = APP.get_balance()
    return {"balance": data, "usable": APP.usable(data)}


def auth_status(force=False):
    return APP.auth_status(force=force)


def download(alias, seq):
    info, episode, filename, pages = APP.download_episode(alias, seq)
    return info, episode, filename, APP.stream_zip(info, episode, filename, pages)


def download_many(alias, seqs, buy=False):
    return APP.download_episodes(alias, seqs, buy=buy)


def stream_many(alias, seqs, buy=False):
    return APP.download_episodes_stream(alias, seqs, buy=buy)
