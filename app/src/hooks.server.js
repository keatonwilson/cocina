// Sign-in for the review inbox (Supabase magic links, session in cookies).
// /mcp is left alone: it has its own bearer token.
import { createServerClient } from '@supabase/ssr';
import { error, redirect } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { isReviewer } from '$lib/inbox.js';

const SIGNED_IN = ['/review', '/login', '/auth'];
const NO_STORE = { 'cache-control': 'private, no-store' };

// For page loads these are built here rather than thrown: a thrown redirect or
// error skips setHeaders. They carry no session cookie either, so nothing
// per-person can be cached. SvelteKit's own fetches (an enhanced Accept or
// Reject, a data reload) still get the thrown kind, the JSON its client follows.
const toLogin = () => new Response(null, { status: 303, headers: { ...NO_STORE, location: '/login' } });
const notReviewer = () => new Response(
  '<!doctype html><meta charset="utf-8"><title>Not a reviewer</title>' +
  '<p>This account is not a cocina reviewer.</p><form method="POST" action="/auth/logout"><button>Sign out</button></form>',
  { status: 403, headers: { ...NO_STORE, 'content-type': 'text/html; charset=utf-8' } });

/** @type {import('@sveltejs/kit').Handle} */
export async function handle({ event, resolve }) {
  const path = event.url.pathname;
  if (!SIGNED_IN.some((p) => path === p || path.startsWith(`${p}/`))) return resolve(event);
  // Per-person pages that may set session cookies: never cached by a CDN or proxy
  // (covers the no-cache headers @supabase/ssr passes to setAll)
  event.setHeaders(NO_STORE);

  event.locals.supabase = createServerClient(env.COCINA_SUPABASE_URL, env.COCINA_SUPABASE_ANON_KEY, {
    cookies: {
      getAll: () => event.cookies.getAll(),
      setAll: (cookies) => cookies.forEach(({ name, value, options }) => event.cookies.set(name, value, { ...options, path: '/' }))
    }
  });

  if (path === '/review' || path.startsWith('/review/')) {
    // getUser() asks Supabase Auth, so a forged cookie doesn't get in
    const { data: { user } } = await event.locals.supabase.auth.getUser();
    const kitFetch = event.isDataRequest || event.request.headers.has('x-sveltekit-action');
    if (!user) {
      if (kitFetch) redirect(303, '/login');
      return toLogin();
    }
    if (!isReviewer(user.email, env.REVIEWERS)) {
      if (kitFetch) error(403, 'This account is not a reviewer.');
      return notReviewer();
    }
    event.locals.reviewer = user.email;
  }
  return resolve(event);
}
