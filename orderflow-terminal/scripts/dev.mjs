// Dev: rebuild server on change + restart it, and run the Vite dev server (proxying /api and /ws).
import { spawn } from 'node:child_process';
import { context } from 'esbuild';

let child = null;
const restart = () => {
  if (child) child.kill();
  child = spawn(process.execPath, ['--disable-warning=ExperimentalWarning', '--enable-source-maps', 'dist/server/index.js'], {
    stdio: 'inherit',
    env: { ...process.env, PORT: process.env.PORT ?? '8080' },
  });
};
const ctx = await context({
  entryPoints: { index: 'src/server/index.ts', worker: 'src/server/worker.ts' },
  outdir: 'dist/server',
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node22',
  packages: 'external',
  sourcemap: true,
  plugins: [{ name: 'restart', setup(b) { b.onEnd((r) => { if (!r.errors.length) restart(); }); } }],
});
await ctx.watch();
spawn('npx', ['vite'], { stdio: 'inherit', shell: true });
process.on('SIGINT', () => { child?.kill(); process.exit(0); });
