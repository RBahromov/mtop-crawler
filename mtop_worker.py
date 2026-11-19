#!/usr/bin/env python3
"""
mtop.uz — masofaviy yig'uvchi (bepul GitHub Actions / VPS / o'z kompyuteringizda ishlaydi).

Sahifalarni internetdan YUKLAYDI va tayyor HTML/PDF ni mtop hostingidagi ingest.php ga yuboradi.
Tahlil va indekslash hostingda mavjud konveyer bilan bajariladi (hammasi bir xil ishlaydi).

Faqat standart kutubxona (pip install shart emas). Python 3.9+.

Muhit o'zgaruvchilari:
  MTOP_URL  — sayt manzili, masalan https://marsfm.uz/mtop   (oxirida / bo'lmasin)
  MTOP_KEY  — CRAWL_KEY (config.php dagi kalit)

Buyruqlar:
  crawl     navbatdan olib yuklaydi:   python mtop_worker.py crawl --minutes 330
  seed-cc   Common Crawl indeksidan .uz URL ro'yxatini navbatga qo'shadi:  python mtop_worker.py seed-cc --limit 20000
  seed-file fayldagi URL larni navbatga qo'shadi: python mtop_worker.py seed-file urls.txt
  stats     navbat holati
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "MtopBot/1.0 (+https://mtop.uz/bot)"
MAX_HTML = 2_000_000
MAX_PDF = 4_000_000          # hosting post_max_size ga sig'ishi uchun (PDF gzip qilinib yuboriladi)
SUBMIT_CHUNK = 2_500_000     # bitta so'rovdagi (base64) hajm chegarasi
ALLOW_PRIVATE = os.environ.get("MTOP_ALLOW_PRIVATE") == "1"   # faqat sinov uchun


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------- hosting API
class Api:
    def __init__(self, base: str, key: str):
        self.url = base.rstrip("/") + "/ingest.php"
        self.key = key.encode()

    def call(self, op: str, **payload) -> dict:
        body = gzip.compress(json.dumps({"op": op, **payload}, ensure_ascii=False).encode())
        last = ""
        for attempt in range(4):
            ts = str(int(time.time()))
            sig = hmac.new(self.key, (ts + "\n" + hashlib.sha256(body).hexdigest()).encode(), hashlib.sha256).hexdigest()
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Content-Type": "application/json", "Content-Encoding": "gzip",
                "X-Mtop-Ts": ts, "X-Mtop-Sig": sig, "User-Agent": UA,
            })
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")[:300]
                if e.code in (401, 403, 405, 503):
                    raise SystemExit(f"Hosting rad etdi (HTTP {e.code}): {txt}")
                last = f"HTTP {e.code} {txt}"
            except Exception as e:  # tarmoq xatosi
                last = str(e)
            time.sleep(2 ** attempt * 2)
        raise RuntimeError("hosting javob bermadi: " + last)


# ----------------------------------------------------------------------------- xavfsiz yuklash
def _public_ip(host: str) -> bool:
    if ALLOW_PRIVATE:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return bool(infos)


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urllib.parse.urlparse(newurl)
        if p.scheme not in ("http", "https") or not _public_ip(p.hostname or ""):
            raise urllib.error.URLError("yo'naltirish ichki/noto'g'ri manzilga")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Redirect())


class HostGate:
    """Bir saytga ketma-ket so'rovlar orasida pauza (xushmuomala yig'ish) + robots.txt keshi."""

    def __init__(self, delay: float):
        self.delay = delay
        self.lock = threading.Lock()
        self.next_at: dict[str, float] = {}
        self.robots: dict[str, tuple[list[tuple[bool, str]], float]] = {}

    def wait(self, host: str, extra: float = 0.0):
        with self.lock:
            now = time.time()
            at = max(now, self.next_at.get(host, 0.0))
            self.next_at[host] = at + max(self.delay, extra)
        if at > now:
            time.sleep(at - now)

    def _rules(self, scheme: str, host: str) -> tuple[list[tuple[bool, str]], float]:
        key = f"{scheme}://{host}"
        with self.lock:
            if key in self.robots:
                return self.robots[key]
        rules: list[tuple[bool, str]] = []
        crawl_delay = 0.0
        try:
            req = urllib.request.Request(key + "/robots.txt", headers={"User-Agent": UA})
            with _opener.open(req, timeout=8) as r:
                txt = r.read(300_000).decode("utf-8", "replace")
            applies = False
            seen_specific = False
            specific: list[tuple[bool, str]] = []
            generic: list[tuple[bool, str]] = []
            cd_spec = cd_gen = 0.0
            group_agents: list[str] = []
            last_was_agent = False
            for line in txt.splitlines():
                line = line.split("#", 1)[0].strip()
                if ":" not in line:
                    continue
                k, v = [x.strip() for x in line.split(":", 1)]
                k = k.lower()
                if k == "user-agent":
                    if not last_was_agent:
                        group_agents = []
                    group_agents.append(v.lower())
                    last_was_agent = True
                    continue
                last_was_agent = False
                mine = any("mtopbot" in a for a in group_agents)
                star = "*" in group_agents
                if k in ("allow", "disallow") and (mine or star):
                    (specific if mine else generic).append((k == "allow", v))
                elif k == "crawl-delay" and (mine or star):
                    try:
                        d = float(v)
                    except ValueError:
                        d = 0.0
                    if mine:
                        cd_spec = d
                    else:
                        cd_gen = d
            rules = specific if specific else generic
            crawl_delay = cd_spec if specific else cd_gen
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                rules = [(False, "/")]            # sayt vaqtincha xato — shu safar o'tkazib yuboramiz
        except Exception:
            pass                                   # robots.txt yo'q/ochilmadi — taqiq yo'q deb hisoblanadi
        out = (rules, min(crawl_delay, 10.0))
        with self.lock:
            self.robots[key] = out
        return out

    def allowed(self, url: str) -> tuple[bool, float]:
        p = urllib.parse.urlparse(url)
        rules, cd = self._rules(p.scheme, p.netloc)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        best_len, verdict = -1, True
        for allow, pat in rules:
            if pat == "":
                continue
            rx = re.escape(pat).replace(r"\*", ".*")
            if rx.endswith(r"\$"):
                rx = rx[:-2] + "$"
            if re.match(rx, path) and len(pat) > best_len:
                best_len, verdict = len(pat), allow
        return verdict, cd


def fetch(job: dict, gate: HostGate) -> dict:
    url = job["url"]
    base = {"id": job["id"]}
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return base | {"status": "skipped", "error": "noto'g'ri URL"}
    if not _public_ip(p.hostname):
        return base | {"status": "skipped", "error": "ichki/ochiq bo'lmagan manzil"}
    ok, cd = gate.allowed(url)
    if not ok:
        return base | {"status": "skipped", "error": "robots.txt taqiqlagan"}
    gate.wait(p.netloc.lower(), cd)
    is_pdf_url = bool(re.search(r"\.pdf(\?|#|$)", url, re.I))
    limit = MAX_PDF if is_pdf_url else MAX_HTML
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept-Encoding": "gzip",
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            "Accept-Language": "uz,ru;q=0.8,en;q=0.5",
        })
        with _opener.open(req, timeout=20) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            final = r.geturl()
            if not ("html" in ctype or "pdf" in ctype or "text/plain" in ctype or ctype == ""):
                return base | {"status": "skipped", "error": "qo'llab-quvvatlanmaydigan turi: " + ctype[:40]}
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > limit * 4:
                return base | {"status": "error", "error": "hajm chegaradan oshdi"}
            data = r.read(limit * 4 + 1)
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                data = gzip.decompress(data)
        if len(data) > limit:
            return base | {"status": "error", "error": "hajm chegaradan oshdi"}
        if "pdf" in ctype or data[:5] == b"%PDF-":
            if len(data) > MAX_PDF:
                return base | {"status": "skipped", "error": "PDF juda katta"}
        return base | {"status": "ok", "type": ctype, "final": final,
                       "body_gz": base64.b64encode(gzip.compress(data, 6)).decode()}
    except urllib.error.HTTPError as e:
        return base | {"status": "error", "error": f"HTTP {e.code}"}
    except Exception as e:
        return base | {"status": "error", "error": str(e)[:120]}


# ----------------------------------------------------------------------------- crawl
def cmd_crawl(api: Api, a):
    deadline = time.time() + a.minutes * 60
    gate = HostGate(a.delay)
    idle_since = None
    total = {"ok": 0, "skipped": 0, "failed": 0}
    log(f"yig'uvchi ishga tushdi: {a.minutes} daqiqa, {a.threads} oqim, batch {a.batch}")
    with ThreadPoolExecutor(max_workers=a.threads) as pool:
        while time.time() < deadline:
            res = api.call("claim", n=a.batch)
            jobs = res.get("jobs", [])
            if not jobs:
                idle_since = idle_since or time.time()
                if time.time() - idle_since > a.idle * 60:
                    log("navbat bo'sh — to'xtadi")
                    break
                time.sleep(30)
                continue
            idle_since = None
            results = list(pool.map(lambda j: fetch(j, gate), jobs))
            # hajm bo'yicha bo'laklab yuboramiz
            chunk, size = [], 0
            sent = {"ok": 0, "skipped": 0, "failed": 0, "rejected": 0}

            def flush():
                nonlocal chunk, size
                if not chunk:
                    return
                r = api.call("submit", results=chunk)
                for k in sent:
                    sent[k] += int(r.get(k, 0))
                chunk, size = [], 0

            for r in results:
                sz = len(r.get("body_gz", "")) + 200
                if chunk and size + sz > SUBMIT_CHUNK:
                    flush()
                chunk.append(r)
                size += sz
            flush()
            for k in total:
                total[k] += sent[k]
            log(f"batch {len(jobs)}: ok={sent['ok']} skip={sent['skipped']} fail={sent['failed']} rad={sent['rejected']} | jami ok={total['ok']} | navbat={res.get('pending')}")
    log("tugadi:", total)


# ----------------------------------------------------------------------------- seed
JUNK = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|css|js|mp[34]|avi|zip|rar|exe|apk|docx?|xlsx?|pptx?)(\?|$)", re.I)


def send_seed(api: Api, urls: list[str]) -> int:
    added = 0
    for i in range(0, len(urls), 2000):
        r = api.call("seed", urls=urls[i:i + 2000])
        added += int(r.get("added", 0))
    return added


def cmd_seed_file(api: Api, a):
    urls = [l.strip() for l in open(a.file, encoding="utf-8") if l.strip().startswith("http")]
    log(f"{len(urls)} URL, navbatga qo'shildi:", send_seed(api, urls))


def _get_json(url: str, timeout: int = 60):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except Exception:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Common Crawl javob bermadi: " + url)


def cmd_seed_cc(api: Api, a):
    """Common Crawl CDX indeksidan .uz sahifalar ro'yxati (bepul, ochiq). Tasodifiy sahifalardan tanlanadi — domenlar xilma-xil bo'ladi."""
    cdx_base = a.cdx
    if not cdx_base:
        info = json.loads(_get_json("https://index.commoncrawl.org/collinfo.json"))
        cdx_base = info[0]["cdx-api"]
        log("Common Crawl:", info[0].get("name", info[0].get("id")))
    q = "url=" + urllib.parse.quote("*.uz") + "&matchType=domain&output=json&fl=url&filter=status:200&filter=mime:text/html&collapse=urlkey"
    pages = 1
    try:
        meta = json.loads(_get_json(f"{cdx_base}?{q}&showNumPages=true").strip().splitlines()[0])
        pages = int(meta.get("pages", 1))
    except Exception as e:
        log("sahifalar soni aniqlanmadi, 1 deb olindi:", e)
    log(f"indeksda {pages} bo'lak")
    order = list(range(pages))
    random.shuffle(order)
    per_host: dict[str, int] = {}
    batch: list[str] = []
    total_added = 0
    seen = set()
    for pg in order:
        if total_added + len(batch) >= a.limit:
            break
        try:
            txt = _get_json(f"{cdx_base}?{q}&page={pg}")
        except Exception as e:
            log("bo'lak o'tkazib yuborildi:", pg, e)
            continue
        for line in txt.splitlines():
            try:
                u = json.loads(line)["url"]
            except Exception:
                continue
            p = urllib.parse.urlparse(u)
            if p.scheme not in ("http", "https") or JUNK.search(u) or len(u) > 300 or len(p.query) > 60 or u in seen:
                continue
            h = p.netloc.lower()
            if per_host.get(h, 0) >= a.per_host:
                continue
            per_host[h] = per_host.get(h, 0) + 1
            seen.add(u)
            batch.append(u if p.scheme == "https" else u)
        if len(batch) >= 2000:
            total_added += send_seed(api, batch)
            log(f"navbatga qo'shildi: {total_added}, domenlar: {len(per_host)}")
            batch = []
        time.sleep(1.0)       # Common Crawl serveriga hurmat
    if batch:
        total_added += send_seed(api, batch)
    log(f"tugadi: {total_added} URL navbatga qo'shildi, {len(per_host)} domen")


def cmd_stats(api: Api, a):
    print(json.dumps(api.call("stats"), ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl")
    c.add_argument("--minutes", type=float, default=330)
    c.add_argument("--threads", type=int, default=12)
    c.add_argument("--batch", type=int, default=24)
    c.add_argument("--delay", type=float, default=1.0, help="bir saytga so'rovlar orasidagi pauza (soniya)")
    c.add_argument("--idle", type=float, default=10, help="navbat shuncha daqiqa bo'sh tursa to'xtaydi")
    s = sub.add_parser("seed-cc")
    s.add_argument("--limit", type=int, default=20000)
    s.add_argument("--per-host", type=int, default=150)
    s.add_argument("--cdx", default="", help="sinov uchun: CDX API manzili")
    f = sub.add_parser("seed-file")
    f.add_argument("file")
    sub.add_parser("stats")
    a = ap.parse_args()
    base, key = os.environ.get("MTOP_URL", ""), os.environ.get("MTOP_KEY", "")
    if not base or not key:
        sys.exit("MTOP_URL va MTOP_KEY muhit o'zgaruvchilari kerak")
    api = Api(base, key)
    {"crawl": cmd_crawl, "seed-cc": cmd_seed_cc, "seed-file": cmd_seed_file, "stats": cmd_stats}[a.cmd](api, a)


if __name__ == "__main__":
    main()
