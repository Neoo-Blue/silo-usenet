# Installation guide

This walks you from nothing to streaming usenet titles inside Silo. Budget about
ten minutes. It also works with Plex and Jellyfin (any server that reads files).

## 1. Prerequisites

- A host running Docker with FUSE available (`/dev/fuse` present, ability to grant
  `SYS_ADMIN`). Almost every normal Linux Docker host qualifies.
- Silo already running on that host (this guide assumes Docker Compose).
- A paid **Easynews** account (username + password).
- Optional but recommended: **Radarr** (so the catalog fills itself), and
  **Seerr/Overseerr/Jellyseerr** in front of it.

## 2. Collect three things

1. **Easynews username and password.** Your normal login.
2. **A Silo API key.** In Silo: Settings, API Keys, create one. It starts `sa_`.
3. **A Radarr API key (optional).** In Radarr: Settings, General, API Key.

## 3. Get the files

```bash
git clone https://github.com/Neoo-Blue/silo-usenet.git
cd silo-usenet
cp .env.example .env
```

## 4. Fill in .env

```ini
EASYNEWS_USER=your_easynews_login
EASYNEWS_PASS=your_easynews_password

SILO_URL=http://silo:8080            # how this container reaches Silo's API
SILO_API_KEY=sa_xxxxxxxxxxxxxxxxxxxx

# Host path the mount lives on, and the path Silo sees it at (keep them equal).
MOUNT_HOST_PATH=/mnt/library/usenet-live
SILO_MEDIA_PATH=/mnt/library/usenet-live

# Optional: fill the catalog automatically from Radarr.
RADARR_URL=http://radarr:7878
RADARR_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
RADARR_FILTER=missing                # only what you have not downloaded yet
MAX_TITLES=60                         # cap the first scan; 0 = unlimited
```

`SILO_URL` / `RADARR_URL` use the container/service name if they share a Docker
network (`silo`, `radarr`), or a LAN IP and port otherwise.

## 5. Let Silo see the mount

Silo can only read the titles if the mount is visible inside its container. Add a
bind of the same host path, with `rslave` propagation, to your existing Silo
service (do not start a second Silo):

```yaml
# in your Silo compose service
    volumes:
      - type: bind
        source: /mnt/library/usenet-live     # == MOUNT_HOST_PATH
        target: /mnt/library/usenet-live     # == SILO_MEDIA_PATH
        bind:
          propagation: rslave
```

Create the host directory first: `sudo mkdir -p /mnt/library/usenet-live`.

## 6. Start it

```bash
docker compose up -d
docker compose logs -f
```

You should see the gateway start, the mount come up, and a provision line like
`created library 'Usenet Live' (id N) -> /mnt/library/usenet-live`. Then recreate
Silo so it picks up the new bind:

```bash
docker compose -f /path/to/your/silo-compose.yml up -d silo
```

## 7. Verify

- In Silo, open the **Usenet Live** library. Titles should appear (matched with
  posters after the first scan).
- Play one. It starts within a few seconds as bytes stream from Easynews.

If a scan is slow, that is expected the first time: every title triggers an
Easynews search and a probe. Lower `MAX_TITLES` or start with a manual list to
keep the first pass quick.

## 8. Choose what appears

- **Automatic:** with Radarr set, every monitored movie you do not have shows up.
  A **Seerr request** creates a monitored Radarr movie, so requesting a film makes
  it streamable here immediately, before the real download finishes.
- **Manual:** edit `config/titles.json` (created on first run). A JSON list of
  `"Title (Year)"` strings, or `{"title": "...", "query": "..."}` objects.

## Troubleshooting

- **Library is empty in Silo.** The mount is not visible to Silo. Recheck the
  `rslave` bind on the Silo service and that both paths match. `docker exec <silo>
  ls /mnt/library/usenet-live` should list folders.
- **`fusermount: permission denied` / mount fails.** The container needs
  `cap_add: [SYS_ADMIN]`, `devices: [/dev/fuse]`, and
  `security_opt: [apparmor:unconfined]` (already in the compose).
- **A title will not play or scan errors on it.** Some Easynews posts are
  AutoUnRAR (extracted from RAR on the fly) and seek poorly. The gateway avoids
  them when a good alternative exists; some obscure titles have none.
- **401/403 from Easynews.** Check the username/password. Signed URLs expire and
  are refreshed automatically, so transient 401s during long idle periods are fine.
- **Nothing from Radarr.** Confirm `RADARR_URL`/`RADARR_API_KEY`, and that you
  actually have monitored, not-yet-downloaded movies (`RADARR_FILTER=missing`).

## Uninstall

```bash
docker compose down
```

Then delete the **Usenet Live** library in Silo and remove the `rslave` bind you
added to the Silo service.
