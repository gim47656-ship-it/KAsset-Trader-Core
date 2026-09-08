import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { RightRemotePanel } from "../desktop/RightRemotePanel";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import * as panelApi from "../api/accountPanel";
import * as signalsApi from "../api/signals";
import type { AccountPanelResponse } from "../types/invest";

const PANEL_RESP: AccountPanelResponse = {
  homeSummary: {
    includedSources: ["kis", "upbit", "toss_manual"],
    excludedSources: [],
    totalValueKrw: 10_100_000,
    costBasisKrw: 9_200_000,
    pnlKrw: 900_000,
    pnlRate: 900_000 / 9_200_000,
  },
  accounts: [
    {
      accountId: "k1",
      displayName: "KIS Live",
      source: "kis",
      accountKind: "live",
      includedInHome: true,
      valueKrw: 1_244_000,
      cashBalances: { krw: 100_000, usd: 25.5 },
      buyingPower: { krw: 100_000, usd: 25.5 },
    },
    {
      accountId: "u1",
      displayName: "Upbit",
      source: "upbit",
      accountKind: "live",
      includedInHome: true,
      valueKrw: 8_500_000,
      cashBalances: { krw: 50_000 },
      buyingPower: { krw: 50_000 },
    },
  ],
  groupedHoldings: [
    {
      groupId: "US:equity:USD:TSLA",
      symbol: "TSLA",
      market: "US",
      assetType: "equity",
      assetCategory: "us_stock",
      displayName: "Tesla",
      currency: "USD",
      totalQuantity: 6,
      averageCost: 200,
      costBasis: 1200,
      valueNative: 1200,
      valueKrw: 1_600_000,
      pnlKrw: 0,
      pnlRate: 0,
      priceState: "live",
      includedSources: ["kis", "toss_manual"],
      sourceBreakdown: [
        {
          holdingId: "h1",
          accountId: "k1",
          source: "kis",
          quantity: 4,
          averageCost: 234,
          costBasis: 936,
          valueNative: 924,
          valueKrw: 1_244_000,
          pnlKrw: -16_000,
          pnlRate: -16_000 / 936,
        },
        {
          holdingId: "h2",
          accountId: "manual-1",
          source: "toss_manual",
          quantity: 2,
          averageCost: 132,
          costBasis: 264,
          valueNative: 276,
          valueKrw: 356_000,
          pnlKrw: 16_000,
          pnlRate: 16_000 / 264,
        },
      ],
    },
    {
      groupId: "CRYPTO:crypto:KRW:BTC",
      symbol: "KRW-BTC",
      market: "CRYPTO",
      assetType: "crypto",
      assetCategory: "crypto",
      displayName: "비트코인",
      currency: "KRW",
      totalQuantity: 0.1,
      averageCost: 80_000_000,
      costBasis: 8_000_000,
      valueNative: 8_500_000,
      valueKrw: 8_500_000,
      pnlKrw: 500_000,
      pnlRate: 0.0625,
      priceState: "live",
      includedSources: ["upbit"],
      sourceBreakdown: [],
    },
  ],
  watchSymbols: [
    { symbol: "AAPL", market: "us", displayName: "Apple Inc." },
  ],
  sourceVisuals: [
    { source: "kis", tone: "navy", badge: "Live", displayName: "KIS" },
    { source: "upbit", tone: "green", badge: "Crypto", displayName: "Upbit" },
    { source: "toss_manual", tone: "gray", badge: "Manual", displayName: "Toss/manual" },
    { source: "kis_mock", tone: "dashed", badge: "Mock", displayName: "KIS mock" },
    { source: "alpaca_paper", tone: "dashed", badge: "Paper", displayName: "Alpaca" },
  ],
  meta: { warnings: [], watchlistAvailable: true },
};

function renderPanel() {
  return render(
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/"]}>
        <RightRemotePanel />
        <LocationProbe />
      </MemoryRouter>
    </AccountPanelProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe" data-path={`${location.pathname}${location.search}`} />;
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(PANEL_RESP);
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr",
    asOf: new Date().toISOString(),
    items: [],
    meta: { warnings: [] },
  });
  localStorage.clear();
});

test("renders the tabbed right remote panel", async () => {
  renderPanel();
  expect(screen.getByTestId("right-remote-panel")).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "내 투자" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "관심" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "최근 본" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "실시간" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
});

test("portfolio tab shows account cash card, filters, and all-account holdings after data loads", async () => {
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
  const cashCard = screen.getByTestId("account-cash-card");
  expect(within(cashCard).getByText("전체")).toBeInTheDocument();
  expect(within(cashCard).getByText("₩150,000")).toBeInTheDocument();
  expect(within(cashCard).getByText("$25.5")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "전체" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "KIS 실계좌" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Upbit" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Toss 수동" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "KIS 모의" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Alpaca Paper" })).not.toBeInTheDocument();
  expect(screen.getByText("전체 보유종목")).toBeInTheDocument();
  expect(screen.getByText("비트코인")).toBeInTheDocument();
  expect(screen.getByText("Tesla")).toBeInTheDocument();
  expect(screen.getByText("₩10,100,000")).toBeInTheDocument();
});

test("KIS filter recomputes totals and rows without refetching account panel", async () => {
  const user = userEvent.setup();
  const fetchSpy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(PANEL_RESP);
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
  expect(fetchSpy).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "KIS 실계좌" }));

  expect(screen.getByRole("button", { name: "KIS 실계좌" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByText("KIS 실계좌 보유종목")).toBeInTheDocument();
  expect(screen.getByText("Tesla")).toBeInTheDocument();
  expect(screen.queryByText("비트코인")).not.toBeInTheDocument();
  expect(screen.getAllByText("₩1,244,000")).toHaveLength(2);
  expect(screen.getByText((_, node) => node?.textContent === "투자원금 ₩1,260,000")).toBeInTheDocument();
  expect(screen.getByTestId("pl")).toHaveAttribute("data-dir", "down");
  const cashCard = screen.getByTestId("account-cash-card");
  expect(within(cashCard).getByText("₩100,000")).toBeInTheDocument();
  expect(within(cashCard).getByText("$25.5")).toBeInTheDocument();
  expect(fetchSpy).toHaveBeenCalledTimes(1);
});

test("Upbit and Toss/manual filters scope holdings and cash independently", async () => {
  const user = userEvent.setup();
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());

  await user.click(screen.getByRole("button", { name: "Upbit" }));
  expect(screen.getByText("Upbit 보유종목")).toBeInTheDocument();
  expect(screen.getByText("비트코인")).toBeInTheDocument();
  expect(screen.queryByText("Tesla")).not.toBeInTheDocument();
  expect(screen.getAllByText("₩8,500,000")).toHaveLength(2);
  expect(screen.getByText((_, node) => node?.textContent === "투자원금 ₩8,000,000")).toBeInTheDocument();
  let cashCard = screen.getByTestId("account-cash-card");
  expect(within(cashCard).getByText("₩50,000")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Toss 수동" }));
  expect(screen.getByText("Toss 수동 보유종목")).toBeInTheDocument();
  expect(screen.getByText("Tesla")).toBeInTheDocument();
  expect(screen.queryByText("비트코인")).not.toBeInTheDocument();
  expect(screen.getAllByText("₩356,000")).toHaveLength(2);
  cashCard = screen.getByTestId("account-cash-card");
  expect(within(cashCard).getByText("현금 정보 없음")).toBeInTheDocument();
});

test("watchlist tab shows watch symbols", async () => {
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
  await userEvent.click(screen.getByRole("tab", { name: "관심" }));
  expect(screen.getByTestId("watchlist-panel")).toBeInTheDocument();
  expect(screen.getByText("Apple Inc.")).toBeInTheDocument();
});

test("portfolio and recent symbol clicks navigate to stock detail pages", async () => {
  const user = userEvent.setup();
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());

  await user.click(screen.getByRole("button", { name: /Tesla/ }));
  expect(screen.getByTestId("location-probe")).toHaveAttribute("data-path", "/stocks/us/TSLA");

  await user.click(screen.getByRole("tab", { name: "최근 본" }));
  await user.click(screen.getByRole("button", { name: /Tesla/ }));
  expect(screen.getByTestId("location-probe")).toHaveAttribute("data-path", "/stocks/us/TSLA");
});

test("recent tab shows empty state initially", async () => {
  renderPanel();
  await userEvent.click(screen.getByRole("tab", { name: "최근 본" }));
  expect(screen.getByTestId("recent-panel-empty")).toBeInTheDocument();
});

test("portfolio tab shows empty holdings gracefully", async () => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    ...PANEL_RESP,
    homeSummary: { ...PANEL_RESP.homeSummary, totalValueKrw: 0, costBasisKrw: null, pnlKrw: null, pnlRate: null },
    groupedHoldings: [],
  });
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("holdings-empty")).toBeInTheDocument());
});

test("does not render order CTA buttons", async () => {
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "매수" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "매도" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /주문/ })).not.toBeInTheDocument();
});

test("현재 계좌 패널은 제거된 provider 칩을 노출하지 않고 mount 당 1회만 조회한다", async () => {
  const fetchSpy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(PANEL_RESP);
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
  expect(fetchSpy).toHaveBeenCalledTimes(1);
  const filters = screen.getByLabelText("계좌 필터");
  expect(within(filters).queryByRole("button", { name: "KIS 모의" })).not.toBeInTheDocument();
  expect(within(filters).queryByRole("button", { name: "Kiwoom 모의" })).not.toBeInTheDocument();
  expect(within(filters).queryByRole("button", { name: "Alpaca Paper" })).not.toBeInTheDocument();
  expect(within(filters).queryByRole("button", { name: "DB Paper" })).not.toBeInTheDocument();
});

test("watchlist tab lazy-loads account panel when it is the stored initial tab", async () => {
  const fetchSpy = vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(PANEL_RESP);
  localStorage.setItem("invest:right-rail-tab", "watchlist");

  renderPanel();

  await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByTestId("watchlist-panel")).toBeInTheDocument());
  expect(screen.getByText("Apple Inc.")).toBeInTheDocument();
});

test("crypto portfolio clicks canonicalize bare Upbit base symbols to KRW detail routes", async () => {
  const user = userEvent.setup();
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    ...PANEL_RESP,
    groupedHoldings: PANEL_RESP.groupedHoldings.map((holding) =>
      holding.market === "CRYPTO"
        ? {
            ...holding,
            groupId: "CRYPTO:crypto:KRW:BTC",
            symbol: "BTC",
            displayName: "비트코인",
          }
        : holding,
    ),
  });

  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());

  await user.click(screen.getByRole("button", { name: /비트코인/ }));

  expect(screen.getByTestId("location-probe")).toHaveAttribute("data-path", "/stocks/crypto/KRW-BTC");
});
