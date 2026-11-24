<script lang="ts">
  import type { TileData } from '../lib/tiles';
  import { edgeTicks, scaleBar, type TileFrame } from '../lib/frame';
  import { track } from '../lib/track';
  import type { Theme, TileInfo } from '../lib/types';

  let {
    tile,
    index,
    data,
    error,
    frame,
    theme,
    oncursor,
  }: {
    tile: TileInfo;
    index: number;
    data: TileData | null;
    error: string | null;
    frame: TileFrame | null;
    theme: Theme;
    oncursor: (c: { ix: number; iy: number } | null) => void;
  } = $props();

  const M = 22;
  let canvas: HTMLCanvasElement | undefined = $state();
  let cw = $state(0);
  let ch = $state(0);
  // Zoom relative to fit and the view centre as a fraction of the image, so both carry across tiles.
  let rel = $state(1);
  let cfx = $state(0.5);
  let cfy = $state(0.5);

  const W = $derived(data?.width ?? 1);
  const H = $derived(data?.height ?? 1);
  const fitZoom = $derived(Math.max(1e-3, Math.min(cw / W, ch / H)));
  const zoom = $derived(fitZoom * rel);
  const cx = $derived(cfx * W);
  const cy = $derived(cfy * H);

  const toScreen = (ix: number, iy: number): [number, number] => [
    (ix - cx) * zoom + cw / 2,
    (iy - cy) * zoom + ch / 2,
  ];
  const toImage = (sx: number, sy: number): [number, number] => [
    (sx - cw / 2) / zoom + cx,
    (sy - ch / 2) / zoom + cy,
  ];

  export function fit() {
    rel = 1;
    cfx = 0.5;
    cfy = 0.5;
  }

  // Zoom stays between a quarter of fit and 64 screen pixels per image pixel.
  const clampRel = (r: number) => Math.min(64 / fitZoom, Math.max(0.25, r));

  // The view centre stays on the image, so some of it is always in view.
  function centreAt(fx: number, fy: number) {
    cfx = Math.min(1, Math.max(0, fx));
    cfy = Math.min(1, Math.max(0, fy));
  }

  export function oneToOne() {
    // A hidden plate has no size to measure 1:1 against.
    if (cw > 0 && ch > 0 && data) {
      rel = clampRel(1 / fitZoom);
    }
  }

  $effect(() => {
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) {
      return;
    }
    // the backing store follows the device's pixels, so a stamp stays sharp at 1:1
    track(theme);
    const dpr = devicePixelRatio || 1;
    canvas.width = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    if (!data) {
      return;
    }
    // smoothed when shrunk, and square pixels once each one is big enough to see
    ctx.imageSmoothingEnabled = zoom < 2;
    const [x, y] = toScreen(0, 0);
    ctx.drawImage(data.bitmap, x, y, W * zoom, H * zoom);
  });

  // Furniture drawn over the canvas, kept to the part of the viewport the image covers.
  const vis = $derived.by(() => {
    const [x0, y0] = toScreen(0, 0);
    const [x1, y1] = toScreen(W, H);
    const l = Math.max(0, Math.round(x0));
    const t = Math.max(0, Math.round(y0));
    const r = Math.min(cw, Math.round(x1));
    const b = Math.min(ch, Math.round(y1));
    return data && r - l > 40 && b - t > 40
      ? { l, t, w: r - l, h: b - t }
      : { l: 0, t: 0, w: cw, h: ch };
  });
  const ticks = $derived(
    frame?.wcs && data && cw > 0
      ? edgeTicks(
          vis.w,
          vis.h,
          (x, y) => frame.toSky(...toImage(vis.l + x, vis.t + y)) ?? [NaN, NaN],
        )
      : [],
  );
  const compass = $derived(frame?.compass() ?? null);
  const bar = $derived(frame ? scaleBar(frame.arcsecPerPx / zoom) : null);
  // The same edges in the frame's coordinates, which sit M outside the viewport.
  const L = $derived(M + vis.l);
  const T = $derived(M + vis.t);
  const R = $derived(M + vis.l + vis.w);
  const B = $derived(M + vis.t + vis.h);
  const corners = $derived<[number, number, number, number][]>([
    [L - 0.5, T - 0.5, -1, -1],
    [R + 0.5, T - 0.5, 1, -1],
    [L - 0.5, B + 0.5, -1, 1],
    [R + 0.5, B + 0.5, 1, 1],
  ]);
  // Pointer: one drags to pan, two pinch, and the wheel zooms about the pointer.
  let drag: { x: number; y: number; cfx: number; cfy: number } | null = null;
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- nothing on screen reads it
  const down = new Map<number, [number, number]>();

  function local(e: { clientX: number; clientY: number }): [number, number] {
    // Only the canvas sends these events, so it is bound by then.
    const r = canvas?.getBoundingClientRect() ?? new DOMRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  /** Zooms by `f`, keeping the image point under viewport point (sx, sy) where it is. */
  function zoomAbout(sx: number, sy: number, f: number) {
    const [ix, iy] = toImage(sx, sy);
    const next = clampRel(rel * f);
    const z = fitZoom * next;
    rel = next;
    centreAt((ix - (sx - cw / 2) / z) / W, (iy - (sy - ch / 2) / z) / H);
  }

  export function zoomBy(f: number) {
    zoomAbout(cw / 2, ch / 2, f);
  }

  /** Moves the view by a share of the viewport, so (1, 0) shows what was one screen to the right. */
  export function panBy(dx: number, dy: number) {
    centreAt(cfx + (dx * cw) / zoom / W, cfy + (dy * ch) / zoom / H);
  }

  function onpointerdown(e: PointerEvent) {
    if (e.button !== 0) {
      return;
    }
    canvas?.setPointerCapture(e.pointerId);
    down.set(e.pointerId, [e.clientX, e.clientY]);
    // A second finger turns the drag into a pinch.
    drag = down.size === 1 ? { x: e.clientX, y: e.clientY, cfx, cfy } : null;
  }

  function pinch(e: PointerEvent, was: [number, number]) {
    const other = [...down].find(([id]) => id !== e.pointerId)?.[1];
    if (!other) {
      return;
    }
    // The midpoint moves half as far as the finger that moved.
    centreAt(
      cfx - (e.clientX - was[0]) / 2 / zoom / W,
      cfy - (e.clientY - was[1]) / 2 / zoom / H,
    );
    const before = Math.hypot(was[0] - other[0], was[1] - other[1]);
    const after = Math.hypot(e.clientX - other[0], e.clientY - other[1]);
    if (before > 0 && after > 0) {
      const mid = {
        clientX: (e.clientX + other[0]) / 2,
        clientY: (e.clientY + other[1]) / 2,
      };
      zoomAbout(...local(mid), after / before);
    }
  }

  function onpointermove(e: PointerEvent) {
    const was = down.get(e.pointerId);
    if (was) {
      down.set(e.pointerId, [e.clientX, e.clientY]);
    }
    if (was && down.size === 2) {
      pinch(e, was);
    } else if (drag) {
      centreAt(
        drag.cfx - (e.clientX - drag.x) / zoom / W,
        drag.cfy - (e.clientY - drag.y) / zoom / H,
      );
    }
    if (!data) {
      return;
    }
    const [ix, iy] = toImage(...local(e));
    oncursor(ix >= 0 && iy >= 0 && ix < W && iy < H ? { ix, iy } : null);
  }

  function onpointerup(e: PointerEvent) {
    down.delete(e.pointerId);
    // The finger still down after a pinch carries on as a drag from where it is.
    const [rest] = down.values();
    drag = rest && down.size === 1 ? { x: rest[0], y: rest[1], cfx, cfy } : null;
    if (canvas?.hasPointerCapture(e.pointerId)) {
      canvas.releasePointerCapture(e.pointerId);
    }
  }

  function onwheel(e: WheelEvent) {
    e.preventDefault();
    zoomAbout(...local(e), Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0018)));
  }
</script>

{#snippet part(p: { v: string; u: string }, lastPart: boolean)}
  {@const sup = p.u === 'h' || p.u === 'm' || p.u === 's'}
  <tspan>{p.v}</tspan><tspan class="u" dy={sup ? '-0.45em' : undefined}>{p.u}</tspan
  >{#if sup || !lastPart}<tspan dy={sup ? '0.45em' : undefined}
      >{lastPart ? '' : ' '}</tspan
    >{/if}
{/snippet}

<section class="plate" aria-label="Plate">
  <div class="toolbar">
    <div class="group">
      <button class="ctl" onclick={fit} title="Fit the stamp to the view (f)"
        >Fit</button
      >
      <button
        class="ctl"
        onclick={oneToOne}
        title="One image pixel per screen pixel (0)">1:1</button
      >
      <span class="zoom num" title="Screen pixels per image pixel"
        >{zoom.toFixed(2)}<span class="u">&times;</span></span
      >
    </div>
  </div>

  <div class="stage">
    <div
      class="viewport"
      style:inset="{M}px"
      bind:clientWidth={cw}
      bind:clientHeight={ch}
    >
      <canvas
        bind:this={canvas}
        class:grab={Boolean(data)}
        {onpointerdown}
        {onpointermove}
        {onpointerup}
        onpointercancel={onpointerup}
        onpointerleave={() => {
          oncursor(null);
        }}
        {onwheel}
        ondblclick={fit}
        aria-label="Object {tile.rank}, drag to pan and scroll to zoom"
      ></canvas>

      {#if !data && !error}
        <div class="skeleton" aria-live="polite">
          <span class="label">Loading Object {String(index).padStart(6, '0')}</span>
        </div>
      {/if}
      {#if error}
        <div class="failed" role="alert">
          <p>{error}</p>
        </div>
      {/if}

      <svg class="over" width={cw} height={ch} aria-hidden="true">
        {#if compass}
          <g class="compass" transform="translate({vis.l + vis.w - 44} {vis.t + 44})">
            <circle r="2" />
            <line x1="0" y1="0" x2={compass.n[0] * 26} y2={compass.n[1] * 26} />
            <line x1="0" y1="0" x2={compass.e[0] * 18} y2={compass.e[1] * 18} />
            <text x={compass.n[0] * 34} y={compass.n[1] * 34} dy="0.35em">N</text>
            <text x={compass.e[0] * 26} y={compass.e[1] * 26} dy="0.35em">E</text>
          </g>
        {/if}
        {#if bar}
          <g class="scalebar" transform="translate({vis.l + 14} {vis.t + vis.h - 16})">
            <path d="M0 -4 V0 H{bar.px} V-4" />
            <text x={bar.px / 2} y="-8">{bar.label}</text>
          </g>
        {/if}
      </svg>
    </div>

    <svg class="frame" aria-hidden="true">
      <rect x={L - 0.5} y={T - 0.5} width={vis.w + 1} height={vis.h + 1} />
      {#each ticks as t, i (i)}
        {#if t.edge === 'top' || t.edge === 'bottom'}
          {@const x = L + t.at}
          {@const y = t.edge === 'top' ? T : B}
          <line x1={x} x2={x} y1={y} y2={t.edge === 'top' ? y + 6 : y - 6} />
          <text
            class="tl"
            {x}
            y={t.edge === 'top' ? T - 7 : B + 15}
            text-anchor="middle"
          >
            {#each t.parts as p, j (j)}{@render part(
                p,
                j === t.parts.length - 1,
              )}{/each}
          </text>
        {:else}
          {@const y = T + t.at}
          {@const x = t.edge === 'left' ? L : R}
          <line x1={x} x2={t.edge === 'left' ? x + 6 : x - 6} y1={y} y2={y} />
          <text
            class="tl"
            transform="translate({t.edge === 'left'
              ? L - 8
              : R + 8} {y}) rotate({t.edge === 'left' ? -90 : 90})"
            text-anchor="middle"
          >
            {#each t.parts as p, j (j)}{@render part(
                p,
                j === t.parts.length - 1,
              )}{/each}
          </text>
        {/if}
      {/each}
      {#each corners as [x, y, sx, sy], i (i)}
        <path
          class="corner"
          d="M{x + sx * 3} {y} H{x + sx * 12} M{x} {y + sy * 3} V{y + sy * 12}"
        />
      {/each}
    </svg>
  </div>
</section>

<style>
  .plate {
    display: grid;
    grid-template-rows: auto 1fr;
    min-width: 0;
    min-height: 0;
    background: var(--ground);
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 14px;
    padding: 6px 12px;
    border-bottom: 1px solid var(--rule);
    min-height: 37px;
  }
  .group {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  @media (max-width: 820px) {
    .toolbar {
      gap: 6px;
    }
    /* On a phone every control flows in one wrap, so a group's last row isn't left half empty. */
    .group {
      display: contents;
    }
  }
  .zoom {
    color: var(--anno);
    min-width: 46px;
  }
  .zoom .u {
    color: var(--chalk-3);
    margin-left: 1px;
  }
  .stage {
    position: relative;
    min-height: 0;
    overflow: hidden;
  }
  /* the Plate theme reads stamps as negatives, as plates were read on a light table */
  :global([data-theme='plate']) canvas {
    filter: grayscale(1) invert(1);
  }
  .viewport {
    position: absolute;
    overflow: hidden;
    background: var(--ground-sunk);
  }
  canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
    touch-action: none;
    cursor: crosshair;
  }
  canvas.grab:active {
    cursor: grabbing;
  }
  .skeleton,
  .failed {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    pointer-events: none;
    background: var(--ground-sunk);
  }
  .skeleton .label {
    color: var(--anno);
    background: var(--ground-sunk);
    padding: 4px 8px;
  }
  .failed p {
    max-width: 46ch;
    background: var(--ground-sunk);
    padding: 10px 14px;
    border: 1px solid var(--rule-strong);
    color: var(--chalk);
  }
  .over {
    position: absolute;
    inset: 0;
    pointer-events: none;
    overflow: visible;
  }
  .compass line {
    stroke: var(--anno);
    stroke-width: 1.25;
  }
  .compass circle {
    fill: var(--anno);
  }
  .compass text,
  .scalebar text {
    fill: var(--anno);
    font-family: var(--font-num);
    font-variant-numeric: slashed-zero;
    font-size: 11px;
    text-anchor: middle;
    paint-order: stroke;
    stroke: var(--ground-sunk);
    stroke-width: 3px;
    stroke-linejoin: round;
  }
  .scalebar path {
    fill: none;
    stroke: var(--anno);
    stroke-width: 1.25;
  }
  .frame {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }
  .frame rect {
    fill: none;
    stroke: var(--rule-strong);
    stroke-width: 1;
  }
  .frame line,
  .corner {
    stroke: var(--anno);
    stroke-width: 1;
  }
  .tl {
    fill: var(--anno);
    font-family: var(--font-num);
    font-size: 11px;
    font-variant-numeric: tabular-nums slashed-zero;
  }
  .tl .u {
    font-size: 8px;
    fill: var(--anno-2);
  }
</style>
