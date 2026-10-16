import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Global hotkeys.
 *
 * Bare keys are suppressed while the user is typing — pressing "3" inside a
 * prompt must not navigate away from the prompt. Modifier combos are NOT
 * suppressed: Cmd/Ctrl+K has to open the palette even when the caret is in a
 * textarea, which is exactly where a user is when they reach for it.
 */
export function useHotkey(map, { enabled = true } = {}) {
  const mapRef = useRef(map);
  mapRef.current = map;

  useEffect(() => {
    if (!enabled) return undefined;
    const onKey = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      const t = e.target;
      const tag = t?.tagName;
      const typing =
        tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable;

      if (typing && !mod && e.key !== 'Escape') return;

      const combo = `${mod ? 'mod+' : ''}${
        e.shiftKey && e.key.length > 1 ? 'shift+' : ''
      }${e.key.toLowerCase()}`;
      const fn = mapRef.current?.[combo] || mapRef.current?.[e.key.toLowerCase()];
      if (fn) {
        e.preventDefault();
        fn(e);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [enabled]);
}

export function useMediaQuery(query) {
  const [match, setMatch] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(query).matches
  );
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatch(mq.matches);
    on();
    mq.addEventListener?.('change', on);
    return () => mq.removeEventListener?.('change', on);
  }, [query]);
  return match;
}

/**
 * Reveal on first intersection. One-shot, so content doesn't re-animate while
 * the user scrolls back up.
 */
export function useReveal({ threshold = 0.12, margin = '0px 0px -8% 0px' } = {}) {
  const ref = useRef(null);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || shown) return undefined;
    if (typeof IntersectionObserver === 'undefined') {
      setShown(true);
      return undefined;
    }
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShown(true);
          io.disconnect();
        }
      },
      { threshold, rootMargin: margin }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [shown, threshold, margin]);

  return [ref, shown];
}

/** localStorage-backed state that survives reloads and never throws. */
export function useLocalStore(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initial : JSON.parse(raw);
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (next) => {
      setValue((prev) => {
        const v = typeof next === 'function' ? next(prev) : next;
        try {
          window.localStorage.setItem(key, JSON.stringify(v));
        } catch {
          /* ignore */
        }
        return v;
      });
    },
    [key]
  );
  return [value, set];
}

/**
 * Row density, applied as an attribute on `<html>`.
 *
 * The Settings toggle persisted `dg_dense` and nothing read it, so it saved a
 * preference that changed nothing — a control that looked actionable and was
 * not. Applying it here, once, as `data-density`, lets `index.css` re-declare
 * row metrics in one place instead of threading a prop through every table.
 *
 * Also exposed as `readDensity()` for non-React code.
 */
export function useDensity(enabled = false) {
  useEffect(() => {
    try {
      const root = document.documentElement;
      if (enabled) root.setAttribute('data-density', 'compact');
      else root.removeAttribute('data-density');
    } catch {
      /* no DOM (tests) — nothing to apply */
    }
  }, [enabled]);
}

/** Current density preference, without subscribing. */
export function readDensity() {
  try {
    return window.localStorage.getItem('dg_dense') === 'true';
  } catch {
    return false;
  }
}

/** Tick a value on an interval — used for "12s ago" labels. */
export function useTicker(ms = 1000, enabled = true) {
  const [, set] = useState(0);
  useEffect(() => {
    if (!enabled) return undefined;
    const id = setInterval(() => set((n) => n + 1), ms);
    return () => clearInterval(id);
  }, [ms, enabled]);
}
