import type { WcsCards } from './wcs';

export interface RunInfo {
  name: string;
  /** What verdicts are filed under when `--name` gave the list a name for show. */
  id?: string;
  created: string;
}

export interface MosaicInfo {
  shape: [number, number];
  preview: string;
  preview_scale: number;
  filters: string[];
  pixel_scale_arcsec: number;
  /** Left out when the mosaic's header has no celestial WCS. */
  wcs?: WcsCards;
  header: Record<string, string>;
}

export interface TileInfo {
  key: string;
  rank: number;
  /** Null when the object had nothing left to compare against. */
  score: number | null;
  mosaic: string;
  x0: number;
  y0: number;
  size: number;
  ra: number;
  dec: number;
}

export interface Bundle {
  version: number;
  writable: boolean;
  run: RunInfo;
  mosaics: Record<string, MosaicInfo>;
  tiles: TileInfo[];
}

export type Verdict = 'interesting' | 'artifact' | 'known' | 'not_anomalous';

export interface VerdictRecord {
  verdict: Verdict | null;
  star: boolean;
  note: string;
  updated: string;
}

export const VERDICTS: { id: Verdict; label: string; key: string }[] = [
  { id: 'interesting', label: 'Interesting', key: '1' },
  { id: 'artifact', label: 'Artifact', key: '2' },
  { id: 'known', label: 'Known Object', key: '3' },
  { id: 'not_anomalous', label: 'Not Anomalous', key: '4' },
];

export type Theme = 'atlas' | 'plate' | 'classic';

export type VerdictFilter =
  | 'all'
  | 'unreviewed'
  | 'interesting'
  | 'artifact'
  | 'known'
  | 'not_anomalous'
  | 'starred';
export interface Filters {
  text: string;
  verdict: VerdictFilter;
  mosaic: string;
}

export function tileId(index: number): string {
  return String(index).padStart(6, '0');
}

/** Drops the program and visit tokens and puts the observation last, so the instrument and filter read first. */
export function shortMosaic(name: string): string {
  const parts = name.split(/[-_]/u).filter((t) => !/^(?:jw\d+|t\d+|clear)$/iu.test(t));
  const obs = parts.filter((t) => /^o\d+$/iu.test(t));
  return [...parts.filter((t) => !obs.includes(t)), ...obs].join(' ');
}
