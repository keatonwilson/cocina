<script>
  export let data;
  /** @type {{ message?: string } | null} */
  export let form;
</script>

<svelte:head><title>Sign in · cocina review</title></svelte:head>

<main>
  <h1>cocina review</h1>
  {#if data.usable && !form?.message}
    <form method="POST">
      <button>Sign in</button>
    </form>
  {:else}
    <p class="error">{form?.message ?? 'This link is incomplete.'}</p>
    <p><a href="/login">Ask for a new sign-in link</a></p>
  {/if}
</main>

<style>
  main { max-width: 26rem; margin: 15vh auto; padding: 0 16px; font: 16px/1.5 system-ui, sans-serif; }
  button { font: inherit; padding: 10px 14px; width: 100%; }
  .error { color: #b3261e; }
</style>
