#!/usr/bin/env python3
"""
Auto-provision Silo: create/verify a library pointing at the usenet mount and
trigger a scan, using the Silo service-account API key. Idempotent and safe to
run on every start. No-op if SILO_URL / SILO_API_KEY are not set.
"""
import os, json, time, urllib.request, urllib.error

SILO_URL = os.environ.get("SILO_URL", "").rstrip("/")
KEY = os.environ.get("SILO_API_KEY", "")
LIB = os.environ.get("LIBRARY_NAME", "Usenet Live")
MPATH = os.environ.get("SILO_MEDIA_PATH", "/mnt/library/usenet-live")
LTYPE = os.environ.get("LIBRARY_TYPE", "movie")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(SILO_URL + "/api/v1" + path, data=data, method=method)
    r.add_header("Authorization", "Bearer " + KEY)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        x = urllib.request.urlopen(r, timeout=30)
        return x.status, x.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


def main():
    if not (SILO_URL and KEY):
        print("[provision] SILO_URL/SILO_API_KEY not set; skipping auto-provision", flush=True)
        return
    # wait for Silo to be reachable
    for _ in range(60):
        s, _b = call("GET", "/libraries")
        if s == 200:
            break
        time.sleep(3)
    else:
        print("[provision] Silo not reachable; giving up (will retry next start)", flush=True)
        return

    s, b = call("GET", "/libraries")
    libs = json.loads(b) if s == 200 else []
    existing = next((l for l in libs if l.get("name") == LIB), None)

    if existing:
        lid = existing["id"]
        paths = existing.get("paths") or []
        if MPATH not in paths:
            existing["paths"] = paths + [MPATH]
            call("PUT", "/libraries/%s" % lid, existing)
            print("[provision] added path %s to library '%s' (id %s)" % (MPATH, LIB, lid), flush=True)
        else:
            print("[provision] library '%s' (id %s) already points at %s" % (LIB, lid, MPATH), flush=True)
    else:
        s, b = call("POST", "/libraries", {"name": LIB, "type": LTYPE, "paths": [MPATH]})
        if s not in (200, 201):
            print("[provision] failed to create library: %s %s" % (s, b[:160]), flush=True)
            return
        lid = json.loads(b)["id"]
        print("[provision] created library '%s' (id %s) -> %s" % (LIB, lid, MPATH), flush=True)

    s, b = call("POST", "/scan", {"library_id": lid})
    print("[provision] scan trigger: %s %s" % (s, b.strip()[:120]), flush=True)


if __name__ == "__main__":
    main()
