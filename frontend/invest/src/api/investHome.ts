import type { InvestHomeResponse } from "../types/invest";

export interface FetchInvestHomeOptions {
  signal?: AbortSignal;
}

export async function fetchInvestHome(
  options: FetchInvestHomeOptions = {},
): Promise<InvestHomeResponse> {
  const res = await fetch("/invest/api/home", {
    credentials: "include",
    signal: options.signal,
  });
  if (!res.ok) {
    throw new Error(`/invest/api/home ${res.status}`);
  }
  return (await res.json()) as InvestHomeResponse;
}
