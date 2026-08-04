"use client";

/**
 * Talking to `public/sw.js`.
 *
 * Registration is NOT automatic. Nothing about this app registers a worker
 * until the estimator presses "Make available offline" on the capture
 * screen (design §8.1) — a device that never asks holds no cached document
 * and no cached pricing. That is what makes caching the company's price
 * list an acceptable trade rather than a standing one.
 */

const SW_PATH = "/sw.js";

export function serviceWorkerSupported(): boolean {
  return typeof navigator !== "undefined" && "serviceWorker" in navigator;
}

/**
 * Register, and resolve only once the worker is actually CONTROLLING this
 * page.
 *
 * Resolving on registration alone would be a lie the next line depends on:
 * `sw.js` calls `skipWaiting()`/`clients.claim()`, but claiming is
 * asynchronous, and a prime message posted before it lands reaches a worker
 * that is active yet controls nothing — so the documents would be cached
 * while `fetch` events still bypassed it. Waiting for `controllerchange`
 * (or for an existing controller) removes that window.
 */
export async function ensureServiceWorker(): Promise<ServiceWorker> {
  if (!serviceWorkerSupported()) {
    throw new Error("This browser cannot work offline (no service worker support).");
  }

  await navigator.serviceWorker.register(SW_PATH, { scope: "/" });
  if (navigator.serviceWorker.controller) return navigator.serviceWorker.controller;

  await navigator.serviceWorker.ready;
  if (navigator.serviceWorker.controller) return navigator.serviceWorker.controller;

  return new Promise<ServiceWorker>((resolve, reject) => {
    const timeout = setTimeout(() => {
      navigator.serviceWorker.removeEventListener("controllerchange", onChange);
      reject(new Error("The offline worker did not start. Reload the page and try again."));
    }, 10_000);
    function onChange() {
      const controller = navigator.serviceWorker.controller;
      if (!controller) return;
      clearTimeout(timeout);
      navigator.serviceWorker.removeEventListener("controllerchange", onChange);
      resolve(controller);
    }
    navigator.serviceWorker.addEventListener("controllerchange", onChange);
  });
}

/**
 * Send one message and wait for the worker's answer on a private port.
 *
 * A reply rather than fire-and-forget because the screen reports what
 * happened: "ready to work offline" is a claim, and it should be made only
 * when the worker says the documents are cached.
 */
function ask(controller: ServiceWorker, message: unknown): Promise<{ ok: boolean; error?: string }> {
  return new Promise((resolve, reject) => {
    const channel = new MessageChannel();
    const timeout = setTimeout(() => reject(new Error("The offline worker did not respond.")), 30_000);
    channel.port1.onmessage = (event) => {
      clearTimeout(timeout);
      resolve(event.data);
    };
    controller.postMessage(message, [channel.port2]);
  });
}

/**
 * Every `/_next/static/*` URL this page is known to have loaded.
 *
 * Sent to the worker as a supplement to what the cached document itself
 * names. The worker treats these as best-effort — the cold start depends on
 * the document's own script tags, which it extracts for itself — but a chunk
 * fetched after first paint appears here and nowhere else.
 *
 * `performance` entries cover what was requested; the DOM covers what is
 * referenced but may have been served from the HTTP cache without a fresh
 * entry. Neither alone is complete.
 */
function loadedStaticAssets(): string[] {
  if (typeof window === "undefined") return [];
  const fromTiming = performance.getEntriesByType("resource").map((entry) => entry.name);
  const fromDom = [
    ...Array.from(document.querySelectorAll("script[src]")).map((el) => (el as HTMLScriptElement).src),
    ...Array.from(document.querySelectorAll("link[rel=stylesheet]")).map(
      (el) => (el as HTMLLinkElement).href
    ),
  ];
  return Array.from(new Set([...fromTiming, ...fromDom])).filter(
    (url) => url.startsWith(window.location.origin) && url.includes("/_next/static/")
  );
}

/** Cache the allowlisted documents, so a cold start with no network works. */
export async function primeDocuments(): Promise<void> {
  const controller = await ensureServiceWorker();
  const result = await ask(controller, {
    type: "PRIME_DOCUMENTS",
    assets: loadedStaticAssets(),
  });
  if (!result.ok) throw new Error(result.error ?? "Failed to store the offline screen.");
}

/**
 * Drop every cache the worker owns, and unregister it.
 *
 * Called from "Remove offline data", from logout and from a company switch.
 * Unregistering as well as clearing is deliberate: leaving a registered
 * worker behind would silently re-cache the capture document on the next
 * visit, and the estimator asked for the data to be gone.
 *
 * Never throws. Every caller is either a teardown path (logout) or a user
 * asking to remove data — failing loudly there would block a sign-out on a
 * cache that could not be opened.
 */
export async function clearServiceWorkerCaches(): Promise<void> {
  if (!serviceWorkerSupported()) return;
  try {
    const controller = navigator.serviceWorker.controller;
    if (controller) await ask(controller, { type: "CLEAR" });
    const registrations = await navigator.serviceWorker.getRegistrations();
    await Promise.all(registrations.map((registration) => registration.unregister()));
  } catch {
    // Nothing actionable, and nothing that should stop a sign-out.
  }
}
