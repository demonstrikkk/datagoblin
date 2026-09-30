/**
 * The stored page text, fetched on demand.
 *
 * It is 21kB and it is only needed once a reader actually opens a proof, so it
 * ships as a static asset rather than inside the route's JavaScript bundle. The
 * landing page's initial payload is the 3kB fixture — the records, the statuses
 * and the counts — which is what the first viewport actually needs to draw.
 *
 * `enabled` exists so a component that is mounted but nowhere near the viewport
 * does not pull 21kB the reader did not ask for. The request is cached at
 * module scope, so whatever asks first, everything else waits on the same one.
 */

import { useEffect, useState } from 'react';

const URL_PATH = '/landing-evidence-page.json';

let pending = null;

export function loadStoredPage() {
  if (!pending) {
    pending = fetch(URL_PATH, { cache: 'force-cache' }).then((r) => {
      if (!r.ok) throw new Error(`stored page unavailable (${r.status})`);
      return r.json();
    });
    // A failed fetch must not poison the cache for the rest of the session.
    pending.catch(() => {
      pending = null;
    });
  }
  return pending;
}

/** True once the returned ref's element has come within `margin` of the viewport. */
export function useNearViewport(margin = '600px') {
  const [ref, setRef] = useState(null);
  const [near, setNear] = useState(false);

  useEffect(() => {
    if (!ref || near) return undefined;
    if (!('IntersectionObserver' in window)) {
      setNear(true);
      return undefined;
    }
    const io = new IntersectionObserver(
      ([e]) => {
        if (e.isIntersecting) {
          setNear(true);
          io.disconnect();
        }
      },
      { rootMargin: margin },
    );
    io.observe(ref);
    return () => io.disconnect();
  }, [ref, near, margin]);

  return [setRef, near];
}

export function useStoredPage(enabled = true) {
  const [state, setState] = useState({ text: null, error: null });

  useEffect(() => {
    if (!enabled) return undefined;
    let live = true;
    loadStoredPage().then(
      (data) => {
        if (live) setState({ text: data.markdown, error: null });
      },
      (err) => {
        if (live) setState({ text: null, error: err.message || 'unavailable' });
      },
    );
    return () => {
      live = false;
    };
  }, [enabled]);

  return state;
}