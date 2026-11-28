<script lang="ts">
  let { dialog = $bindable() }: { dialog?: HTMLDialogElement | undefined } = $props();

  const KEYS: [string[], string][] = [
    [['j', 'Down', 'Right'], 'Next candidate'],
    [['k', 'Up', 'Left'], 'Previous candidate'],
    [['Home', 'End'], 'First or last in the list'],
    [['PgDn', 'PgUp'], 'Ten forward or back'],
    [['g'], 'Go to a rank'],
    [['1'], 'Interesting'],
    [['2'], 'Artifact'],
    [['3'], 'Known object'],
    [['4'], 'Not anomalous'],
    [['Del'], 'Clear the verdict'],
    [['s'], 'Star or unstar'],
    [['n'], 'Write a note, and Esc to leave it'],
    [['/'], 'Filter the index'],
    [['f'], 'Fit the stamp'],
    [['0'], 'One image pixel per screen pixel'],
    [['+', '-'], 'Zoom in or out'],
    [['Shift', 'Arrows'], 'Pan the stamp'],
    [['?'], 'This sheet'],
  ];
</script>

<dialog
  bind:this={dialog}
  class="sheet"
  aria-label="Keyboard Keys"
  onclick={(e) => {
    if (e.target === dialog) {
      dialog.close();
    }
  }}
>
  <div class="sheet-head">
    <h2 class="label">Keys</h2>
    <button class="ctl mini" onclick={() => dialog?.close()}>Close</button>
  </div>
  <dl>
    {#each KEYS as [keys, what] (what)}
      <dt>
        {#each keys as k (k)}<kbd>{k}</kbd>{/each}
      </dt>
      <dd>{what}</dd>
    {/each}
  </dl>
  <p class="dim">
    A verdict moves to the next object unless you turn that off in the Verdict block.
    Drag the plate to pan, scroll or pinch to zoom and double-click to fit.
  </p>
</dialog>

<style>
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
  h2 {
    margin: 0;
    color: var(--anno);
  }
  dl {
    display: grid;
    grid-template-columns: 150px 1fr;
    gap: 5px 12px;
    margin: 0 0 12px;
  }
  dt {
    display: flex;
    gap: 3px;
  }
  dd {
    margin: 0;
    color: var(--chalk-2);
  }
  p {
    margin: 0;
    font-size: 11.5px;
    line-height: 1.5;
  }
</style>
