#!/usr/bin/env python3
"""
Easynews -> local HTTP (range) gateway.

Presents each configured title as an on-demand streamable video file under a
folder, resolved lazily from Easynews search. File reads are proxied to the
Easynews direct-download URL with HTTP Range, so nothing is pre-downloaded.
Configuration comes from environment variables plus a hot-reloaded titles file.
Standard library only.
"""
import base64, json, os, re, threading, time, urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

U = os.environ["EASYNEWS_USER"]
P = os.environ["EASYNEWS_PASS"]
BIND = os.environ.get("GATEWAY_BIND", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8791"))
TTL = int(os.environ.get("RESOLVE_TTL", "1800"))
TITLES_FILE = os.environ.get("TITLES_FILE", "/config/titles.json")
# only skip a RAR/AutoUnRAR post when a non-RAR alternative is at least this
# fraction of the best result's size (else the good file is RAR-only and we keep it)
REQUIRE_NONRAR_RATIO = float(os.environ.get("REQUIRE_NONRAR_RATIO", "0.5"))

# dynamic title sources (Radarr = whatever you have requested/monitored; Seerr
# requests flow into Radarr, so requesting a movie makes it appear here)
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
RADARR_FILTER = os.environ.get("RADARR_FILTER", "missing").lower()   # missing | all
SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")   # reserved for series
SOURCE_TTL = int(os.environ.get("SOURCE_TTL", "120"))
MAX_TITLES = int(os.environ.get("MAX_TITLES", "0"))   # cap total exposed titles (0 = unlimited)

# control API (called by the Silo request-router plugin to make a title playable)
CONTROL_BIND = os.environ.get("CONTROL_BIND", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "0"))     # 0 = control API disabled
CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "")
DYNAMIC_TITLES_FILE = os.environ.get("DYNAMIC_TITLES_FILE", "/opt/usenet-vfs/dynamic-titles.json")
DYNAMIC_TV_FILE = os.environ.get("DYNAMIC_TV_FILE", "/opt/usenet-vfs/dynamic-tv.json")
SILO_TV_LIBRARY_ID = int(os.environ.get("SILO_TV_LIBRARY_ID", "0"))   # usenet-live-tv library id
SILO_URL = os.environ.get("SILO_URL", "").rstrip("/")
SILO_API_KEY = os.environ.get("SILO_API_KEY", "")
SILO_LIBRARY_ID = int(os.environ.get("SILO_LIBRARY_ID", "0"))   # usenet-live library id (for scans)

BASE = "https://members.easynews.com"
AUTH = "Basic " + base64.b64encode(("%s:%s" % (U, P)).encode()).decode()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SiloUsenet/1.0"
VIDEO_EXT = (".mkv", ".mp4", ".mov", ".avi", ".m4v", ".ts", ".webm")
CT = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4",
      ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".ts": "video/mp2t",
      ".webm": "video/webm"}
# container preference (fast to probe), then largest; RAR/AutoUnRAR posts are
# deprioritised because on-the-fly extraction is flaky on tail seeks.
PREF = {".mp4": 0, ".m4v": 0, ".mkv": 1, ".mov": 2, ".webm": 3, ".avi": 4, ".ts": 5}

_cache = {}      # title -> (ts, entry)
_lock = threading.Lock()


_titles_cache = {"ts": 0.0, "val": {}}


def _file_titles():
    """Manual list: JSON of strings or {"title","query"} objects."""
    try:
        raw = json.load(open(TITLES_FILE))
    except Exception:
        return {}
    out = {}
    for e in raw:
        if isinstance(e, str):
            out[e] = e
        elif isinstance(e, dict) and e.get("title"):
            out[e["title"]] = e.get("query") or e["title"]
    return out


def _radarr_titles():
    """Movies from Radarr. Default filter 'missing' = monitored and not yet
    downloaded, i.e. exactly the things you want but do not have — instantly
    streamable from usenet. Seerr requests become monitored Radarr movies, so
    a request shows up here automatically."""
    if not (RADARR_URL and RADARR_API_KEY):
        return {}
    r = urllib.request.Request(RADARR_URL + "/api/v3/movie")
    r.add_header("X-Api-Key", RADARR_API_KEY)
    movies = json.loads(urllib.request.urlopen(r, timeout=20).read().decode())
    out = {}
    for m in movies:
        if not m.get("monitored", True):
            continue
        if RADARR_FILTER == "missing":
            if m.get("hasFile"):
                continue
            if not m.get("isAvailable"):   # skip unreleased; it cannot be on usenet yet
                continue
        title, year = m.get("title"), m.get("year")
        if not title:
            continue
        disp = "%s (%s)" % (title, year) if year else title
        out[disp] = ("%s %s" % (title, year)) if year else title
    return out


def load_titles():
    """Merged {display_title: search_query} from the manual file plus any
    configured dynamic sources. Cached for SOURCE_TTL so listings are cheap."""
    now = time.time()
    if _titles_cache["val"] and now - _titles_cache["ts"] < SOURCE_TTL:
        return _titles_cache["val"]
    merged = {}
    merged.update(_file_titles())
    radarr = {}
    try:
        radarr = _radarr_titles()
    except Exception:
        pass
    if MAX_TITLES > 0 and len(radarr) > MAX_TITLES:   # cap only the Radarr backlog
        radarr = dict(list(radarr.items())[:MAX_TITLES])
    merged.update(radarr)
    try:                                     # on-demand titles from the plugin are never capped
        for e in json.load(open(DYNAMIC_TITLES_FILE)):
            if isinstance(e, dict) and e.get("title"):
                merged[e["title"]] = e.get("query") or e["title"]
    except Exception:
        pass
    _titles_cache["ts"] = now
    _titles_cache["val"] = merged
    return merged


def en_open(url, rng=None, timeout=60):
    r = urllib.request.Request(url)
    r.add_header("Authorization", AUTH)
    r.add_header("User-Agent", UA)
    if rng:
        r.add_header("Range", rng)
    return urllib.request.urlopen(r, timeout=timeout)


def en_search(query, pby="75"):
    params = {"fly": "2", "sb": "1", "pno": "1", "pby": str(pby), "u": "1", "chxu": "1",
              "chxgx": "1", "st": "basic", "gps": query, "vv": "1", "safeO": "0",
              "s1": "dsize", "s1d": "-"}
    qs = "&".join("%s=%s" % (k, urllib.parse.quote(v)) for k, v in params.items()) + "&fty%5B%5D=VIDEO"
    return json.loads(en_open(BASE + "/2.0/search/solr-search/?" + qs, timeout=45).read().decode())


_EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
_tv_cache = {}   # show -> (ts, {(season,ep): entry})


def series_episodes(show, query):
    """Resolve a show's episodes from Easynews results: parse SxxExx from
    filenames and keep the best release per episode. No metadata source needed;
    it exposes whatever the search surfaces (popular shows fare best)."""
    now = time.time()
    c = _tv_cache.get(show)
    if c and now - c[0] < TTL:
        return c[1]
    try:
        d = en_search(query, pby=250)
    except Exception:
        return {}
    downURL, dlFarm = d.get("downURL"), d.get("dlFarm")
    eps = {}
    for it in d.get("data", []):
        ext = (it.get("11") or it.get("extension") or "").lower()
        if ext not in VIDEO_EXT:
            continue
        fn = it.get("10") or it.get("fn") or ""
        m = _EP_RE.search(fn)
        if not m:
            continue
        se, ep = int(m.group(1)), int(m.group(2))
        size = int(it.get("rawSize") or 0)
        if size <= 0 or not it.get("sig") or not it.get("hash"):
            continue
        subj = (it.get("6") or "").lower()
        rar = 1 if (".rar" in subj or "autounrar" in subj) else 0
        score = (rar, PREF.get(ext, 9), -size)
        key = (se, ep)
        if key not in eps or score < eps[key]["score"]:
            eps[key] = {"score": score, "ext": ext,
                        "url": "%s/%s/%s/%s%s/%s" % (downURL, dlFarm, it["sig"], it["hash"], ext,
                                                     urllib.parse.quote(fn + ext)),
                        "file": "%s - S%02dE%02d%s" % (show, se, ep, ext),
                        "size": size}
    _tv_cache[show] = (now, eps)
    return eps


def load_tv_shows():
    """{show_folder: search_query} of on-demand series added by the plugin."""
    out = {}
    try:
        for e in json.load(open(DYNAMIC_TV_FILE)):
            if isinstance(e, dict) and e.get("show"):
                out[e["show"]] = e.get("query") or e["show"]
    except Exception:
        pass
    return out


def build_entry(title, query):
    d = en_search(query)
    downURL, dlFarm = d.get("downURL"), d.get("dlFarm")
    cands = []
    for it in d.get("data", []):
        ext = (it.get("11") or it.get("extension") or "").lower()
        if ext not in VIDEO_EXT:
            continue
        size = int(it.get("rawSize") or 0)
        if size <= 0 or not it.get("sig") or not it.get("hash"):
            continue
        subj = (it.get("6") or "").lower()
        fn = it.get("10") or it.get("fn") or title
        cands.append({
            "size": size, "ext": ext, "file": title + ext,
            "rar": (".rar" in subj or "autounrar" in subj),
            "url": "%s/%s/%s/%s%s/%s" % (downURL, dlFarm, it["sig"], it["hash"], ext,
                                         urllib.parse.quote(fn + ext)),
        })
    if not cands:
        return None

    def quality(c):
        return (PREF.get(c["ext"], 9), -c["size"])   # lower is better

    best = min(cands, key=quality)
    # prefer a non-RAR post, but only if it is not much worse than the best
    # overall (RAR/AutoUnRAR posts can be flaky on seeks, but a tiny non-RAR
    # sample is worse than a good AutoUnRAR release)
    non_rar = [c for c in cands if not c["rar"]]
    if non_rar:
        best_non_rar = min(non_rar, key=quality)
        if best_non_rar["size"] >= REQUIRE_NONRAR_RATIO * best["size"]:
            best = best_non_rar
    return best


def resolve(title, query, force=False):
    now = time.time()
    with _lock:
        c = _cache.get(title)
        if c and not force and now - c[0] < TTL:
            return c[1]
    entry = build_entry(title, query)
    if entry:
        with _lock:
            _cache[title] = (now, entry)
    return entry


def _listing(dirs, files):
    rows = ['<a href="%s/">%s/</a><br>' % (urllib.parse.quote(n), n) for n in dirs]
    rows += ['<a href="%s">%s</a><br>' % (urllib.parse.quote(n), n) for n in files]
    return ("<html><body>\n%s\n</body></html>\n" % "\n".join(rows)).encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _head(self, code, hdrs, body_len=None):
        self.send_response(code)
        for k, v in hdrs.items():
            self.send_header(k, v)
        if body_len is not None:
            self.send_header("Content-Length", str(body_len))
        self.end_headers()

    def do_HEAD(self):
        self._route(True)

    def do_GET(self):
        self._route(False)

    def _route(self, head):
        path = urllib.parse.unquote(self.path.split("?", 1)[0])
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == "tv":
            return self._route_tv(parts[1:], head)
        titles = load_titles()
        try:
            if len(parts) == 0:
                body = _listing(list(titles.keys()), [])
                self._head(200, {"Content-Type": "text/html"}, len(body))
                if not head:
                    self.wfile.write(body)
            elif len(parts) == 1:
                t = parts[0]
                if t not in titles:
                    self._head(404, {"Content-Type": "text/plain"}, 0); return
                e = resolve(t, titles[t])
                # unresolvable title -> empty folder (200) so the scanner does
                # not error; Silo just adds no item for it
                body = _listing([], [e["file"]] if e else [])
                self._head(200, {"Content-Type": "text/html"}, len(body))
                if not head:
                    self.wfile.write(body)
            elif len(parts) == 2:
                t, fname = parts
                e = resolve(t, titles[t]) if t in titles else None
                if not e or fname != e["file"]:
                    self._head(404, {"Content-Type": "text/plain"}, 0); return
                self._stream(t, titles[t], e, head)
            else:
                self._head(404, {"Content-Type": "text/plain"}, 0)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self._head(502, {"Content-Type": "text/plain"}, 0)
            except Exception:
                pass

    def _stream(self, title, query, e, head):
        ctype = CT.get(e["ext"], "application/octet-stream")
        if head:
            self._head(200, {"Accept-Ranges": "bytes", "Content-Type": ctype}, e["size"])
            return
        rng = self.headers.get("Range")
        up = None
        for attempt in (0, 1):
            try:
                up = en_open(e["url"], rng=rng, timeout=60)
                break
            except urllib.error.HTTPError as ex:
                if ex.code in (401, 403, 404, 410) and attempt == 0:
                    e2 = resolve(title, query, force=True)   # signed URL likely expired
                    if e2:
                        e = e2; continue
                self._head(ex.code, {"Content-Type": "text/plain"}, 0); return
            except Exception:
                self._head(502, {"Content-Type": "text/plain"}, 0); return
        hdrs = {"Accept-Ranges": "bytes", "Content-Type": ctype}
        if up.headers.get("Content-Range"):
            hdrs["Content-Range"] = up.headers.get("Content-Range")
        cl = up.headers.get("Content-Length")
        self.send_response(up.status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        if cl:
            self.send_header("Content-Length", cl)
        self.end_headers()
        try:
            while True:
                chunk = up.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            up.close()

    def _route_tv(self, parts, head):
        shows = load_tv_shows()
        try:
            if len(parts) == 0:                                  # /tv/ -> show folders
                body = _listing(list(shows.keys()), [])
                self._head(200, {"Content-Type": "text/html"}, len(body))
                if not head:
                    self.wfile.write(body)
            elif len(parts) == 1:                                # /tv/{Show}/ -> season folders
                show = parts[0]
                if show not in shows:
                    return self._head(404, {"Content-Type": "text/plain"}, 0)
                eps = series_episodes(show, shows[show])
                seasons = sorted({se for (se, ep) in eps})
                body = _listing(["Season %02d" % se for se in seasons], [])
                self._head(200, {"Content-Type": "text/html"}, len(body))
                if not head:
                    self.wfile.write(body)
            elif len(parts) == 2:                                # /tv/{Show}/Season NN/ -> episodes
                show, season = parts
                if show not in shows:
                    return self._head(404, {"Content-Type": "text/plain"}, 0)
                m = re.search(r"(\d+)", season)
                se = int(m.group(1)) if m else -1
                eps = series_episodes(show, shows[show])
                files = [e["file"] for (s, ep), e in sorted(eps.items()) if s == se]
                body = _listing([], files)
                self._head(200, {"Content-Type": "text/html"}, len(body))
                if not head:
                    self.wfile.write(body)
            elif len(parts) == 3:                                # /tv/{Show}/Season NN/{file} -> stream
                show, _season, fname = parts
                if show not in shows:
                    return self._head(404, {"Content-Type": "text/plain"}, 0)
                eps = series_episodes(show, shows[show])
                ent = next((e for e in eps.values() if e["file"] == fname), None)
                if not ent:
                    return self._head(404, {"Content-Type": "text/plain"}, 0)
                self._stream_tv(show, shows[show], ent, head)
            else:
                self._head(404, {"Content-Type": "text/plain"}, 0)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self._head(502, {"Content-Type": "text/plain"}, 0)
            except Exception:
                pass

    def _stream_tv(self, show, query, ent, head):
        ctype = CT.get(ent["ext"], "application/octet-stream")
        if head:
            return self._head(200, {"Accept-Ranges": "bytes", "Content-Type": ctype}, ent["size"])
        rng = self.headers.get("Range")
        up = None
        for attempt in (0, 1):
            try:
                up = en_open(ent["url"], rng=rng, timeout=60)
                break
            except urllib.error.HTTPError as ex:
                if ex.code in (401, 403, 404, 410) and attempt == 0:
                    _tv_cache.pop(show, None)
                    e2 = next((e for e in series_episodes(show, query).values() if e["file"] == ent["file"]), None)
                    if e2:
                        ent = e2
                        continue
                self._head(ex.code, {"Content-Type": "text/plain"}, 0)
                return
            except Exception:
                self._head(502, {"Content-Type": "text/plain"}, 0)
                return
        hdrs = {"Accept-Ranges": "bytes", "Content-Type": ctype}
        if up.headers.get("Content-Range"):
            hdrs["Content-Range"] = up.headers.get("Content-Range")
        cl = up.headers.get("Content-Length")
        self.send_response(up.status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        if cl:
            self.send_header("Content-Length", cl)
        self.end_headers()
        try:
            while True:
                chunk = up.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            up.close()


# --------------------------------------------------------------------------- #
# control API: the Silo request-router plugin calls this to make a title
# playable on demand. /add searches usenet, registers the title, and triggers a
# Silo scan; /status reports whether Silo has indexed it yet.
# --------------------------------------------------------------------------- #
import os
_dyn_lock = threading.Lock()


def silo_call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SILO_URL + "/api/v1" + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + SILO_API_KEY)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read().decode()


def silo_scan(lib_id=None):
    lib = lib_id or SILO_LIBRARY_ID
    if SILO_URL and SILO_API_KEY and lib:
        try:
            silo_call("POST", "/scan", {"library_id": lib})
        except Exception:
            pass


def silo_has(tmdb):
    if not (SILO_URL and SILO_API_KEY and tmdb):
        return False
    try:
        st, b = silo_call("GET", "/catalog/items/movie-tmdb-%s" % tmdb)
        if st != 200:
            return False
        for v in (json.loads(b).get("versions") or []):
            if str(v.get("file_path") or "").startswith("/mnt/library/usenet-live"):
                return True
    except Exception:
        pass
    return False


def add_dynamic_title(title, query):
    with _dyn_lock:
        try:
            cur = json.load(open(DYNAMIC_TITLES_FILE))
        except Exception:
            cur = []
        if not any(isinstance(e, dict) and e.get("title") == title for e in cur):
            cur.append({"title": title, "query": query})
            tmp = DYNAMIC_TITLES_FILE + ".tmp"
            open(tmp, "w").write(json.dumps(cur))
            os.replace(tmp, DYNAMIC_TITLES_FILE)
    _titles_cache["ts"] = 0.0   # force listings to include the new title immediately


def add_dynamic_show(show, query):
    with _dyn_lock:
        try:
            cur = json.load(open(DYNAMIC_TV_FILE))
        except Exception:
            cur = []
        if not any(isinstance(e, dict) and e.get("show") == show for e in cur):
            cur.append({"show": show, "query": query})
            tmp = DYNAMIC_TV_FILE + ".tmp"
            open(tmp, "w").write(json.dumps(cur))
            os.replace(tmp, DYNAMIC_TV_FILE)
    _tv_cache.pop(show, None)


def silo_has_series(tvdb, tmdb):
    if not (SILO_URL and SILO_API_KEY):
        return False
    for cid in (("series-tvdb-%s" % tvdb) if tvdb else "", ("series-tmdb-%s" % tmdb) if tmdb else ""):
        if not cid:
            continue
        try:
            st, b = silo_call("GET", "/catalog/items/" + cid)
            if st == 200:
                for fp in (json.loads(b).get("folder_paths") or []):
                    if str(fp).startswith("/mnt/library/usenet-live-tv"):
                        return True
        except Exception:
            pass
    return False


class Control(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            return self._json(200, {"ok": True})
        self._json(404, {"ok": False})

    def do_POST(self):
        if CONTROL_TOKEN and self.headers.get("X-Api-Key") != CONTROL_TOKEN:
            return self._json(401, {"ok": False, "message": "unauthorized"})
        path = self.path.split("?")[0]
        body = self._read()
        title = (body.get("title") or "").strip()
        year = body.get("year")
        tmdb = body.get("tmdb")
        tvdb = body.get("tvdb")
        media_type = (body.get("media_type") or "movie").lower()
        disp = "%s (%s)" % (title, year) if year else title
        if path == "/add":
            if not title:
                return self._json(400, {"ok": False, "message": "title required"})
            if media_type == "series":
                try:
                    found = len(series_episodes(disp, title)) > 0
                except Exception:
                    found = False
                if not found:
                    return self._json(200, {"ok": False, "available": False, "message": "no episodes on usenet"})
                add_dynamic_show(disp, title)
                silo_scan(SILO_TV_LIBRARY_ID)
                return self._json(200, {"ok": True, "available": silo_has_series(tvdb, tmdb), "message": "streamable"})
            query = ("%s %s" % (title, year)) if year else title
            try:
                found = build_entry(disp, query) is not None
            except Exception:
                found = False
            if not found:
                return self._json(200, {"ok": False, "available": False, "message": "not available on usenet"})
            add_dynamic_title(disp, query)
            silo_scan()
            return self._json(200, {"ok": True, "available": silo_has(tmdb), "message": "streamable"})
        if path == "/status":
            if media_type == "series":
                avail = silo_has_series(tvdb, tmdb)
                if not avail:
                    silo_scan(SILO_TV_LIBRARY_ID)
                return self._json(200, {"ok": True, "available": avail})
            avail = silo_has(tmdb)
            if not avail:
                silo_scan()   # keep nudging the scan until the title is indexed
            return self._json(200, {"ok": True, "available": avail})
        self._json(404, {"ok": False})


if __name__ == "__main__":
    if CONTROL_PORT:
        threading.Thread(
            target=lambda: ThreadingHTTPServer((CONTROL_BIND, CONTROL_PORT), Control).serve_forever(),
            daemon=True,
        ).start()
        print("[gateway] control API on %s:%d" % (CONTROL_BIND, CONTROL_PORT), flush=True)
    print("[gateway] listening on %s:%d titles=%s" % (BIND, PORT, TITLES_FILE), flush=True)
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
