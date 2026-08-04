/*
 * The offline capture service worker.
 *
 * It exists to do one thing: let `/estimates/capture` cold-start with no
 * network, so an estimator standing in a building can open the app and work.
 * Everything here is shaped by three constraints that are easy to break by
 * accident, so each one is written down where it takes effect.
 *
 * 1. CACHE `response.clone()`, NEVER A REBUILT RESPONSE.
 *    The document's CSP is minted per request in `middleware.ts` around a
 *    nonce that Next stamps into the inline bootstrap scripts of that same
 *    response. Cached whole — headers included — the script tags and the
 *    policy carry the same nonce, they agree, and the page hydrates.
 *    Re-fabricating a `Response` from the body would drop the
 *    `Content-Security-Policy` header, leaving a document whose nonce no
 *    policy names, and the page would fail SILENTLY. That failure is the one
 *    the design originally predicted for caching at all; it is reachable
 *    only by getting this line wrong.
 *
 * 2. A CACHED DOCUMENT REPLAYS ITS NONCE, SO IT IS SERVED FOR 24 HOURS.
 *    The cache entry's life IS the nonce's life. `DOCUMENT_MAX_AGE_MS` is
 *    the decided bound (design §8.2), enforced on SERVE rather than by an
 *    eviction policy that only runs when something else happens to trigger
 *    it. `/_next/static/*` is exempt: content-addressed per build, carries
 *    no nonce.
 *
 * 3. IT CACHES DOCUMENTS, NEVER `/api/*`.
 *    An HTTP cache is keyed by URL, and one person may hold memberships in
 *    two companies (migration 0031) — so a URL-keyed cache of `/api/catalog
 *    /items` would serve company A's pricing while the user acts as company
 *    B. Data caching is the app's job, in IndexedDB, keyed by
 *    `(user_id, active_company_id)`: see `lib/offline/store.ts`.
 *
 * Nothing is cached until the page asks. Registration and priming both
 * happen behind the capture screen's "Make available offline" button
 * (design §8.1), so a device that never presses it holds nothing.
 */

// Bump on any change to what is cached or how. `activate` deletes every
// cache this worker owns whose name is not in CURRENT_CACHES, so an old
// worker's entries cannot outlive it.
const VERSION = "v1";
const DOCUMENT_CACHE = `bs-documents-${VERSION}`;
// Cache-time stamps live beside the documents rather than inside them,
// because the documents must be stored byte-for-byte as they arrived
// (constraint 1) — adding a header would mean rebuilding the response.
const DOCUMENT_META_CACHE = `bs-document-meta-${VERSION}`;
const STATIC_CACHE = `bs-static-${VERSION}`;
const CURRENT_CACHES = [DOCUMENT_CACHE, DOCUMENT_META_CACHE, STATIC_CACHE];

const DOCUMENT_MAX_AGE_MS = 24 * 60 * 60 * 1000;

/**
 * The only paths whose documents may be cached.
 *
 * An allowlist, not a rule about what looks cacheable: caching every
 * navigation would put every screen this user visited on the device, and
 * the feature needs exactly one of them.
 */
const OFFLINE_PATHS = ["/estimates/capture"];

self.addEventListener("install", () => {
  // The page registers this worker and then immediately asks it to prime,
  // so waiting for every other tab to close first would mean the button
  // does nothing on a device that already has the app open elsewhere.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((name) => name.startsWith("bs-") && !CURRENT_CACHES.includes(name))
          .map((name) => caches.delete(name))
      );
      await self.clients.claim();
    })()
  );
});

function isOfflinePath(pathname) {
  return OFFLINE_PATHS.includes(pathname);
}

/** A document is cacheable only if it is really the document we asked for. */
function isCacheableDocument(response) {
  return (
    response.ok &&
    // `middleware.ts` redirects an unauthenticated navigation to /login.
    // Following that and caching the result would store the SIGN-IN page
    // under the capture URL, and the estimator would cold-start offline
    // into a login form they cannot submit.
    !response.redirected &&
    (response.headers.get("content-type") ?? "").includes("text/html")
  );
}

async function stampCachedAt(url) {
  const meta = await caches.open(DOCUMENT_META_CACHE);
  await meta.put(url, new Response(String(Date.now())));
}

async function cachedAt(url) {
  const meta = await caches.open(DOCUMENT_META_CACHE);
  const stamp = await meta.match(url);
  if (!stamp) return null;
  const value = Number(await stamp.text());
  return Number.isFinite(value) ? value : null;
}

async function putDocument(url, response) {
  const cache = await caches.open(DOCUMENT_CACHE);
  // clone(), and the clone is what is stored — see constraint 1.
  await cache.put(url, response.clone());
  await stampCachedAt(url);
}

/**
 * The `/_next/static/*` URLs a cached document names in its own markup.
 *
 * THIS IS THE HALF THAT IS EASY TO MISS. Caching the document alone gives a
 * cold start that fetches its JavaScript, fails, and renders an empty page —
 * markup with no React attached. It looks like the app is broken rather than
 * like the cache is incomplete, because nothing errors: the document served
 * perfectly.
 *
 * The document's own script/link tags are exactly the right set to take: it
 * is the list the browser will ask for when it replays that document, no
 * more and no less. Priming from what the CURRENT page happens to have
 * loaded is not the same list — the estimator may have arrived here by
 * client-side navigation, whose chunk set differs from a fresh load's.
 */
function assetsReferencedBy(html) {
  const pattern = /(?:src|href)="(\/_next\/static\/[^"]+)"/g;
  const found = new Set();
  let match = pattern.exec(html);
  while (match !== null) {
    found.add(match[1]);
    match = pattern.exec(html);
  }
  return Array.from(found);
}

/**
 * Cache every asset, and FAIL if one cannot be stored.
 *
 * A missing chunk is a blank screen on site, hours later, with no way to
 * recover it. Better for the button to say it did not work while there is
 * still a network to fix it with.
 */
async function cacheAssets(urls) {
  const cache = await caches.open(STATIC_CACHE);
  await Promise.all(
    urls.map(async (url) => {
      if (await cache.match(url)) return;
      const response = await fetch(url, { cache: "reload" });
      if (!response.ok) throw new Error(`Refused to cache ${url}: ${response.status}`);
      await cache.put(url, response.clone());
    })
  );
}

/**
 * Assets the PAGE reports having loaded — best effort, unlike the above.
 *
 * A superset belt to the document's braces: shared chunks a future build
 * splits differently, or anything requested after first paint. One of these
 * failing is not a reason to refuse the whole prime, because the cold start
 * does not depend on them.
 */
async function cacheAssetsBestEffort(urls) {
  const cache = await caches.open(STATIC_CACHE);
  await Promise.all(
    urls.map(async (url) => {
      try {
        if (await cache.match(url)) return;
        const response = await fetch(url, { cache: "reload" });
        if (response.ok) await cache.put(url, response.clone());
      } catch {
        // Ignored on purpose — see the docstring.
      }
    })
  );
}

/**
 * The cached document, or null if there is none or it has aged out.
 *
 * Expiry deletes rather than merely declines: an entry that will never be
 * served again is a document sitting on the device for no reason, and its
 * nonce is exactly what the bound exists to retire.
 */
async function freshCachedDocument(url) {
  const cache = await caches.open(DOCUMENT_CACHE);
  const cached = await cache.match(url);
  if (!cached) return null;

  const stamp = await cachedAt(url);
  const expired = stamp === null || Date.now() - stamp > DOCUMENT_MAX_AGE_MS;
  if (expired) {
    await cache.delete(url);
    const meta = await caches.open(DOCUMENT_META_CACHE);
    await meta.delete(url);
    return null;
  }
  return cached;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Build output: content-addressed per build, so cache-first is safe and
  // is what makes an offline cold start find its JavaScript. No TTL — see
  // constraint 2.
  if (url.pathname.startsWith("/_next/static/")) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(STATIC_CACHE);
        const cached = await cache.match(request);
        if (cached) return cached;
        const response = await fetch(request);
        if (response.ok) await cache.put(request, response.clone());
        return response;
      })()
    );
    return;
  }

  // Everything else — including every `/api/*` call — falls through to the
  // network untouched. Not responding at all is deliberate: it leaves the
  // request exactly as it would have been with no worker installed.
  if (request.mode !== "navigate" || !isOfflinePath(url.pathname)) return;

  // Network-first, so an online estimator is never shown yesterday's app.
  // The cache is the fallback, not the source.
  event.respondWith(
    (async () => {
      try {
        const response = await fetch(request);
        if (isCacheableDocument(response)) await putDocument(url.pathname, response);
        return response;
      } catch (networkError) {
        const cached = await freshCachedDocument(url.pathname);
        if (cached) return cached;
        throw networkError;
      }
    })()
  );
});

/**
 * Priming and clearing, both driven by the page.
 *
 * Priming fetches the allowlisted documents from inside the worker rather
 * than relying on the estimator to have navigated to each one, so pressing
 * the button is enough. `cache: "reload"` bypasses the HTTP cache, so what
 * is stored is a document minted now — with a nonce minted now.
 *
 * Every message replies on the port it arrived on, so the page can report
 * "ready" (or a failure) rather than guessing.
 */
self.addEventListener("message", (event) => {
  const data = event.data ?? {};
  const reply = (payload) => event.ports[0]?.postMessage(payload);

  if (data.type === "PRIME_DOCUMENTS") {
    event.waitUntil(
      (async () => {
        try {
          for (const path of OFFLINE_PATHS) {
            const response = await fetch(path, {
              cache: "reload",
              credentials: "include",
              headers: { Accept: "text/html" },
            });
            if (!isCacheableDocument(response)) {
              throw new Error(`Refused to cache ${path}: ${response.status}`);
            }
            // Read before storing: `putDocument` consumes a clone, and the
            // body can only be read once from each.
            const html = await response.clone().text();
            await putDocument(path, response);
            await cacheAssets(assetsReferencedBy(html));
          }
          await cacheAssetsBestEffort(data.assets ?? []);
          reply({ ok: true });
        } catch (err) {
          reply({ ok: false, error: String(err) });
        }
      })()
    );
    return;
  }

  if (data.type === "CLEAR") {
    event.waitUntil(
      (async () => {
        const names = await caches.keys();
        await Promise.all(
          names.filter((name) => name.startsWith("bs-")).map((name) => caches.delete(name))
        );
        reply({ ok: true });
      })()
    );
  }
});
