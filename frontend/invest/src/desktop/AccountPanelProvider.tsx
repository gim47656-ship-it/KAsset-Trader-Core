import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { AccountPanelResponse } from "../types/invest";
import { fetchAccountPanel } from "../api/accountPanel";

export interface AccountPanelContextValue {
  data: AccountPanelResponse | undefined;
  error: string | undefined;
  loading: boolean;
  refreshing: boolean;
  lastLoadedAt: number | undefined;
  /** Lazy fetch entry-point. Safe to call multiple times. */
  load: () => void;
  /** Re-fetch the current account panel. No-op if never loaded. */
  reload: () => void;
}

const AccountPanelContext = createContext<AccountPanelContextValue | null>(null);

export function AccountPanelProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [data, setData] = useState<AccountPanelResponse | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [lastLoadedAt, setLastLoadedAt] = useState<number | undefined>();

  const inflightRef = useRef<AbortController | null>(null);
  const hasLoadedRef = useRef(false);

  const load = useCallback(() => {
    inflightRef.current?.abort();
    const controller = new AbortController();
    inflightRef.current = controller;

    setError(undefined);
    if (hasLoadedRef.current) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    fetchAccountPanel({ signal: controller.signal })
      .then((r) => {
        if (controller.signal.aborted) return;
        setData(r);
        setLoading(false);
        setRefreshing(false);
        setLastLoadedAt(Date.now());
        hasLoadedRef.current = true;
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        setLoading(false);
        setRefreshing(false);
        hasLoadedRef.current = true;
      });
  }, []);

  const reload = useCallback(() => {
    // Lazy mode: do not auto-fetch unless we have previously loaded.
    if (!hasLoadedRef.current) return;
    load();
  }, [load]);

  const value = useMemo(
    () => ({
      data,
      error,
      loading,
      refreshing,
      lastLoadedAt,
      load,
      reload,
    }),
    [data, error, loading, refreshing, lastLoadedAt, load, reload],
  );

  return (
    <AccountPanelContext.Provider value={value}>
      {children}
    </AccountPanelContext.Provider>
  );
}

export function useAccountPanelContext(): AccountPanelContextValue {
  const ctx = useContext(AccountPanelContext);
  if (!ctx) throw new Error("useAccountPanelContext must be used within AccountPanelProvider");
  return ctx;
}
