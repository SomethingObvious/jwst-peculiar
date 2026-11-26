<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import IndexList from './components/IndexList.svelte';
  import Plate from './components/Plate.svelte';
  import Readout from './components/Readout.svelte';
  import { TileFrame } from './lib/frame';
  import { TileCache } from './lib/cache';
  import type { TileData } from './lib/tiles';
  import {
    THEMES,
    VERDICTS,
    type Bundle,
    type Filters,
    type Theme,
    type Verdict,
    type VerdictRecord,
  } from './lib/types';
  import { Verdicts } from './lib/verdicts.svelte';
  import { tanOf, type Tan } from './lib/wcs';
  import { parseRank, runBase } from './lib/checks';

  const params = new URLSearchParams(location.search);
  const asked = runBase(params.get('run'), location.href);
  const base = asked ?? './run/';

  const PREFS = 'jwst-peculiar:prefs';
  const saved = (() => {
    try {
      return JSON.parse(localStorage.getItem(PREFS) ?? '{}') as {
        theme?: Theme;
        advance?: boolean;
      };
    } catch {
      return {};
    }
  })();

  let bundle = $state.raw<Bundle | null>(null);
  let loadError = $state<string | null>(null);
  let verdicts = $state.raw<Verdicts | null>(null);
  let cache: TileCache | null = null;
  let wcs = new Map<string, Tan | null>();

  let theme = $state<Theme>(
    saved.theme && THEMES.some((t) => t.toLowerCase() === saved.theme)
      ? saved.theme
      : 'atlas',
  );
  let advance = $state(saved.advance ?? true);
  let filters = $state<Filters>({
    text: '',
    verdict: 'all',
    mosaic: '',
  });
  let current = $state(0);
  const tile = $derived(bundle?.tiles[current]);
  let data = $state.raw<TileData | null>(null);
  let tileError = $state<string | null>(null);
  let cursor = $state<{ ix: number; iy: number } | null>(null);
  let lastPos = 0;

  let plate:
    | {
        fit: () => void;
        oneToOne: () => void;
        zoomBy: (f: number) => void;
        panBy: (dx: number, dy: number) => void;
      }
    | undefined = $state();
  let filterInput: HTMLInputElement | undefined = $state();
  let noteEl: HTMLTextAreaElement | undefined = $state();
  let sheet: HTMLDialogElement | undefined = $state();
  let gotoEl: HTMLInputElement | undefined = $state();
  let goto = $state<string | null>(null);
  let gotoMsg = $state('');
  let narrow = $state(false);
  let tab = $state<'plate' | 'index' | 'readout'>('plate');

  // Before the other effects, as the plate and the locator read their colours from
  // the theme's CSS when it changes.
  $effect.pre(() => {
    document.documentElement.dataset['theme'] = theme;
  });

  $effect(() => {
    const snapshot = JSON.stringify({ theme, advance });
    try {
      localStorage.setItem(PREFS, snapshot);
    } catch {
      // Prefs are a convenience, so a blocked store just means defaults next time.
    }
  });

  /** The tile the address names with #rank=N, or -1. */
  function hashTile(b: Bundle): number {
    const rank = /rank=(?<rank>\d+)/u.exec(location.hash)?.groups?.['rank'];
    return rank === undefined ? -1 : b.tiles.findIndex((t) => t.rank === Number(rank));
  }

  async function load() {
    try {
      if (asked === null) {
        throw new Error(
          `?run= points at ${params.get('run') ?? ''}, which is on another site. The viewer opens runs from this site or from localhost, so copy the run folder here or serve it locally.`,
        );
      }
      const res = await fetch(`${base}index.json`, { cache: 'no-store' });
      if (!res.ok) {
        throw new Error(`${base}index.json returned ${String(res.status)}`);
      }
      const b = (await res.json()) as Bundle;
      if (b.version !== 1) {
        throw new Error(
          `index.json is bundle version ${String(b.version)}, and this viewer reads version 1`,
        );
      }
      // The file comes from outside, so its shape is checked rather than trusted.
      if (!Array.isArray(b.tiles) || b.tiles.length === 0) {
        throw new Error('index.json lists no objects');
      }
      wcs = new Map(Object.entries(b.mosaics).map(([k, m]) => [k, tanOf(m.wcs)]));
      const v = new Verdicts(b, base);
      await v.load();
      verdicts = v;
      cache = new TileCache(base);
      current = Math.max(0, hashTile(b));
      bundle = b;
      document.title = `${b.run.name} · jwst-peculiar`;
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    }
  }

  onMount(() => {
    const mq = matchMedia('(max-width: 820px)');
    narrow = mq.matches;
    const onmq = () => (narrow = mq.matches);
    mq.addEventListener('change', onmq);
    void load();
    const unload = (e: BeforeUnloadEvent) => {
      if (verdicts?.hasUnsaved()) {
        e.preventDefault();
      }
    };
    addEventListener('beforeunload', unload);
    // A link to another rank pasted into this tab changes the hash without a reload.
    const onhash = () => {
      const at = bundle ? hashTile(bundle) : -1;
      if (at >= 0) {
        current = at;
      }
    };
    addEventListener('hashchange', onhash);
    return () => {
      mq.removeEventListener('change', onmq);
      removeEventListener('beforeunload', unload);
      removeEventListener('hashchange', onhash);
    };
  });

  const lowered = $derived(
    bundle ? bundle.tiles.map((t) => `${t.key} ${t.mosaic}`.toLowerCase()) : [],
  );

  const indexOfKey = $derived(new Map(bundle?.tiles.map((t, i) => [t.key, i]) ?? []));

  function passesReview(
    f: Filters,
    q: string,
    text: string,
    v: VerdictRecord | undefined,
  ): boolean {
    if (f.verdict === 'unreviewed' && v?.verdict) {
      return false;
    }
    if (f.verdict === 'starred' && !v?.star) {
      return false;
    }
    if (
      f.verdict !== 'all' &&
      f.verdict !== 'unreviewed' &&
      f.verdict !== 'starred' &&
      v?.verdict !== f.verdict
    ) {
      return false;
    }
    return !q || text.includes(q) || (v?.note ?? '').toLowerCase().includes(q);
  }

  const order = $derived.by(() => {
    if (!bundle || !verdicts) {
      return new Int32Array(0);
    }
    const f = filters;
    const q = f.text.trim().toLowerCase();
    const out: number[] = [];
    const tiles = bundle.tiles;
    const map = verdicts.map;
    tiles.forEach((t, i) => {
      if (f.mosaic && t.mosaic !== f.mosaic) {
        return;
      }
      // The verdict is only looked up when a filter needs it.
      if (
        (f.verdict !== 'all' || q) &&
        !passesReview(f, q, lowered[i] ?? '', map.get(t.key))
      ) {
        return;
      }
      out.push(i);
    });
    return Int32Array.from(out);
  });

  const posOf = $derived.by(() => {
    const p = new Int32Array(bundle?.tiles.length ?? 0).fill(-1);
    order.forEach((i, k) => (p[i] = k));
    return p;
  });
  const pos = $derived(posOf[current] ?? -1);
  $effect(() => {
    if (pos >= 0) {
      lastPos = pos;
    }
  });

  const reviewed = $derived(verdicts?.reviewedCount() ?? 0);

  $effect(() => {
    const i = current;
    // bundle comes first so the effect always tracks it. cache isn't reactive, and
    // bailing on it alone would leave the effect waiting for current to change,
    // which it never does when the first tile is the one asked for.
    if (!bundle || !cache) {
      return;
    }
    const c = cache;
    cursor = null;
    const hit = c.peek(i);
    tileError = null;
    data = hit ?? null;
    if (!hit) {
      c.get(i).then(
        (d) => {
          if (current === i) {
            data = d;
          }
        },
        (err: unknown) => {
          // Only a load that was left behind gets stopped, and coming back starts another.
          const stopped = err instanceof DOMException && err.name === 'AbortError';
          if (current === i && !stopped) {
            const rank = bundle?.tiles[i]?.rank ?? i + 1;
            tileError = `Couldn't load the object at rank ${String(rank)}. ${err instanceof Error ? err.message : String(err)}. Step away and back to try again.`;
          }
        },
      );
    }
    const p = untrack(() => (pos >= 0 ? pos : lastPos));
    const ord = untrack(() => order);
    const ahead = [1, 2, 3, -1].flatMap((d) => {
      const x = ord[p + d];
      return x !== undefined && x !== i ? [x] : [];
    });
    void c.prefetch(ahead, [i, ...ahead]);
    const t = bundle.tiles[i];
    if (t) {
      try {
        history.replaceState(
          null,
          '',
          `${location.pathname}${location.search}#rank=${String(t.rank)}`,
        );
      } catch {
        // Safari refuses more than 100 of these in 10 seconds, which holding J reaches.
      }
    }
  });

  const frame = $derived.by(() => {
    if (!bundle || !data || !tile) {
      return null;
    }
    const m = bundle.mosaics[tile.mosaic];
    return m
      ? new TileFrame(tile, m, wcs.get(tile.mosaic) ?? null, data.width, data.height)
      : null;
  });

  function select(i: number) {
    if (bundle && i >= 0 && i < bundle.tiles.length) {
      current = i;
    }
  }

  function selectAt(k: number) {
    const i = order[k];
    if (i !== undefined) {
      select(i);
    }
  }

  function step(d: number) {
    if (!order.length) {
      return;
    }
    let p = pos;
    if (p < 0) {
      p = d > 0 ? lastPos - 1 : lastPos;
    }
    selectAt(Math.max(0, Math.min(order.length - 1, p + d)));
  }

  function neighbour(): number | null {
    const p = pos >= 0 ? pos + 1 : lastPos;
    return order[p] ?? null;
  }

  function setVerdict(v: Verdict | null) {
    if (!verdicts || !tile) {
      return;
    }
    const next = neighbour();
    verdicts.update(tile.key, { verdict: v });
    if (advance && v && next !== null) {
      select(next);
    }
  }

  function toggleStar() {
    if (!verdicts || !tile) {
      return;
    }
    const key = tile.key;
    verdicts.update(key, { star: !verdicts.get(key)?.star });
  }

  function jump() {
    if (!bundle || goto === null) {
      return;
    }
    const r = parseRank(goto);
    const i = r === null ? -1 : bundle.tiles.findIndex((t) => t.rank === r);
    const ranks = bundle.tiles.map((t) => t.rank);
    if (i >= 0) {
      select(i);
      goto = null;
    } else if (r === null) {
      gotoMsg = 'Type a whole number, like 12.';
    } else {
      gotoMsg = `No object has rank ${String(r)}. The lowest is ${String(Math.min(...ranks))} and the highest ${String(Math.max(...ranks))}.`;
    }
  }

  // Keys that move through the list or open an input. Returns false for any other key.
  function moveKey(k: string): boolean {
    if (k === 'j' || k === 'ArrowDown' || k === 'ArrowRight') {
      step(1);
    } else if (k === 'k' || k === 'ArrowUp' || k === 'ArrowLeft') {
      step(-1);
    } else if (k === 'Home') {
      selectAt(0);
    } else if (k === 'End') {
      selectAt(order.length - 1);
    } else if (k === 'PageDown') {
      step(10);
    } else if (k === 'PageUp') {
      step(-10);
    } else if (k === 'g') {
      goto = '';
      gotoMsg = '';
      queueMicrotask(() => gotoEl?.focus());
    } else if (k === '/') {
      if (narrow) {
        tab = 'index';
      }
      queueMicrotask(() => filterInput?.focus());
    } else {
      return false;
    }
    return true;
  }

  // Keys that change the view or the verdict. Returns false for any other key.
  function actKey(k: string): boolean {
    const verdict = VERDICTS.find((d) => d.key === k);
    if (k === 'f') {
      plate?.fit();
    } else if (k === '0') {
      plate?.oneToOne();
    } else if (k === '?') {
      if (sheet?.open) {
        sheet.close();
      } else {
        sheet?.showModal();
      }
    } else if (verdict) {
      setVerdict(verdict.id);
    } else if (k === 'Delete' || k === 'Backspace') {
      setVerdict(null);
    } else if (k === 's') {
      toggleStar();
    } else if (k === 'n') {
      if (narrow) {
        tab = 'readout';
      }
      queueMicrotask(() => noteEl?.focus());
    } else {
      return false;
    }
    return true;
  }

  const PAN: Record<string, [number, number]> = {
    ArrowLeft: [-0.2, 0],
    ArrowRight: [0.2, 0],
    ArrowUp: [0, -0.2],
    ArrowDown: [0, 0.2],
  };

  // Keys that move the plate. Plain arrows step through the list, so panning takes Shift.
  function viewKey(e: KeyboardEvent): boolean {
    const pan = e.shiftKey ? PAN[e.key] : undefined;
    if (pan) {
      plate?.panBy(...pan);
    } else if (e.key === '+' || e.key === '=') {
      plate?.zoomBy(1.25);
    } else if (e.key === '-' || e.key === '_') {
      plate?.zoomBy(0.8);
    } else {
      return false;
    }
    return true;
  }

  function focusKind(el: HTMLElement): { typing: boolean; widget: boolean } {
    const type = el instanceof HTMLInputElement ? el.type : '';
    // Sliders and checkboxes keep their own arrow and delete keys but let every other key through.
    const widget = type === 'range' || type === 'checkbox';
    const typing =
      el.isContentEditable ||
      el instanceof HTMLTextAreaElement ||
      el instanceof HTMLSelectElement ||
      (el instanceof HTMLInputElement && !widget);
    return { typing, widget };
  }

  function onkeydown(e: KeyboardEvent) {
    const el = e.target as HTMLElement;
    const { typing, widget } = focusKind(el);
    if (e.key === 'Escape') {
      if (goto !== null) {
        goto = null;
      }
      if (typing || widget) {
        el.blur();
      }
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey || !bundle) {
      return;
    }
    if (widget && /^(?:Arrow|Home|End|Page|Delete|Backspace)/u.test(e.key)) {
      return;
    }
    if (sheet?.open && e.key !== '?') {
      return;
    }
    // A held key steps through the list, but it mustn't mark every tile it passes.
    if (viewKey(e) || moveKey(e.key) || (!e.repeat && actKey(e.key))) {
      e.preventDefault();
    }
  }

  const save = $derived(verdicts?.state ?? { kind: 'idle' as const });
  const clock = (iso: string) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime())
      ? iso
      : d.toLocaleTimeString('en-CA', { hour12: false });
  };
</script>

<!-- A select keeps focus after a choice, which would swallow J and K, so it lets go once it changes. -->
<svelte:window
  {onkeydown}
  onchange={(e: Event) => {
    if (e.target instanceof HTMLSelectElement) {
      e.target.blur();
    }
  }}
/>

{#if loadError}
  <main class="fatal">
    <h1>Couldn't Open the List</h1>
    <p>{loadError}</p>
    <p class="dim">
      The viewer reads <code>{base}index.json</code>. Point it at another exported list
      with <code>?run=path/to/run/</code> in the address.
    </p>
  </main>
{:else if !bundle || !verdicts || !tile}
  <main class="fatal">
    <p class="label">Opening List</p>
  </main>
{:else}
  <div class="app" class:narrow data-tab={tab}>
    <h1 class="sr">jwst-peculiar, {bundle.run.name}</h1>
    {#if !bundle.writable}
      <div class="demo">
        <span class="label">Demo List</span>
        <span class="name">{bundle.run.name}</span>
        <span class="dim"
          >Verdicts stay in this browser. Export them from the Verdict block.</span
        >
        <a
          href="https://github.com/SomethingObvious/jwst-peculiar"
          target="_blank"
          rel="noopener noreferrer">jwst-peculiar on GitHub</a
        >
      </div>
    {/if}

    {#if narrow}
      <nav class="tabs" aria-label="Panels">
        {#each ['Index', 'Plate', 'Readout'] as const as label (label)}
          {@const t = label.toLowerCase() as typeof tab}
          <button class="ctl" aria-pressed={tab === t} onclick={() => (tab = t)}
            >{label}</button
          >
        {/each}
      </nav>
    {/if}

    <div class="cols">
      <div class="col index-col">
        <IndexList
          {bundle}
          {order}
          {pos}
          {current}
          {verdicts}
          {reviewed}
          bind:filters
          bind:filterInput
          onselect={(i: number) => {
            select(i);
            if (narrow) {
              tab = 'plate';
            }
          }}
        />
      </div>
      <main class="col plate-col" aria-label="Plate">
        <Plate
          bind:this={plate}
          {tile}
          index={current}
          {data}
          error={tileError}
          {frame}
          {theme}
          oncursor={(c: { ix: number; iy: number } | null) => (cursor = c)}
        />
      </main>
      <div class="col readout-col">
        <Readout
          {bundle}
          {base}
          {current}
          {data}
          {frame}
          {cursor}
          {verdicts}
          {order}
          {theme}
          bind:advance
          bind:noteEl
          onselect={select}
          onverdict={setVerdict}
          onstar={toggleStar}
        />
      </div>
    </div>

    <footer class="rail">
      {#if goto !== null}
        <form
          class="goto"
          onsubmit={(e) => {
            e.preventDefault();
            jump();
          }}
        >
          <label
            ><span class="label">Go to Rank</span><input
              class="ctl num"
              bind:this={gotoEl}
              bind:value={goto}
              inputmode="numeric"
            /></label
          >
          <button class="ctl" type="submit">Go</button>
          {#if gotoMsg}<span class="dim">{gotoMsg}</span>{/if}
        </form>
      {:else}
        <span class="hints">
          <span><kbd>j</kbd><kbd>k</kbd> Step</span>
          <span><kbd>1</kbd><kbd>2</kbd><kbd>3</kbd><kbd>4</kbd> Verdict</span>
          <span><kbd>s</kbd> Star</span>
          <span><kbd>n</kbd> Note</span>
          <span><kbd>g</kbd> Rank</span>
          <button class="keys" onclick={() => sheet?.showModal()}
            ><kbd>?</kbd> All Keys</button
          >
        </span>
      {/if}
      {#if narrow}
        <span class="step" role="group" aria-label="Step Through the List">
          <button
            class="ctl"
            disabled={pos === 0}
            onclick={() => {
              step(-1);
            }}>Prev</button
          >
          <button
            class="ctl"
            disabled={pos === order.length - 1}
            onclick={() => {
              step(1);
            }}>Next</button
          >
        </span>
      {/if}
      <span class="where" aria-live="polite" aria-atomic="true">
        <span>Rank <b class="num">{tile.rank}</b></span>
        <span class="dim"
          >{#if pos >= 0}<span class="num">{pos + 1}</span> of
            <span class="num">{order.length}</span> shown{:else}not in the filtered list{/if}</span
        >
      </span>
      <!-- The countdown sits outside the live region, or a reader would hear it every second. -->
      <span class="save">
        <span class="save" role="status">
          {#if save.kind === 'saved'}
            <i class="led ok"></i>Saved {clock(save.at)}
          {:else if save.kind === 'saving'}
            <i class="led busy"></i>Saving {save.pending}
          {:else if save.kind === 'retrying'}
            <i class="led bad"></i>Not saved, {save.pending} waiting ({save.error}).
          {:else if save.kind === 'rejected'}
            {@const r = bundle.tiles[indexOfKey.get(save.key) ?? -1]?.rank}
            <i class="led bad"></i>The server turned down the verdict on rank {r} ({save.error}),
            so it was dropped.
          {:else if save.kind === 'local' && !verdicts.storageOk}
            <i class="led bad"></i>This browser won't store verdicts. Export them before
            you close the tab.
          {:else if save.kind === 'local'}
            <i class="led ok"></i><span
              ><span class="num">{save.count}</span>
              {save.count === 1 ? 'verdict' : 'verdicts'} in this browser</span
            >
          {:else}
            <i class="led ok"></i>Every verdict saved
          {/if}
        </span>
        {#if save.kind === 'retrying'}
          Retrying in {save.inSeconds} s
          <button class="ctl mini" onclick={() => verdicts?.retryNow()}
            >Retry Now</button
          >
        {/if}
      </span>
      <span class="themes" role="group" aria-label="Theme">
        {#each THEMES as label (label)}
          {@const t = label.toLowerCase() as Theme}
          <button
            class="ctl mini"
            aria-pressed={theme === t}
            onclick={() => (theme = t)}>{label}</button
          >
        {/each}
      </span>
    </footer>

    <dialog
      bind:this={sheet}
      class="sheet"
      aria-label="Keyboard Keys"
      onclick={(e) => {
        if (e.target === sheet) {
          sheet.close();
        }
      }}
    >
      <div class="sheet-head">
        <h2 class="label">Keys</h2>
        <button class="ctl mini" onclick={() => sheet?.close()}>Close</button>
      </div>
      <dl>
        <dt><kbd>j</kbd> <kbd>Down</kbd> <kbd>Right</kbd></dt>
        <dd>Next candidate</dd>
        <dt><kbd>k</kbd> <kbd>Up</kbd> <kbd>Left</kbd></dt>
        <dd>Previous candidate</dd>
        <dt><kbd>Home</kbd> <kbd>End</kbd></dt>
        <dd>First or last in the list</dd>
        <dt><kbd>PgDn</kbd> <kbd>PgUp</kbd></dt>
        <dd>Ten forward or back</dd>
        <dt><kbd>g</kbd></dt>
        <dd>Go to a rank</dd>
        <dt><kbd>1</kbd></dt>
        <dd>Interesting</dd>
        <dt><kbd>2</kbd></dt>
        <dd>Artifact</dd>
        <dt><kbd>3</kbd></dt>
        <dd>Known object</dd>
        <dt><kbd>4</kbd></dt>
        <dd>Not anomalous</dd>
        <dt><kbd>Del</kbd></dt>
        <dd>Clear the verdict</dd>
        <dt><kbd>s</kbd></dt>
        <dd>Star or unstar</dd>
        <dt><kbd>n</kbd></dt>
        <dd>Write a note, <kbd>Esc</kbd> to leave it</dd>
        <dt><kbd>/</kbd></dt>
        <dd>Filter the index</dd>
        <dt><kbd>f</kbd></dt>
        <dd>Fit the stamp</dd>
        <dt><kbd>0</kbd></dt>
        <dd>One image pixel per screen pixel</dd>
        <dt><kbd>+</kbd> <kbd>-</kbd></dt>
        <dd>Zoom in or out</dd>
        <dt><kbd>Shift</kbd> <kbd>Arrows</kbd></dt>
        <dd>Pan the stamp</dd>
        <dt><kbd>?</kbd></dt>
        <dd>This sheet</dd>
      </dl>
      <p class="dim">
        A verdict moves to the next object unless you turn that off in the Verdict
        block. Drag the plate to pan, scroll or pinch to zoom and double-click to fit.
      </p>
    </dialog>
  </div>
{/if}

<style>
  .fatal {
    height: 100dvh;
    display: grid;
    place-content: center;
    padding: 16px;
    max-width: 60ch;
    margin: 0 auto;
  }
  .fatal h1 {
    font-size: 18px;
    font-weight: 500;
    color: var(--anno);
    margin: 0 0 8px;
  }
  .fatal p {
    margin: 0 0 8px;
  }
  .fatal .label {
    color: var(--anno);
  }
  code {
    font-family: var(--font-num);
    font-size: 11.5px;
    color: var(--anno);
  }
  .dim {
    color: var(--chalk-3);
  }
  .app {
    height: 100dvh;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .demo {
    display: flex;
    align-items: baseline;
    gap: 14px;
    padding: 4px 12px;
    border-bottom: 1px solid var(--rule);
    background: var(--ground-sunk);
    font-size: 11.5px;
    white-space: nowrap;
    overflow: hidden;
  }
  .demo .label {
    color: var(--anno);
  }
  .demo .name {
    font-size: 13px;
  }
  .demo a {
    margin-left: auto;
    padding-block: 4px;
    margin-block: -4px;
  }
  .demo,
  .tabs {
    flex: none;
  }
  .cols {
    flex: 1 1 0;
    display: grid;
    grid-template-columns: 300px minmax(0, 1fr) 340px;
    min-height: 0;
  }
  .col {
    min-height: 0;
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr);
  }
  .rail {
    flex: none;
    height: 28px;
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 12px;
    border-top: 1px solid var(--rule);
    background: var(--ground-sunk);
    font-size: 11px;
    color: var(--chalk-2);
    white-space: nowrap;
    overflow: hidden;
  }
  .hints {
    display: flex;
    gap: 14px;
    align-items: center;
  }
  .hints > span {
    display: inline-flex;
    gap: 3px;
    align-items: center;
  }
  .hints > span > kbd:last-of-type {
    margin-right: 3px;
  }
  .keys {
    background: none;
    border: 0;
    padding: 0;
    color: var(--chalk-2);
    cursor: pointer;
    display: inline-flex;
    gap: 6px;
    align-items: center;
    min-height: 24px;
  }
  .keys:hover {
    color: var(--anno);
  }
  .where {
    margin-left: auto;
    display: inline-flex;
    gap: 8px;
    align-items: baseline;
  }
  .where b {
    color: var(--anno);
    font-weight: 500;
  }
  .save {
    display: inline-flex;
    gap: 6px;
    align-items: center;
    color: var(--chalk-2);
  }
  .led {
    width: 6px;
    height: 6px;
    display: inline-block;
    transform: rotate(45deg);
  }
  .led.ok {
    background: var(--v-known);
  }
  .led.busy {
    background: var(--anno);
  }
  .led.bad {
    background: transparent;
    border: 1.5px solid var(--anno);
  }
  .themes {
    display: flex;
  }
  .themes .ctl + .ctl {
    margin-left: -1px;
  }
  .mini {
    height: 24px;
    padding: 0 7px;
    font-size: 11px;
  }
  .goto {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .goto label {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--anno);
  }
  .goto input {
    width: 80px;
    height: 20px;
  }
  .sheet {
    background: var(--ground-2);
    color: var(--chalk);
    border: 1px solid var(--rule-strong);
    border-radius: 2px;
    padding: 14px 18px 16px;
    width: min(440px, calc(100vw - 32px));
  }
  .sheet::backdrop {
    background: rgb(4 8 16 / 0.6);
  }
  .sheet-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--rule);
  }
  .sheet h2 {
    margin: 0;
    color: var(--anno);
  }
  .sheet dl {
    display: grid;
    grid-template-columns: 150px 1fr;
    gap: 5px 12px;
    margin: 0 0 12px;
  }
  .sheet dt {
    display: flex;
    gap: 3px;
  }
  .sheet dd {
    margin: 0;
    color: var(--chalk-2);
  }
  .sheet p {
    margin: 0;
    font-size: 11.5px;
    line-height: 1.5;
  }

  /* Phones: the plate with tabs for the index and readout. */
  .narrow .rail {
    height: 52px;
    gap: 12px;
  }
  .step {
    display: inline-flex;
    gap: 6px;
  }
  .step .ctl {
    height: 40px;
    min-width: 64px;
  }
  .tabs {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
    padding: 6px 16px;
    border-bottom: 1px solid var(--rule);
  }
  .tabs .ctl {
    height: 28px;
  }
  .narrow .cols {
    grid-template-columns: minmax(0, 1fr);
  }
  .narrow .col {
    display: none;
  }
  .narrow[data-tab='index'] .index-col,
  .narrow[data-tab='plate'] .plate-col,
  .narrow[data-tab='readout'] .readout-col {
    display: grid;
  }
  .narrow .demo {
    padding: 4px 16px;
    flex-wrap: wrap;
    white-space: normal;
    gap: 2px 10px;
  }
  .narrow .demo .dim {
    display: none;
  }
  .narrow .rail {
    padding: 0 16px;
  }
  .narrow .hints,
  .narrow .themes {
    display: none;
  }
  .narrow .where {
    margin-left: 0;
  }
  .narrow .where .dim {
    display: none;
  }
  .narrow .save {
    margin-left: auto;
  }
  .narrow :global(.index),
  .narrow :global(.readout) {
    border: 0;
  }
  .narrow :global(.index .controls),
  .narrow :global(.readout .block) {
    padding-left: 16px;
    padding-right: 16px;
  }
  .narrow :global(.index .item) {
    padding-right: 16px;
  }
  .narrow :global(.plate .toolbar) {
    padding: 6px 16px;
  }
  @media (pointer: coarse) {
    .demo {
      align-items: center;
      padding-block: 0;
    }
    .demo a {
      display: inline-flex;
      align-items: center;
      min-height: 44px;
      margin-block: 0;
      padding-block: 0;
    }
  }
</style>
