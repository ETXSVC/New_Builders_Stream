"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";

/**
 * A photo the browser cannot fetch on its own.
 *
 * `<img src="/api/team/{id}/photo">` would be simpler and does not work
 * here: the product session keeps its access token in memory (AuthContext),
 * not in a cookie, so a plain image request arrives without an Authorization
 * header and the BFF answers 401. That is the access control working — the
 * bytes sit on a volume shared by every tenant, and the backend serves them
 * behind the directory's role check rather than off a static path.
 *
 * So the bytes are fetched like any other authenticated request and handed
 * to the element as an object URL, which is revoked on unmount or when the
 * member changes. Without that revoke, a list of twenty people scrolled a
 * few times leaks twenty blobs per pass.
 *
 * Falls back to initials rather than a broken-image icon: a member with no
 * photo is the normal state, not a failure.
 */
export function MemberPhoto({
  src: endpoint,
  hasPhoto,
  name,
  version,
  size = 40,
}: {
  /**
   * The BFF endpoint serving these bytes — `/api/team/{id}/photo` for the
   * directory, `/api/team/me/photo` for your own.
   *
   * A URL rather than a user id, because the self route deliberately takes
   * no id: the product session has no user id in it (AuthContext holds a
   * token and a role), and `/team/me` exists so the browser never needs one.
   */
  src: string;
  hasPhoto: boolean;
  /** Used for the initials fallback, and as the alt text. */
  name: string;
  /**
   * Anything that changes when the BYTES change — in practice the profile's
   * `updated_at`, which moves on the photo write.
   *
   * Without it, REPLACING a photo shows the old one until the page is left:
   * `userId` is the same, the token is the same, and `has_photo` was already
   * true and is true again, so the effect below has nothing to react to. The
   * first upload works (false → true) and every later one appears to do
   * nothing, which is the worst version of this bug to debug.
   */
  version?: string | null;
  size?: number;
}) {
  const { accessToken } = useAuth();
  const [src, setSrc] = React.useState<string | null>(null);

  React.useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;

    // Deferred to a promise callback so no setState runs synchronously in
    // the effect body (react-hooks/set-state-in-effect) — the same shape
    // every loader in this app uses.
    void Promise.resolve().then(async () => {
      // Clear first: on a change of member this element is reused, and
      // leaving the previous person's photo up while the new one loads
      // shows the wrong face next to the right name.
      setSrc(null);
      if (!hasPhoto || !accessToken) return;
      try {
        const response = await fetch(endpoint, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (!response.ok) return;
        const blob = await response.blob();
        // Checked BEFORE creating the URL, not after: a blob created for an
        // unmounted component has nothing left to revoke it.
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      } catch {
        // A photo that will not load is not worth an error banner over the
        // record it belongs to. The initials stand in.
      }
    });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [accessToken, endpoint, hasPhoto, version]);

  const initials = name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");

  if (!src) {
    return (
      <span
        aria-hidden="true"
        className="inline-flex shrink-0 items-center justify-center rounded-full bg-slate-200 font-medium text-slate-600"
        style={{ width: size, height: size, fontSize: Math.round(size / 2.6) }}
      >
        {initials || "?"}
      </span>
    );
  }

  // next/image is not an option here: it cannot optimise a `blob:` URL, and
  // there is no remote URL to hand it — the bytes arrive through an
  // authenticated fetch, for the reason in this file's docstring.
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={name}
      width={size}
      height={size}
      className="shrink-0 rounded-full object-cover"
      style={{ width: size, height: size }}
    />
  );
}
