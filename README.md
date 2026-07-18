# silo-usenet

Direct-stream Easynews (usenet) content into [Silo](https://siloserver.org) on
demand. Nothing is downloaded ahead of time: when you press play, bytes are
pulled from Easynews with HTTP range requests through a FUSE mount that Silo
reads like any other library.

You give it two things and it configures itself:

1. your **Easynews** username and password
2. a **Silo API key**

It then mounts the content and creates the Silo library for you.

## Why it is a companion, not a Silo plugin

Silo's plugin SDK has no video source capability. Plugins can supply metadata,
scan notifications, HTTP routes, and audiobook/ebook byte backends, but Silo
plays movies and TV from files on disk. So the bytes have to arrive as a mount.
This container is that mount, packaged to self configure. It also works with
Plex and Jellyfin, since it just presents files.

## Requirements

- Docker with FUSE available on the host (`/dev/fuse`, `SYS_ADMIN`).
- Silo (or Plex/Jellyfin) running on the same host, able to bind a shared path.
- A paid Easynews account.
- amd64 host (the image pins the amd64 rclone build; adjust the Dockerfile for arm64).

## Quick start

```bash
cp .env.example .env
# edit .env: Easynews user/pass, SILO_URL, SILO_API_KEY, MOUNT_HOST_PATH,
#            and (recommended) RADARR_URL + RADARR_API_KEY
docker compose up -d              # pulls the published image; no build needed
docker compose logs -f            # watch it mount and provision
```

To build locally instead of pulling, uncomment `build: .` in `docker-compose.yml`.
The image is published multi-arch (amd64 + arm64) by the included GitHub Actions
workflow to `ghcr.io/<owner>/<repo>` on every push to `main` and every `v*` tag.

Then add these two lines to your existing **Silo** service so it can see the
mount (do not start a second Silo):

```yaml
    volumes:
      - type: bind
        source: /mnt/library/usenet-live   # == MOUNT_HOST_PATH
        target: /mnt/library/usenet-live   # == SILO_MEDIA_PATH
        bind:
          propagation: rslave
```

Recreate Silo (`docker compose up -d silo`). Within a scan cycle the titles
appear in the "Usenet Live" library, matched with posters, ready to stream.

## Choosing what shows up

Two sources, merged:

**Dynamic (recommended).** Set `RADARR_URL` + `RADARR_API_KEY`. Every monitored
movie Radarr does not have a file for (`RADARR_FILTER=missing`) appears here,
streamable immediately. Because a Seerr request creates a monitored Radarr
movie, the flow becomes: **request in Seerr -> it is instantly streamable from
usenet**, before Radarr even finishes grabbing it. Once the real download lands,
it drops out of "missing" and moves to your normal Movies library. Set
`RADARR_FILTER=all` to expose your whole Radarr catalog instead.

**Manual.** Edit `config/titles.json` (seeded on first run) for one-off titles:

```json
[
  "Big Buck Bunny (2008)",
  { "title": "Sintel (2010)", "query": "sintel 1080p" }
]
```

Both are cached briefly and picked up live (no restart). Silo's scheduled scan
indexes new titles, or trigger a scan yourself.

Series (Sonarr) is not wired up yet; the same pattern extends to it.

## How it works

```
Silo player
  -> /api/v1/stream/{session}                 (HTTP 206, range)
  -> reads /mnt/library/usenet-live/<Title>/<Title>.mp4
  -> rclone FUSE mount (on demand, no cache by default)
  -> gateway.py (this container)              (stdlib HTTP proxy)
  -> Easynews direct URL                      (Basic auth, HTTP 206 range)
```

`gateway.py` resolves each title via the Easynews search API, picks the best
result (prefers mp4/mkv, avoids RAR/AutoUnRAR posts), exposes it as a browsable
file, and proxies range reads. `provision.py` creates the Silo library and
kicks a scan. `entrypoint.sh` wires them together and supervises.

## Environment

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `EASYNEWS_USER` / `EASYNEWS_PASS` | yes | | Easynews Basic auth |
| `SILO_URL` | for auto-setup | | e.g. `http://silo:8080` |
| `SILO_API_KEY` | for auto-setup | | Silo service-account key |
| `MOUNT_HOST_PATH` | yes | `/mnt/library/usenet-live` | host path of the mount |
| `SILO_MEDIA_PATH` | yes | `/mnt/library/usenet-live` | path Silo sees |
| `LIBRARY_NAME` | no | `Usenet Live` | Silo library name |
| `VFS_CACHE_MODE` | no | `off` | `off` = pure stream, `full` = cache to disk |
| `RADARR_URL` / `RADARR_API_KEY` | no | | dynamic catalog source |
| `RADARR_FILTER` | no | `missing` | `missing` = not-yet-downloaded, `all` = everything |
| `REQUIRE_NONRAR_RATIO` | no | `0.5` | prefer non-RAR unless it is < this fraction of the best |

## Caveats

- Easynews "AutoUnRAR" posts (extracted from RAR on the fly) can be flaky on
  seeks; the gateway avoids them but not every title has an alternative.
- Signed Easynews URLs expire; the gateway re-resolves automatically on 401/403/410.
- This is a working proof of concept, not hardened for scale. Use responsibly
  and within Easynews' terms.

## Uninstall

```bash
docker compose down
```

Then delete the "Usenet Live" library in Silo and remove the rslave bind you
added to the Silo service.
