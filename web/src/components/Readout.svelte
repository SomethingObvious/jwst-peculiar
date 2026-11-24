<script lang="ts">
  import Glyph from './Glyph.svelte';
  import Locator from './Locator.svelte';
  import type { TileData } from '../lib/tiles';
  import type { TileFrame } from '../lib/frame';
  import { VERDICTS, type Bundle, type Theme, type Verdict } from '../lib/types';
  import { decDms, icrsToGalactic, raHms, signed } from '../lib/wcs';
  import type { Verdicts } from '../lib/verdicts.svelte';

  let {
    bundle,
    base,
    current,
    data,
    frame,
    cursor,
    verdicts,
    order,
    theme,
    advance = $bindable(),
    noteEl = $bindable(),
    onselect,
    onverdict,
    onstar,
  }: {
    bundle: Bundle;
    base: string;
    current: number;
    data: TileData | null;
    frame: TileFrame | null;
    cursor: { ix: number; iy: number } | null;
    verdicts: Verdicts;
    order: Int32Array;
    theme: Theme;
    advance: boolean;
    noteEl?: HTMLTextAreaElement | undefined;
    onselect: (i: number) => void;
    onverdict: (v: Verdict | null) => void;
    onstar: () => void;
  } = $props();

  const tile = $derived.by(() => {
    const t = bundle.tiles[current];
    if (!t) {
      throw new Error(`The list has no object ${String(current)}`);
    }
    return t;
  });
  const mosaic = $derived(bundle.mosaics[tile.mosaic]);
  const record = $derived(verdicts.map.get(tile.key));

  // With no pointer on the plate the readout shows the stamp's centre, so it is never empty.
  const probe = $derived.by(() => {
    if (cursor) {
      return { ...cursor, live: true };
    }
    return data ? { ix: data.width / 2, iy: data.height / 2, live: false } : null;
  });

  const read = $derived.by(() => {
    if (!probe || !data || !frame) {
      return null;
    }
    const px = Math.floor(probe.ix);
    const py = Math.floor(probe.iy);
    const [mx, my] = frame.toMosaic(px + 0.5, py + 0.5);
    const sky = frame.toSky(px + 0.5, py + 0.5);
    return {
      px,
      py,
      mx,
      my,
      sky: sky ? { ra: sky[0], dec: sky[1], gal: icrsToGalactic(...sky) } : null,
    };
  });

  const arcsec = $derived((mosaic?.pixel_scale_arcsec ?? 0) * tile.size);
  // Note, saved after a pause in typing and whenever the tile changes.
  let draft = $state('');
  let draftKey = '';
  // Whether the draft holds typing that isn't saved yet. Without it, a note that an
  // import or another tab brought in would be overwritten by the stale draft.
  let dirty = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  function commitNote() {
    if (timer) {
      clearTimeout(timer);
    }
    timer = null;
    if (dirty && draftKey) {
      verdicts.update(draftKey, { note: draft });
    }
    dirty = false;
  }
  function typed() {
    dirty = true;
    if (timer) {
      clearTimeout(timer);
    }
    timer = setTimeout(commitNote, 700);
  }
  $effect(() => {
    const key = tile.key;
    const stored = verdicts.map.get(key)?.note ?? '';
    if (key !== draftKey) {
      commitNote();
      draftKey = key;
      draft = stored;
    } else if (!dirty) {
      draft = stored;
    }
  });
  $effect(() => {
    // A tab closed inside the pause would lose what was typed.
    addEventListener('pagehide', commitNote);
    return () => {
      removeEventListener('pagehide', commitNote);
      commitNote();
    };
  });

  const save = $derived(verdicts.state);
  let copied = $state('');
  async function copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      copied = what;
    } catch {
      copied = 'blocked';
    }
    setTimeout(() => (copied = ''), 1600);
  }

  const radius = $derived(Math.max(5, (arcsec / 2) * Math.SQRT2));
  const links = $derived.by(() => {
    const ra = tile.ra.toFixed(6);
    const dec = tile.dec.toFixed(6);
    const fovDeg = Math.max(0.01, (arcsec * 2) / 3600).toFixed(4);
    const pos = encodeURIComponent(`${ra} ${dec}`);
    return [
      {
        label: 'ESASky',
        href: `https://sky.esa.int/esasky/?target=${pos}&fov=${fovDeg}&cooframe=J2000&sci=true`,
      },
      {
        label: 'Aladin Lite',
        href: `https://aladin.cds.unistra.fr/AladinLite/?target=${pos}&fov=${fovDeg}&survey=CDS%2FP%2FDSS2%2Fcolor`,
      },
      {
        label: 'SIMBAD Cone',
        href: `https://simbad.cds.unistra.fr/simbad/sim-coo?Coord=${pos}&CooFrame=ICRS&Radius=${radius.toFixed(1)}&Radius.unit=arcsec&submit=submit%20query`,
      },
      {
        label: 'NED Cone',
        href: `https://ned.ipac.caltech.edu/conesearch?search_type=Near%20Position%20Search&in_csys=Equatorial&in_equinox=J2000&coordinates=${encodeURIComponent(`${ra}d ${dec}d`)}&radius=${(radius / 60).toFixed(3)}`,
      },
      {
        label: 'MAST Portal',
        href: `https://mast.stsci.edu/portal/Mashup/Clients/Mast/Portal.html?searchQuery=${pos}`,
      },
    ];
  });

  // Import and export, for the hosted demo where verdicts stay in the browser.
  let importMsg = $state('');
  function exportCsv() {
    const blob = new Blob([verdicts.toCsv(bundle)], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${bundle.run.id ?? bundle.run.name}-verdicts.csv`;
    a.click();
    setTimeout(() => {
      URL.revokeObjectURL(a.href);
    }, 1000);
  }
  async function importFile(e: Event & { currentTarget: HTMLInputElement }) {
    const f = e.currentTarget.files?.[0];
    e.currentTarget.value = '';
    if (!f) {
      return;
    }
    try {
      const { taken, skipped } = verdicts.importText(await f.text());
      const left = skipped ? ` Left out ${String(skipped)} not in this list.` : '';
      importMsg = `Took ${String(taken)} ${taken === 1 ? 'object' : 'objects'} from ${f.name}.${left}`;
    } catch (err) {
      importMsg = `Couldn't read ${f.name}. ${err instanceof Error ? err.message : String(err)}`;
    }
  }
</script>

{#snippet head(title: string, aside?: string)}
  <h2 class="bh">
    <span class="label">{title}</span>
    <span class="rule"></span>
    {#if aside}<span class="aside num">{aside}</span>{:else}<span class="node"
      ></span>{/if}
  </h2>
{/snippet}

<aside class="readout" aria-label="Readout">
  <section class="block">
    {@render head(
      probe?.live ? 'Cursor' : 'Centre',
      probe?.live ? undefined : 'pointer off plate',
    )}
    {#if read}
      <dl class="facts">
        <dt>Stamp px</dt>
        <dd class="num">{read.px}, {read.py}</dd>
        <dt>Mosaic px</dt>
        <dd class="num">{read.mx.toFixed(1)}, {read.my.toFixed(1)}</dd>
        {#if read.sky}
          {@const { ra, dec, gal } = read.sky}
          <dt>RA</dt>
          <dd class="num">
            <b>{raHms(ra, 3)}</b><span class="sub">{ra.toFixed(6)}&deg;</span>
          </dd>
          <dt>Dec</dt>
          <dd class="num">
            <b>{decDms(dec, 2)}</b><span class="sub">{signed(dec, 6)}&deg;</span>
          </dd>
          <dt>Galactic</dt>
          <dd class="num">
            l {gal[0].toFixed(5)}&deg; <span class="gap"></span> b {signed(
              gal[1],
              5,
            )}&deg;
          </dd>
        {:else}
          <dt>Sky</dt>
          <dd class="dim">This mosaic has no WCS the viewer reads.</dd>
        {/if}
      </dl>
    {:else}
      <p class="quiet">Waiting for the stamp to load.</p>
    {/if}
  </section>

  <section class="block">
    {@render head('Object')}
    <dl class="facts">
      <dt>Rank</dt>
      <dd class="num">
        <b>{tile.rank.toLocaleString('en-CA')}</b>
        <span class="dim">of {bundle.tiles.length.toLocaleString('en-CA')}</span>
      </dd>
      <dt>Score</dt>
      <dd class="num">
        {#if tile.score === null}<b>unscored</b>
          <span class="dim">nothing was left to compare it with</span>{:else}<b
            >{tile.score.toFixed(4)}</b
          >{/if}
      </dd>
      <dt>Field</dt>
      <dd class="name mosaic" title={tile.mosaic}>{tile.mosaic}</dd>
      <dt>Centre</dt>
      <dd class="num">
        <b>{raHms(tile.ra)}</b> <b>{decDms(tile.dec)}</b><span class="sub"
          >{tile.ra.toFixed(5)}&deg; {signed(tile.dec, 5)}&deg;</span
        >
      </dd>
      <dt>Size</dt>
      <dd class="num">
        {tile.size} px <span class="gap"></span>
        {arcsec.toFixed(2)}&Prime;
      </dd>
      <dt>Pixels</dt>
      <dd>
        {#if data}
          <span class="num">{data.width} &times; {data.height}</span>
        {:else}
          <span class="dim">Loading</span>
        {/if}
      </dd>
    </dl>
  </section>

  <section class="block">
    {@render head(
      'Verdict',
      record?.updated
        ? record.updated.replace('T', ' ').replace('Z', ' UTC')
        : undefined,
    )}
    <div class="verdicts">
      {#each VERDICTS as v (v.id)}
        <button
          class="ctl vbtn"
          aria-pressed={record?.verdict === v.id}
          onclick={() => {
            onverdict(v.id);
          }}
        >
          <Glyph verdict={v.id} />
          <span>{v.label}</span>
          <kbd>{v.key}</kbd>
        </button>
      {/each}
      <button
        class="ctl vbtn"
        onclick={() => {
          onverdict(null);
        }}
        disabled={!record?.verdict}
      >
        <Glyph verdict={null} />
        <span>Clear</span>
        <kbd>Del</kbd>
      </button>
      <button class="ctl vbtn" aria-pressed={Boolean(record?.star)} onclick={onstar}>
        <Glyph verdict={record?.star ? 'star' : 'star-off'} size={11} />
        <span>{record?.star ? 'Starred' : 'Star'}</span>
        <kbd>s</kbd>
      </button>
    </div>
    <label class="note">
      <span class="fl label">Note <kbd>n</kbd></span>
      <textarea
        bind:this={noteEl}
        bind:value={draft}
        oninput={typed}
        onblur={commitNote}
        rows="3"
        placeholder="What you see here, for whoever follows it up"></textarea>
    </label>
    <div class="saverow">
      <label class="check"
        ><input type="checkbox" bind:checked={advance} /> Next object after a verdict</label
      >
    </div>
    {#if !bundle.writable}
      <div class="io">
        <button class="ctl" onclick={exportCsv}>Export CSV</button>
        <label class="ctl file"
          >Import<input
            type="file"
            accept=".csv,.json,text/csv,application/json"
            onchange={importFile}
          /></label
        >
        <span class="dim small">
          {#if save.kind === 'local'}<span class="num">{save.count}</span>
            {save.count === 1 ? 'verdict' : 'verdicts'} in this browser{/if}
          {#if !verdicts.storageOk}This browser is blocking storage, so export before
            you close the tab.{/if}
        </span>
      </div>
      {#if importMsg}<p class="quiet">{importMsg}</p>{/if}
    {/if}
  </section>

  <section class="block">
    {@render head('Field Locator')}
    <Locator {bundle} {base} {current} {order} {theme} {onselect} />
  </section>

  <section class="block">
    {@render head('Links Out', 'object centre')}
    <ul class="links">
      {#each links as l (l.label)}
        <li>
          <a href={l.href} target="_blank" rel="noopener noreferrer">
            {l.label}
            <svg width="9" height="9" viewBox="0 0 9 9" aria-hidden="true"
              ><path
                d="M3 1.5 H7.5 V6 M7.5 1.5 L1.5 7.5"
                fill="none"
                stroke="currentColor"
                stroke-width="1.1"
              /></svg
            >
          </a>
        </li>
      {/each}
    </ul>
    <div class="io">
      <button
        class="ctl"
        onclick={() => copy(`${tile.ra.toFixed(6)} ${tile.dec.toFixed(6)}`, 'deg')}
        >{copied === 'deg' ? 'Copied' : 'Copy Degrees'}</button
      >
      <button
        class="ctl"
        onclick={() =>
          copy(
            `${raHms(tile.ra)} ${tile.dec < 0 ? '-' : '+'}${decDms(Math.abs(tile.dec)).slice(1)}`,
            'sexa',
          )}>{copied === 'sexa' ? 'Copied' : 'Copy Sexagesimal'}</button
      >
      {#if copied === 'blocked'}<span class="dim small"
          >The browser blocked the clipboard.</span
        >{/if}
    </div>
  </section>

  <section class="block">
    {@render head('List')}
    <dl class="facts">
      <dt>Name</dt>
      <dd class="name">{bundle.run.name}</dd>
      <dt>Written</dt>
      <dd class="num">{bundle.run.created.slice(0, 10)}</dd>
    </dl>
  </section>
</aside>

<style>
  .readout {
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-gutter: stable;
    min-height: 0;
    border-left: 1px solid var(--rule);
    background: var(--ground-2);
  }
  .block {
    padding: 10px 14px 12px;
    border-bottom: 1px solid var(--rule);
    display: grid;
    gap: 8px;
  }
  .bh {
    margin: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: inherit;
    font-weight: inherit;
  }
  .bh .label {
    color: var(--anno);
  }
  .bh .rule {
    flex: 1;
    height: 1px;
    background: var(--rule);
  }
  .bh .node {
    width: 4px;
    height: 4px;
    background: var(--anno);
    transform: rotate(45deg);
    opacity: 0.7;
  }
  .bh .aside {
    color: var(--chalk-3);
    font-size: 10px;
  }
  .facts {
    display: grid;
    grid-template-columns: 74px 1fr;
    gap: 3px 10px;
    margin: 0;
    align-items: baseline;
  }
  dt {
    color: var(--chalk-3);
    font-size: 11px;
  }
  dd {
    margin: 0;
    min-width: 0;
    color: var(--chalk);
  }
  dd b {
    font-weight: 500;
    color: var(--anno);
  }
  dd .sub {
    display: block;
    color: var(--chalk-3);
  }
  .dim {
    color: var(--chalk-3);
  }
  .gap {
    display: inline-block;
    width: 8px;
  }
  .mosaic {
    font-size: 13.5px;
    overflow-wrap: anywhere;
  }
  .quiet {
    margin: 0;
    color: var(--chalk-2);
    font-size: 11.5px;
  }
  .small {
    font-size: 11px;
  }
  .fl {
    color: var(--chalk-3);
    padding-top: 6px;
  }
  .check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--chalk-2);
    cursor: pointer;
    white-space: nowrap;
  }
  .check input {
    accent-color: var(--anno);
    margin: 0;
  }
  .verdicts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 4px;
  }
  .vbtn {
    display: flex;
    align-items: center;
    gap: 7px;
    height: 28px;
    color: var(--chalk);
  }
  .vbtn span {
    flex: 1;
    text-align: left;
  }
  .vbtn[aria-pressed='true'] span {
    color: var(--anno);
  }
  .note {
    display: grid;
    gap: 4px;
  }
  .note .fl {
    padding: 0;
    display: flex;
    gap: 6px;
    align-items: center;
  }
  textarea {
    resize: vertical;
    min-height: 54px;
    background: var(--ground-sunk);
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 6px 8px;
    color: var(--chalk);
    line-height: 1.45;
  }
  textarea:focus {
    border-color: var(--anno);
    outline: none;
  }
  .io {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .file {
    display: inline-grid;
    place-items: center;
    position: relative;
  }
  .file input {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
  }
  .file:focus-within {
    outline: 1px solid var(--anno);
  }
  /* Centred on the first line, as a wrapped name makes the row several lines tall. */
  .links {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 4px 12px;
  }
  .links a {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    text-decoration: none;
    min-height: 24px;
  }
  .links a:hover {
    text-decoration: underline;
  }
  @media (pointer: coarse) {
    .check,
    .links a {
      min-height: 44px;
    }
  }
</style>
