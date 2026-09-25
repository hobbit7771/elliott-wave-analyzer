import type { SourceId } from '../../core/types.js';
import type { MarketAdapter } from './adapter.js';
import { BinanceFuturesAdapter, BinanceSpotAdapter } from './binance.js';

const adapters = new Map<SourceId, MarketAdapter>();

export function getAdapter(id: SourceId): MarketAdapter {
  let a = adapters.get(id);
  if (!a) {
    if (id === 'binance-futures') a = new BinanceFuturesAdapter();
    else if (id === 'binance-spot') a = new BinanceSpotAdapter();
    else throw new Error(`unknown source ${id}`);
    adapters.set(id, a);
  }
  return a;
}

export const SOURCES: SourceId[] = ['binance-futures', 'binance-spot'];

export function isSource(s: string): s is SourceId {
  return (SOURCES as string[]).includes(s);
}

/**
 * Sources that are architecturally planned but NOT available, with the reason.
 * They are shown in the "Data sources" panel only — never as selectable instruments.
 */
export const UNAVAILABLE_SOURCES = [
  {
    id: 'bybit',
    name: 'Bybit (linear perpetuals)',
    reason: 'Adapter interface ready (MarketAdapter); not implemented in this release. Public orderbook.200 + publicTrade streams are free and would fit.',
  },
  {
    id: 'cme',
    name: 'CME futures (NQ, ES, GC, CL)',
    reason: 'A real CME depth-of-market feed requires a licensed paid data feed (e.g. Rithmic, CQG, dxFeed, Databento). No free public WebSocket exists, so it is not offered.',
  },
  {
    id: 'cfd',
    name: 'CFD / OTC XAUUSD, BRXUSD',
    reason: 'CFD and spot-FX/OTC metals have no centralized order book; broker "depth" is the broker\'s own quote ladder and is not comparable to exchange depth. Not offered.',
  },
];
