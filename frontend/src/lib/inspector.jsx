import { createContext, useCallback, useContext, useMemo, useState } from 'react';

const Ctx = createContext(null);

/**
 * One global slot for "show me the detail behind this thing".
 *
 * Records, quotes, sources, events and run diagnostics all open in the same
 * right-hand inspector. That is what keeps the main canvas readable: the
 * canvas answers "what is happening", the inspector answers "prove it", and
 * neither has to hold both at once.
 */
export function InspectorProvider({ children }) {
  const [item, setItem] = useState(null);
  const [open, setOpen] = useState(false);

  const inspect = useCallback((next) => {
    setItem(next || null);
    setOpen(true);
  }, []);
  const close = useCallback(() => setOpen(false), []);
  const clear = useCallback(() => setItem(null), []);

  const value = useMemo(
    () => ({ item, open, inspect, close, clear }),
    [item, open, inspect, close, clear]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useInspector() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useInspector must be used inside <InspectorProvider>');
  return ctx;
}
