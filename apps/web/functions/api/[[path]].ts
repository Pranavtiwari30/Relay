// Cloudflare Pages Function: same-origin proxy from the static UI to the API.
//
// Why a proxy: with the UI and API on one origin the session cookie is
// first-party (SameSite=Lax), so sign-in keeps working in browsers that block
// third-party cookies, and the API needs no cross-site cookie configuration.
//
// RELAY_API_ORIGIN is set as a Pages environment variable (not a secret), for
// example https://relay-api-staging-xxxx.a.run.app.
//
// NOTE: written from Cloudflare's documented Pages Functions API and not yet
// exercised against a live Pages project. See docs/PROJECT_STATE.md.

interface Env {
  RELAY_API_ORIGIN: string;
}

// Hop-by-hop headers must not be forwarded.
const DROP = new Set(["host", "connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"]);

export const onRequest: PagesFunction<Env> = async ({ request, env }) => {
  const incoming = new URL(request.url);
  const upstream = new URL(incoming.pathname + incoming.search, env.RELAY_API_ORIGIN);
  // Only ever proxy to the configured API origin.
  if (upstream.origin !== new URL(env.RELAY_API_ORIGIN).origin)
    return new Response("bad upstream", { status: 500 });

  const headers = new Headers();
  for (const [name, value] of request.headers) if (!DROP.has(name.toLowerCase())) headers.set(name, value);

  return fetch(upstream, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    redirect: "manual",
  });
};
