// Bundles the server and the ingestion worker into dist/server (ESM, node22). Dependencies stay external.
import { build, context } from 'esbuild';

const opts = {
  entryPoints: {
    'server/index': 'src/server/index.ts',
    'server/worker': 'src/server/worker.ts',
    'server/analysisWorker': 'src/server/analysisWorker.ts',
    'tools/record': 'src/tools/record.ts',
    'tools/replay': 'src/tools/replay.ts',
    'tools/verify-live': 'src/tools/verify-live.ts',
  },
  outdir: 'dist',
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node22',
  packages: 'external',
  sourcemap: true,
  logLevel: 'info',
};

if (process.argv.includes('--watch')) {
  const ctx = await context(opts);
  await ctx.watch();
} else {
  await build(opts);
}
