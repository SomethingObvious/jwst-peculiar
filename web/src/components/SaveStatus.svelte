<script lang="ts">
  import type { Bundle } from '../lib/types';
  import type { Verdicts } from '../lib/verdicts.svelte';

  let { bundle, verdicts }: { bundle: Bundle; verdicts: Verdicts } = $props();

  const save = $derived(verdicts.state);
  const rankOf = (key: string) => bundle.tiles.find((t) => t.key === key)?.rank;
  const clock = (iso: string) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime())
      ? iso
      : d.toLocaleTimeString('en-CA', { hour12: false });
  };
</script>

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
      {@const r = rankOf(save.key)}
      <i class="led bad"></i>The server turned down the verdict on rank {r} ({save.error}),
      so it was dropped.
    {:else if save.kind === 'local' && !verdicts.storageOk}
      <i class="led bad"></i>This browser won't store verdicts. Export them before you
      close the tab.
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
    <button
      class="ctl mini"
      onclick={() => {
        verdicts.retryNow();
      }}>Retry Now</button
    >
  {/if}
</span>

<style>
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
</style>
