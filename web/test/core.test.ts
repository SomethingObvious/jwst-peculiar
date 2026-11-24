import { describe, expect, it, vi } from 'vitest';
import { Tan, decDms, icrsToGalactic, raHms } from '../src/lib/wcs';
import { TileCache } from '../src/lib/cache';
import { TileFrame } from '../src/lib/frame';
import type { MosaicInfo, TileInfo } from '../src/lib/types';

describe('Tan', () => {
  // Reference values from astropy 8.0.2 with the same keywords.
  const wcs = new Tan({
    CTYPE1: 'RA---TAN',
    CTYPE2: 'DEC--TAN',
    CRPIX1: 2274.5,
    CRPIX2: 2306.5,
    CRVAL1: 110.8375,
    CRVAL2: -73.4547,
    CD1_1: -1.6444620863753398e-5,
    CD1_2: 5.985352508199202e-6,
    CD2_1: -5.985352508199202e-6,
    CD2_2: -1.6444620863753398e-5,
  });

  it('maps pixels to sky like astropy', () => {
    const [ra, dec] = wcs.pix2world(100, 4000);
    expect(ra).toBeCloseTo(110.9988034943329, 10);
    expect(dec).toBeCloseTo(-73.46947188260971, 10);
    const [ra0, dec0] = wcs.pix2world(2274.5, 2306.5);
    expect(ra0).toBeCloseTo(110.8375, 12);
    expect(dec0).toBeCloseTo(-73.4547, 12);
  });

  it('maps sky to pixels like astropy', () => {
    const [x, y] = wcs.world2pix(110.9, -73.5);
    expect(x).toBeCloseTo(2206.8567283593407, 7);
    expect(y).toBeCloseTo(5086.385386365631, 7);
  });

  it('formats sexagesimal and galactic coordinates', () => {
    expect(raHms(110.8375)).toBe('07:23:21.00');
    expect(decDms(-73.4547, 2)).toBe('−73:27:16.92');
    expect(raHms(359.99999999)).toBe('00:00:00.00');
    // astropy goes through FK5 for galactic, which moves it by about 0.02 arcsec.
    const [l, b] = icrsToGalactic(110.8375, -73.4547);
    expect(l).toBeCloseTo(284.9938224656199, 4);
    expect(b).toBeCloseTo(-23.69871978177526, 4);
  });
});

describe('TileFrame', () => {
  const tile = { x0: 100, y0: 200, size: 10 } as TileInfo;
  const f = new TileFrame(tile, {} as MosaicInfo, {} as Tan, 10, 10);

  it("maps the top image row to the tile's highest mosaic row", () => {
    expect(f.toMosaic(0.5, 0.5)).toEqual([100, 209]);
    expect(f.toMosaic(9.5, 9.5)).toEqual([109, 200]);
  });

  it('round-trips mosaic pixels', () => {
    expect(f.fromMosaic(...f.toMosaic(3.25, 7.75))).toEqual([3.25, 7.75]);
  });
});

describe('TileCache', () => {
  it('stops the loads a step leaves behind and keeps the rest', async () => {
    const asked: [string, AbortSignal][] = [];
    vi.stubGlobal('fetch', (url: string, init: { signal: AbortSignal }) => {
      asked.push([url, init.signal]);
      // Never answers, like a slow server, and fails the way fetch does when stopped.
      return new Promise((_resolve, reject) => {
        const stop = () => {
          reject(new DOMException('stopped', 'AbortError'));
        };
        if (init.signal.aborted) {
          stop();
        }
        init.signal.addEventListener('abort', stop);
      });
    });
    const cache = new TileCache('');
    const left = cache.get(0);
    const wanted = cache.get(1);
    void cache.prefetch([2], [1, 2]);
    await expect(left).rejects.toThrow('stopped');
    const stopped = (id: string) =>
      asked.filter(([url]) => url.includes(id)).map(([, signal]) => signal.aborted);
    expect(stopped('000000')).not.toContain(false);
    expect(stopped('000001')).toEqual([false]);
    expect(stopped('000002')).toEqual([false]);
    // Coming back asks again instead of handing over the stopped load.
    expect(cache.get(0)).not.toBe(left);
    expect(cache.get(1)).toBe(wanted);
    vi.unstubAllGlobals();
  });
});
