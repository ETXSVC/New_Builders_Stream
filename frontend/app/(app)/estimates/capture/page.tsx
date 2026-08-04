import { CaptureScreen } from "@/components/estimates/CaptureScreen";

/**
 * The one route a service worker caches (`public/sw.js`'s OFFLINE_PATHS).
 *
 * An ordinary `(app)` route, which is the whole point of the CSP spike: a
 * worker that caches the response WHOLE — headers included — replays a
 * document whose script tags and whose policy carry the same nonce, so it
 * hydrates offline. No separate entry point, no static CSP, no change to
 * `middleware.ts`.
 */
export default function CapturePage() {
  return <CaptureScreen />;
}
