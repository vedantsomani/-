# Deploying SPHEREx Time Machine

The public setup has two pieces:

```
browser --> Vercel (static React build + /api rewrite) --> Fly.io machine (FastAPI + volume at /data)
                                                              \--> IRSA (SIA2 + cutouts), CDS, AWS open data
```

- **Backend** (`backend/`): one container on Fly.io with a persistent volume that holds the field cache.
  It must run as exactly one machine: download and processing jobs live in that process's memory, and
  a Fly volume attaches to a single machine.
- **Frontend** (`frontend/`): a static Vite build on Vercel. The app calls relative `/api/...` URLs, so
  `frontend/vercel.json` rewrites `/api/*` to the Fly app. The browser only ever talks to the Vercel
  origin, so there is no CORS to configure and shared links keep working.

Files involved: `fly.toml`, `backend/Dockerfile`, `backend/docker-entrypoint.sh`, `.dockerignore`,
`frontend/vercel.json`, `scripts/demo-targets.txt`, `scripts/prewarm-demo.mjs`, `scripts/prewarm-demo.sh`.

## Before you start

1. The code is pushed to a GitHub repository (Vercel imports from it; Fly builds from your local checkout).
2. You have a Fly.io account and a Vercel account. Either may ask for a payment card, see [Costs](#costs).
3. You do not need Docker locally: `fly deploy` builds the image on Fly's builders.

## 1. Install flyctl and log in

Windows (PowerShell):

```powershell
iwr https://fly.io/install.ps1 -useb | iex
```

macOS / Linux: `curl -L https://fly.io/install.sh | sh` (or `brew install flyctl`).

Open a new terminal, then:

```bash
fly version
fly auth login      # opens a browser; use `fly auth signup` for a new account
```

Run every `fly` command below from the **repo root** (the folder containing `fly.toml`).

## 2. Name the app and create it

Edit `fly.toml`:

- `app = "YOUR-APP-NAME"`: app names are global on Fly, so pick something unique, e.g. `spherex-tm-yourname`.
- `primary_region = "lax"`: optional. IRSA is at Caltech and the cutout downloads dominate traffic,
  so a US West region is a reasonable default. List regions with `fly platform regions`.

Then create the app without deploying. Either:

```bash
fly apps create YOUR-APP-NAME          # simplest; keeps fly.toml exactly as written
```

or let flyctl do it with the provided config:

```bash
fly launch --no-deploy --copy-config --name YOUR-APP-NAME
```

If you use `fly launch`, decline when it offers to tweak settings, then run `git diff fly.toml` to confirm
it did not rewrite the file (the volume mount, health check, 1 GB memory and `auto_stop_machines = "off"`
must still be there).

## 3. Create the volume

The volume holds `/data/cache` (raw cutouts, processed frames, stacks). It must be in the same region as
`primary_region`, and its name must match `source` under `[[mounts]]` in `fly.toml` (`spherex_data`).

```bash
fly volumes create spherex_data --region lax --size 10
```

Fly warns that a single volume has no redundancy. That is acceptable here: the cache can always be rebuilt
from IRSA, it just takes time. Pick a size that fits your demo and extend it later if needed
(`fly volumes extend <volume-id> -s 20`). Check real usage after the prewarm step with
`fly ssh console -C "df -h /data"`.

## 4. Environment and secrets

Nothing is secret: the app needs no API keys. Settings are plain environment variables with the
`SPHEREX_` prefix, set in the `[env]` block of `fly.toml`:

| Variable | Value in this setup | Purpose |
| --- | --- | --- |
| `SPHEREX_CACHE_DIR` | `/data/cache` | Cache location. Must be on the volume, or every deploy wipes it. |
| `PORT` | `8080` | Port uvicorn listens on. Must equal `internal_port` in `fly.toml`. |
| `SPHEREX_CORS_ORIGINS` | unset (localhost only) | JSON list, e.g. `'["https://my-app.vercel.app"]'`. Only needed if a page on another origin calls the Fly URL directly. Not needed with the Vercel rewrite. |
| `SPHEREX_DOWNLOAD_CONCURRENCY`, `SPHEREX_PROCESSING_WORKERS` | unset (code defaults 12 and 4) | Lower these (commented example in `fly.toml`) if the 1 GB machine runs out of memory. |

If you ever need a real secret, use `fly secrets set NAME=value`; it applies on the next restart.
Any setting in `backend/app/config.py` can be overridden the same way, for example
`SPHEREX_COLLECTIONS='["spherex_dr2","spherex_dr1"]'` when a new data release lands.

## 5. Deploy the backend

```bash
fly deploy --ha=false
```

`--ha=false` makes Fly start one machine instead of two (its default for HTTP apps). A second machine would
have no volume and no shared job state.

Check it:

```bash
fly status
fly logs                                   # Ctrl+C to stop following
curl https://YOUR-APP-NAME.fly.dev/api/health
# {"ok":true,"collections":["spherex_dr1","spherex_qr3"]}
```

The first build installs astropy, scipy and the rest of `requirements.txt`, so it takes a few minutes.

## 6. Pre-build the demo fields

The first visit to a sky position downloads every SPHEREx exposure covering it from IRSA (hundreds of
cutouts, minutes per field). Pre-building the demo fields makes the demo instant. The targets are in
`scripts/demo-targets.txt` (coordinates, one per line, `#` comments allowed).

**Locally** (fills `backend/cache`, useful for a local demo):

```bash
npm run prewarm                       # all targets concurrently
npm run prewarm -- --sequential       # one at a time
npm run prewarm -- --dry-run          # only print what would run
```

**In the deployed Fly machine** (fills the volume, which is what visitors hit). The script is baked into the
image at `/app/prewarm-demo.sh` and reads `/app/demo-targets.txt`. It runs one target at a time by default
to stay inside 1 GB of RAM, and it drops from root to the `app` user so the files it writes stay writable
by the web server.

```bash
# Foreground: keep the terminal open until it finishes (can take a long time)
fly ssh console -C "sh /app/prewarm-demo.sh"

# Or detached, then follow the log (survives a dropped SSH session)
fly ssh console -C "sh -c 'nohup sh /app/prewarm-demo.sh > /data/prewarm.log 2>&1 &'"
fly ssh console -C "tail -n 20 /data/prewarm.log"
```

Other options: `--fov 10` (arcmin), `--parallel` (all targets in one process), or a different targets file as
the last argument. The prewarm process is separate from the web server; they share the on-disk cache, and
the server loads finished fields from disk. If prewarm is killed for memory (check `fly logs`), raise memory
temporarily with `fly scale memory 2048`, run it, then go back with `fly scale memory 1024`.

The demo targets are built at the app's default 5 arcmin FOV. The field key includes the centre (snapped
to 1e-5 deg) and the FOV, so a visitor only gets the instant path when they open that same centre and FOV.
Targets with fast proper motion must be listed at the epoch the frontend centres them on (see the comment
at the top of `scripts/demo-targets.txt`). If a demo link is not instant, compare the `ra`, `dec` and `fov`
in the link with the targets file.

## 7. Deploy the frontend on Vercel

1. Edit `frontend/vercel.json` and replace the placeholder in the first rewrite:

   ```json
   { "source": "/api/(.*)", "destination": "https://YOUR-BACKEND.fly.dev/api/$1" }
   ```

   **This is the one value you must change.** Put in your Fly hostname, `https://YOUR-APP-NAME.fly.dev`
   (the `app` name from `fly.toml`), and keep the `/api/$1` suffix. If you leave the placeholder, the page
   loads but every API call fails.
2. Commit and push to GitHub.
3. In Vercel: **Add New... > Project**, then import the repository.
4. Set **Root Directory** to `frontend`. Vercel reads `frontend/vercel.json` from there (framework Vite,
   `npm ci`, `npm run build`, output `dist`). No environment variables are needed.
5. Deploy. Later pushes to the production branch redeploy automatically.

The second rewrite (`/(.*)` to `/index.html`) is the single-page-app fallback. Vercel serves real files
(assets, favicon) first and only then applies rewrites, so the fallback only catches unknown paths.

## 8. Test it

1. `https://YOUR-PROJECT.vercel.app/api/health` should return the same JSON as the Fly URL. If it returns
   the app's HTML, the first rewrite is not in effect (wrong root directory, or `vercel.json` not pushed).
   If it returns an error, the destination host is wrong or the machine is down.
2. Open a link with a query string in a fresh tab:
   `https://YOUR-PROJECT.vercel.app/?ra=133.76002&dec=-7.24221&fov=5`. It should load the app on that
   field, instantly if you prewarmed it.
3. In the browser dev tools network tab, confirm `/api/frames` requests go to your Vercel hostname (same
   origin) and succeed.

## Updating

- **Frontend:** `git push`. Vercel builds and promotes it. Roll back from the Vercel dashboard (Deployments).
- **Backend:** from the repo root, `fly deploy --ha=false`. This restarts the machine, so jobs running at
  that moment stop. Cutouts already on the volume are kept, so requesting the field again resumes quickly.
  Avoid deploying while a long prewarm is running. Bumping `PROCESSING_VERSION` in
  `backend/app/processing.py` makes fields reprocess on the next request (or re-run the prewarm).
- **Rollback:** `fly releases` lists releases; redeploy an earlier commit if needed.
- **Settings only:** edit `[env]` in `fly.toml` and run `fly deploy --ha=false`, or use `fly secrets set`
  for a restart without a rebuild.

## Costs

Check current pricing before committing; plans, free allowances and card requirements change.

- **Fly.io** bills for machine time, volume storage and bandwidth. This app keeps one machine running all
  the time (auto-stop is off on purpose), so expect a steady small charge rather than scale-to-zero
  behaviour. A free allowance may or may not apply to your account. See https://fly.io/docs/about/pricing/
- **Vercel** has a free Hobby tier with usage limits, intended for personal, non-commercial use. Images
  and API responses proxied through the `/api` rewrite count toward its traffic. See https://vercel.com/pricing
- Downloads from IRSA and AWS open data are free to you, but each cutout is a few MB, so a popular
  uncached field costs time and Fly bandwidth. Prewarming the demo fields avoids most of it.
- Review the usage pages in both dashboards after the first week.

## Troubleshooting

**Slow first request, or `/api/health` times out right after a deploy.**
Fly restarts the machine on deploy and the health check has a 30 s grace period. Wait until `fly status`
shows the check passing. If you change `auto_stop_machines` to allow stopping, a stopped machine needs several
seconds to start and a field being built when it stops is lost. Keep it `"off"`.

**Volume full** (`No space left on device` in `fly logs`, or jobs failing at the write step).
Check `fly ssh console -C "df -h /data"`. Either extend the volume (`fly volumes list`, then
`fly volumes extend <id> -s 20`) or delete cached fields you do not need:
`fly ssh console -C "ls /data/cache/fields"`, then `fly ssh console -C "rm -rf /data/cache/fields/<key>"`.
Do not delete the demo fields unless you plan to re-run the prewarm.

**IRSA returns 503 or is slow.**
The backend retries with backoff, and frames that still fail are reported as failed for that field. Wait a
few minutes and request the field again. `refresh=true` on `/api/frames` ignores the cached manifest and
rebuilds the field; cutouts already on the volume should not be fetched again. If IRSA is down for
maintenance, only fields already on the volume work, which is one more reason to prewarm.

**CORS errors in the browser console.**
You should see none while the frontend uses relative `/api` URLs through the Vercel rewrite. If you do, the
page is calling the Fly hostname directly (a stale build, or `VITE_API_TARGET` pointed somewhere). Fix the
frontend to use `/api`, or add the page's origin to `SPHEREX_CORS_ORIGINS` as a JSON list in `fly.toml`.
The API only allows GET.

**Vercel shows 404 or the wrong page on a deep link.**
Check that Root Directory is `frontend` and that `frontend/vercel.json` is in the deployed commit.

**Vercel `/api` returns 502 or 504.**
The Fly machine is down or restarting, or the destination hostname in `vercel.json` is wrong. Test the Fly
URL directly (`curl https://YOUR-APP-NAME.fly.dev/api/health`) and read `fly logs`.

**Out of memory (machine restarts, `Out of memory` in `fly logs`).**
Lower `SPHEREX_DOWNLOAD_CONCURRENCY` and `SPHEREX_PROCESSING_WORKERS`, or raise memory
(`fly scale memory 2048`; update `memory` in `fly.toml` to match so a later deploy does not undo it).

**`fly deploy` created two machines.**
Run `fly machines list`, destroy the one without a volume (`fly machines destroy <id> --force`), and keep
using `--ha=false`.

## Optional: test the image locally

If you have Docker, from the repo root:

```bash
docker build -f backend/Dockerfile -t spherex-api .
docker run --rm -p 8080:8080 -v spherex_data:/data spherex-api
curl http://localhost:8080/api/health
```
