# Deploying the frontend on Cloudflare Pages

The GAMAS frontend is a static Vite build. Cloudflare Pages serves it on your subdomain, and a small
Pages Function proxies `/api/*` to the Python backend, so the browser only talks to one origin:

```
browser --> gamas.example.org (Cloudflare Pages: static files + functions/api/[[path]].ts)
                 \--> /api/* --> API_ORIGIN (FastAPI backend with a persistent disk)
```

**The backend cannot run on Cloudflare.** It is a long-running FastAPI process that downloads
SPHEREx cutouts from IRSA, keeps background jobs in memory and caches gigabytes of processed images
on disk. Workers and Pages Functions have no persistent local disk and short CPU limits. Host the
backend somewhere with a disk and point the proxy at it. Two options:

- **Fly.io (recommended for a public site).** Follow [deploy.md](deploy.md) up to and including
  "deploy the backend"; you can skip the Vercel part. The backend URL is then
  `https://YOUR-APP-NAME.fly.dev`.
- **Cloudflare Tunnel (quick demo from your own machine).** Run the backend locally
  (`npm run dev`, or `uvicorn app.main:app --port 8000` in `backend/`), then expose it with
  `cloudflared tunnel --url http://localhost:8000` (a temporary `*.trycloudflare.com` URL), or a
  named tunnel on a hostname of your own such as `api.gamas.example.org`. Use that URL as
  `API_ORIGIN`. The site is only up while your machine and the tunnel are running.

Files involved: `frontend/functions/api/[[path]].ts` (the proxy), `frontend/public/_headers`
(cache and security headers), `frontend/src/lib/api.ts` (`apiUrl`, the one place API URLs are built).

## 1. Create the Pages project

1. Push the repository to GitHub.
2. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** → **Connect to Git**, pick the
   repository.
3. Build settings:

   | Setting | Value |
   | --- | --- |
   | Production branch | `main` |
   | Framework preset | None (or Vite) |
   | Root directory (advanced) | `frontend` |
   | Build command | `npm run build` |
   | Build output directory | `dist` |

   Because the root directory is `frontend`, Pages also finds `frontend/functions/` and deploys the
   proxy automatically. Node 20 or newer is needed: if the build log shows an older version, add the
   environment variable `NODE_VERSION` = `22`.
4. **Settings → Environment variables** (Production, and Preview if you use preview deploys):

   | Variable | Example | Notes |
   | --- | --- | --- |
   | `API_ORIGIN` | `https://gamas-api.fly.dev` | Backend origin, no trailing slash, no `/api`. Read at request time by the Function. |
   | `NODE_VERSION` | `22` | Only if the default build image is older. |

5. Save and **Retry deployment** (environment variables apply to new deployments).

## 2. Custom subdomain

1. Pages project → **Custom domains** → **Set up a custom domain** → enter e.g. `gamas.example.org`.
2. If the zone `example.org` is on Cloudflare, Pages creates the DNS record for you. Otherwise add a
   `CNAME` record at your DNS provider: name `gamas`, target `YOUR-PROJECT.pages.dev`.
3. Wait until the domain shows **Active** (certificate issued; usually a few minutes).

## 3. Backend settings for this setup

Set these on the backend host (on Fly: `fly secrets set NAME=value`):

- `SPHEREX_TRUST_PROXY=1`: the Function sets `X-Forwarded-For` to exactly the client address
  Cloudflare saw, so rate limits apply per visitor instead of per Cloudflare edge.
- `SPHEREX_TRUSTED_PROXY_HOPS`: which entry from the right of `X-Forwarded-For` is the visitor. It
  is 1 when nothing sits between Cloudflare and the backend, and 2 when the backend host's own proxy
  appends one more address (Fly.io does). Check with a request to `/api/health` and the backend log
  if unsure.
- CORS is not needed with the proxy: the browser never calls the backend directly.

## 4. Verify

```bash
curl -i https://gamas.example.org/api/health
```

Expect `HTTP/2 200` and the backend's JSON. A `500` with `API_ORIGIN is not configured` means the
variable is missing on this deployment; a `502` means the Function could not reach the backend
(wrong URL, backend stopped, tunnel closed). Then open `https://gamas.example.org/`, pick an example
target, and check in the browser's network tab that `/api/frames?...` and the `.png` images load from
your own domain.

Static assets are served with `Cache-Control: public, max-age=31536000, immutable` (hashed files in
`/assets/`), the HTML shell with `no-cache`; see `frontend/public/_headers`, which also sets
`X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `Permissions-Policy` and a
Content-Security-Policy that allows the Aladin Lite sky viewer (WebAssembly, HiPS tiles over https).

## Alternative: call the backend directly (CORS)

If you prefer not to proxy, build with an absolute backend URL:

```bash
VITE_API_BASE=https://gamas-api.fly.dev npm run build
```

(set `VITE_API_BASE` as a build environment variable in Pages). Every API request and image then
goes straight to the backend, which must allow your site's origin:
`SPHEREX_CORS_ORIGINS='["https://gamas.example.org"]'`. The backend's CORS middleware currently
allows only `GET` and `DELETE`, so the batch page's `POST /api/batch` needs the proxy (or a backend
change). Images are requested with `crossOrigin="anonymous"` so the viewer can still read pixels.
The Content-Security-Policy in `_headers` already allows https connections and images from any host.

Nothing in this guide deploys anything for you, logs in, or runs `wrangler`: all steps are done in
the Cloudflare dashboard by the project owner.
