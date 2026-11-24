import { tileId } from './types';

export interface TileData {
  index: number;
  width: number;
  height: number;
  bitmap: ImageBitmap;
}

/** Fetches and decodes one stamp. */
export async function loadTile(
  base: string,
  index: number,
  signal?: AbortSignal,
): Promise<TileData> {
  const url = `${base}tiles/${tileId(index)}.png`;
  const res = await fetch(url, { signal: signal ?? null });
  if (!res.ok) {
    throw new Error(`${url} returned ${String(res.status)}`);
  }
  const bitmap = await createImageBitmap(await res.blob());
  return { index, width: bitmap.width, height: bitmap.height, bitmap };
}
