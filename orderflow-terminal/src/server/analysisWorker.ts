// Worker thread for the historical level/setup analysis (walk-forward backtest). It runs the same
// LevelSetupEngine used live, off the main thread so the live feed and the WebSocket clients never wait.
import { parentPort } from 'node:worker_threads';
import type { Candle } from '../core/types.js';
import { walkForward, stats, groupStats, scoreBucket } from '../core/levelEngine/backtest.js';
import { DEFAULT_SETUP_PARAMS } from '../core/levelEngine/setupEngine.js';

export interface AnalysisRequest {
  id: number;
  meta: { symbol: string; exchange: string; tick: number };
  daily: Candle[];
  bars: Candle[];
}

parentPort!.on('message', (req: AnalysisRequest) => {
  const t0 = Date.now();
  try {
    const first = req.bars[0]?.t ?? 0;
    const dailyBefore = req.daily.filter((d) => d.t + 86_400_000 <= first);
    const wf = walkForward(req.meta, dailyBefore, req.bars, DEFAULT_SETUP_PARAMS);
    const setups = wf.setups;
    parentPort!.postMessage({
      id: req.id,
      ok: true,
      result: {
        symbol: req.meta.symbol,
        computedAt: Date.now(),
        ms: Date.now() - t0,
        coverage: { from: first, to: req.bars[req.bars.length - 1]?.t ?? 0, bars: req.bars.length, days: dailyBefore.length },
        params: wf.params,
        chosenBy: wf.chosenBy,
        grid: wf.grid,
        segments: wf.segments,
        overall: stats(setups),
        byDirection: groupStats(setups, (s) => s.direction),
        byScore: groupStats(setups, scoreBucket),
        byRegime: groupStats(setups, (s) => s.regime),
        setups,
      },
    });
  } catch (e) {
    parentPort!.postMessage({ id: req.id, ok: false, error: (e as Error).message });
  }
});
