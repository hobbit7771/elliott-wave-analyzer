import type { SourceId } from '../../core/types.js';
import type { MarketAdapter } from './adapter.js';
import { BinanceFuturesAdapter, BinanceSpotAdapter } from './binance.js';
import { BybitLinearAdapter } from './bybit.js';

const adapters = new Map<SourceId, MarketAdapter>();

export function getAdapter(id: SourceId): MarketAdapter {
  let a = adapters.get(id);
  if (!a) {
    if (id === 'binance-futures') a = new BinanceFuturesAdapter();
    else if (id === 'binance-spot') a = new BinanceSpotAdapter();
    else if (id === 'bybit-linear') a = new BybitLinearAdapter();
    else throw new Error(`unknown source ${id}`);
    adapters.set(id, a);
  }
  return a;
}

export const SOURCES: SourceId[] = ['binance-futures', 'binance-spot', 'bybit-linear'];

export function isSource(s: string): s is SourceId {
  return (SOURCES as string[]).includes(s);
}

/**
 * Sources that are architecturally planned but NOT available, with the reason.
 * They are shown in the "Data sources" panel only — never as selectable instruments.
 */
export const UNAVAILABLE_SOURCES = [
  {
    id: 'databento-glbx',
    name: 'CME Globex через Databento (GLBX.MDP3: NQ, ES, GC, CL)',
    status: 'адаптер написан, не проверен на реальных данных',
    reason: 'Исторический HTTP-клиент Databento (схемы mbp-10 и trades, сторона агрессора из поля side) реализован и покрыт тестами на документированном формате записей. Реальных данных не получали: нужен ключ, а трафик Databento платный. Live-шлюз (Raw API) не реализован.',
    needs: ['DATABENTO_API_KEY в секретах Render', 'ваше отдельное разрешение на платное использование данных', 'лицензия CME на non-display/display использование при необходимости'],
  },
  {
    id: 'databento-xnas',
    name: 'Nasdaq TotalView через Databento (XNAS.ITCH)',
    status: 'адаптер написан, не проверен на реальных данных',
    reason: 'Тот же клиент Databento; для акций Nasdaq доступен L3 (MBO) и L2 (mbp-10). Не проверен: нет ключа и разрешения на платные данные.',
    needs: ['DATABENTO_API_KEY', 'разрешение на платное использование'],
  },
  {
    id: 'dxfeed',
    name: 'dxFeed (CME / фьючерсы)',
    status: 'не реализован',
    reason: 'dxFeed отдаёт стакан CME только по платной подписке с биржевыми лицензиями; бесплатного реального потока нет. Адаптер не писался, чтобы не выдавать непроверенный код за рабочий.',
    needs: ['платная подписка dxFeed и ваше разрешение'],
  },
  {
    id: 'cfd',
    name: 'CFD / OTC XAUUSD, BRXUSD',
    status: 'недоступно по природе рынка',
    reason: 'У CFD и спотового OTC-золота нет централизованного стакана: «глубина» брокера — его собственная лестница котировок, не сравнимая с биржевой. Не предлагается.',
  },
];
