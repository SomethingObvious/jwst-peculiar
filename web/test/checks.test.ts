import { describe, expect, it } from 'vitest';
import { parseRank, runBase } from '../src/lib/checks';
import { TileFrame } from '../src/lib/frame';
import type { MosaicInfo, TileInfo } from '../src/lib/types';
import { tanOf } from '../src/lib/wcs';

describe('parseRank', () => {
  it('takes a whole number with spaces around it', () => {
    expect(parseRank('12')).toBe(12);
    expect(parseRank(' 7 ')).toBe(7);
  });

  it('refuses anything else', () => {
    expect(parseRank('12abc')).toBeNull();
    expect(parseRank('1.5')).toBeNull();
    expect(parseRank('-3')).toBeNull();
    expect(parseRank('')).toBeNull();
  });
});

describe('runBase', () => {
  const here = 'https://example.org/viewer/index.html';

  it('takes a folder on this site and adds the slash', () => {
    expect(runBase(null, here)).toBe('./run/');
    expect(runBase('runs/a', here)).toBe('runs/a/');
    expect(runBase('https://example.org/other/', here)).toBe(
      'https://example.org/other/',
    );
  });

  it('takes a server on this computer', () => {
    expect(runBase('http://localhost:8765/run', here)).toBe(
      'http://localhost:8765/run/',
    );
    expect(runBase('http://127.0.0.1:8765/run/', here)).toBe(
      'http://127.0.0.1:8765/run/',
    );
  });

  it('refuses another site', () => {
    expect(runBase('https://evil.example/run/', here)).toBeNull();
    expect(runBase('//evil.example/run/', here)).toBeNull();
    expect(runBase('file:///C:/run/', here)).toBeNull();
  });
});

describe('a mosaic without a WCS', () => {
  const tile = { x0: 100, y0: 200, size: 10 } as TileInfo;

  it('has no Tan when the cards are missing or not TAN', () => {
    expect(tanOf(undefined)).toBeNull();
    expect(tanOf({ CTYPE1: 'RA---SIN', CTYPE2: 'DEC--SIN' })).toBeNull();
  });

  it('keeps its mosaic pixels but has no sky or compass', () => {
    const f = new TileFrame(tile, {} as MosaicInfo, null, 10, 10);
    expect(f.toMosaic(0.5, 0.5)).toEqual([100, 209]);
    expect(f.toSky(0.5, 0.5)).toBeNull();
    expect(f.compass()).toBeNull();
  });
});
