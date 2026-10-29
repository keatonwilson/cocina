<script>
  import { page } from '$app/stores';

  /** @type {{ sent?: string, again?: boolean, email?: string, message?: string } | null} */
  export let form;
</script>

<svelte:head><title>Sign in · cocina review</title></svelte:head>

<main>
  <h1>cocina review</h1>
  {#if form?.sent}
    <p>If {form.sent} is a reviewer, a sign-in link is on its way. Open it in this browser.</p>
    {#if form.again}<p class="note">One was sent in the last minute: check your inbox and spam before asking again.</p>{/if}
  {:else}
    {#if $page.url.searchParams.has('failed')}
      <p class="error">That link didn't work or has expired. Ask for a new one.</p>
    {/if}
    <form method="POST">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" autocomplete="email" required value={form?.email ?? ''} />
      <button>Send sign-in link</button>
    </form>
    {#if form?.message}<p class="error">{form.message}</p>{/if}
  {/if}
</main>

<style>
  main { max-width: 26rem; margin: 15vh auto; padding: 0 16px; font: 16px/1.5 system-ui, sans-serif; }
  form { display: grid; gap: 8px; }
  input, button { font: inherit; padding: 8px 10px; }
  .error { color: #b3261e; }
  .note { color: #6b665f; }
</style>
