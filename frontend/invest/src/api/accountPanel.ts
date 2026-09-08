import type { AccountPanelResponse } from "../types/invest";

export interface FetchAccountPanelOptions {
  signal?: AbortSignal;
}

export async function fetchAccountPanel(
  options: FetchAccountPanelOptions = {},
): Promise<AccountPanelResponse> {
  const res = await fetch("/invest/api/account-panel", {
    credentials: "include",
    signal: options.signal,
  });
  if (!res.ok) throw new Error(`account-panel ${res.status}`);
  return res.json();
}
