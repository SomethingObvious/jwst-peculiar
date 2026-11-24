import { loadTile, type TileData } from './tiles';

/**
 * Keeps decoded stamps around the current one. A load that is already running is
 * shared, so stepping onto a stamp being prefetched just waits for that fetch. A load
 * nobody is waiting for any more is stopped, as holding a key down passes stamps far
 * faster than they arrive.
 */
export class TileCache {
  private done = new Map<number, TileData>();
  private running = new Map<number, { p: Promise<TileData>; stop: AbortController }>();
  private keep = new Set<number>();
  private turn = 0;
  private readonly base: string;
  private readonly capacity: number;

  constructor(base: string, capacity = 30) {
    this.base = base;
    this.capacity = capacity;
  }

  peek(index: number): TileData | undefined {
    const d = this.done.get(index);
    if (d) {
      // Map keeps insertion order, so re-inserting marks it as recently used.
      this.done.delete(index);
      this.done.set(index, d);
    }
    return d;
  }

  get(index: number): Promise<TileData> {
    const d = this.peek(index);
    if (d) {
      return Promise.resolve(d);
    }
    const now = this.running.get(index);
    if (now) {
      return now.p;
    }
    const stop = new AbortController();
    // A stopped load is taken off the list at once, so only the one still listed clears it.
    const finish = () => {
      if (this.running.get(index)?.stop === stop) {
        this.running.delete(index);
      }
    };
    const p = loadTile(this.base, index, stop.signal).then(
      (data) => {
        finish();
        this.done.set(index, data);
        this.trim(index);
        return data;
      },
      (err: unknown) => {
        finish();
        throw err;
      },
    );
    this.running.set(index, { p, stop });
    return p;
  }

  /** Loads these in order, one after another, so the next stamp is never queued behind a far one. */
  async prefetch(indices: number[], keep: number[]): Promise<void> {
    this.keep = new Set(keep);
    this.turn += 1;
    const { turn } = this;
    for (const [i, load] of this.running) {
      if (!this.keep.has(i)) {
        this.running.delete(i);
        load.stop.abort();
      }
    }
    for (const i of indices) {
      // A later call has its own list, and this one's stamps may be far behind by now.
      if (turn !== this.turn) {
        return;
      }
      if (this.done.has(i)) {
        continue;
      }
      try {
        // eslint-disable-next-line no-await-in-loop -- one at a time is the point
        await this.get(i);
      } catch {
        // The plate shows the error if this stamp is ever opened.
      }
    }
  }

  private trim(justLoaded: number) {
    for (const k of this.done.keys()) {
      if (this.done.size <= this.capacity) {
        break;
      }
      if (k !== justLoaded && !this.keep.has(k)) {
        this.done.delete(k);
      }
    }
  }
}
