import React, { useEffect, useMemo } from "react";
import { Outlet } from "react-router-dom";
import type { LoaderFunctionArgs, ShouldRevalidateFunction } from "react-router-dom";
import { AppShell } from "../components/layout/AppShell";
import { useLiveOdds } from "../hooks/useLiveOdds";
import { useCommanderStore } from "../store/useCommanderStore";
import { useMarketStore } from "../store/useMarketStore";
import type { ConnectionStatus } from "../store/useMarketStore";

export interface CommanderOutletContext {
  connection: ConnectionStatus;
  quoteCount: number;
}

const EXECUTION_PANEL_LIMIT = 12;

/**
 * Runs BEFORE the matched route renders on every navigation (including back/forward and the
 * initial load), so the store is already correct when the new tree paints: no flash, and no
 * setState-during-render.
 */
export function commanderLoader({ request }: LoaderFunctionArgs): null {
  useCommanderStore.getState().syncCommander(new URL(request.url).pathname);
  return null;
}

/** The root route has no params, so force revalidation whenever the pathname changes. */
export const shouldRevalidateCommander: ShouldRevalidateFunction = ({ currentUrl, nextUrl, defaultShouldRevalidate }) =>
  currentUrl.pathname !== nextUrl.pathname || defaultShouldRevalidate;

export default function CommanderLayout(): React.ReactNode {
  const commander = useCommanderStore((s) => s.activeCommander);
  const { status, lastUpdatedAt, error, reconnect } = useLiveOdds({ enabled: commander.networkProtocol !== "REST" });

  const odds = useMarketStore((s) => s.odds);
  const fetchMarkets = useMarketStore((s) => s.fetchMarkets);

  useEffect(() => {
    const controller = new AbortController();
    void fetchMarkets(controller.signal);
    return () => controller.abort();
  }, [fetchMarkets]);

  const quoteList = useMemo(() => Object.values(odds), [odds]);
  const latestQuotes = useMemo(
    () => [...quoteList].sort((a, b) => b.timestamp - a.timestamp).slice(0, EXECUTION_PANEL_LIMIT),
    [quoteList],
  );
  const outletContext = useMemo<CommanderOutletContext>(
    () => ({ connection: status, quoteCount: quoteList.length }),
    [status, quoteList.length],
  );

  return (
    <AppShell
      commander={commander}
      connection={status}
      error={error}
      onReconnect={reconnect}
      lastUpdatedAt={lastUpdatedAt}
      quotes={latestQuotes}
    >
      <Outlet context={outletContext} />
    </AppShell>
  );
}
