// Cloudflare Pages Function: same-origin proxy for /api/* to the FastAPI backend.
//
// The browser only ever talks to the Pages origin, so the app keeps using relative /api URLs,
// shared links keep working and no CORS is needed. Set API_ORIGIN (e.g. https://gamas-api.fly.dev,
// no trailing slash) in the Pages project's environment variables. See docs/deploy-cloudflare.md.
//
// Method, path, query, headers and body are forwarded; the response body streams back with its status
// and headers (including Cache-Control and Retry-After). X-Forwarded-For is set to exactly the
// client address Cloudflare saw (a client-supplied value is discarded, so it cannot be spoofed).

interface Env {
  API_ORIGIN?: string
}

interface Context {
  request: Request
  env: Env
}

/** Connection-level headers that must not be forwarded by a proxy (RFC 9110 section 7.6.1). */
const HOP_BY_HOP = [
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'proxy-connection',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]

const problem = (status: number, detail: string) =>
  new Response(JSON.stringify({ detail }), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  })

export async function onRequest({ request, env }: Context): Promise<Response> {
  const origin = (env.API_ORIGIN ?? '').trim().replace(/\/+$/, '')
  if (!/^https?:\/\//.test(origin)) {
    return problem(500, 'API_ORIGIN is not configured for this Pages project.')
  }

  const url = new URL(request.url)
  const upstreamUrl = `${origin}${url.pathname}${url.search}`

  const headers = new Headers(request.headers)
  for (const h of HOP_BY_HOP) headers.delete(h)
  headers.delete('host')
  // Cloudflare-internal request headers mean nothing to the backend.
  for (const h of [...headers.keys()]) if (h.startsWith('cf-') && h !== 'cf-connecting-ip') headers.delete(h)
  const clientIp = request.headers.get('cf-connecting-ip')
  if (clientIp) headers.set('X-Forwarded-For', clientIp)
  else headers.delete('X-Forwarded-For')
  headers.set('X-Forwarded-Proto', url.protocol.replace(':', ''))
  headers.set('X-Forwarded-Host', url.host)

  const hasBody = !['GET', 'HEAD'].includes(request.method)
  let upstream: Response
  try {
    upstream = await fetch(upstreamUrl, {
      method: request.method,
      headers,
      // Request bodies here are small JSON (batch submissions); buffering avoids streaming-upload quirks.
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: 'manual',
    })
  } catch (e) {
    return problem(502, `The GAMAS backend could not be reached (${(e as Error).message}).`)
  }

  const out = new Headers(upstream.headers)
  for (const h of HOP_BY_HOP) out.delete(h)
  // Stream the body through untouched (images, JSON, CSV, HTML report).
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out })
}
