// Production code must never generate or load synthetic market data.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx|js|mjs|html|css)$/.test(f) ? [p] : [];
  });
}

describe('No fake data in production code', () => {
  const src = files('src');
  it('scans a non-trivial code base', () => {
    expect(src.length).toBeGreaterThan(20);
  });
  it('never uses Math.random or crypto random values', () => {
    for (const f of src) {
      const s = readFileSync(f, 'utf8');
      expect(s, f).not.toMatch(/Math\.random|getRandomValues|randomUUID|randomInt/);
    }
  });
  it('never imports test fixtures, mocks or fake-data libraries', () => {
    for (const f of src) {
      const s = readFileSync(f, 'utf8');
      expect(s, f).not.toMatch(/from ['"][^'"]*(tests\/|fixtures|faker|mock|chance)[^'"]*['"]/i);
      expect(s, f).not.toMatch(/\b(mockData|fakeData|dummyData|sampleData|demoData)\b/);
    }
  });
  it('has no hard-coded price series', () => {
    for (const f of src) {
      const s = readFileSync(f, 'utf8');
      // long literal arrays of decimals would be a baked-in series
      expect(s, f).not.toMatch(/\[\s*(\d+\.\d+\s*,\s*){30,}/);
    }
  });
});
