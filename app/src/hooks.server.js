// Sign-in for the review inbox (Supabase magic links, session in cookies).
// /mcp is left alone: it has its own bearer token.
import { createServerClient } from '@supabase/ssr';
import { error, redirect } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { isReviewer } from '$lib/inbox.js';

const SIGNED_IN = ['/review', '/login', '/auth'];

/** @type {import('@sveltejs/kit').Handle} */
export async function handle({ event, resolve }) {
  const path = event.url.pathname;
  if (!SIGNED_IN.some((p) => path === p || path.startsWith(`${p}/`))) return resolve(event);

  event.locals.supabase = createServerClient(env.COCINA_SUPABASE_URL, env.COCINA_SUPABASE_ANON_KEY, {
    cookies: {
      getAll: () => event.cookies.getAll(),
      setAll: (cookies) => cookies.forEach(({ name, value, options }) => event.cookies.set(name, value, { ...options, path: '/' }))
    }
  });

  if (path === '/review' || path.startsWith('/review/')) {
    // getUser() asks Supabase Auth, so a forged cookie doesn't get in
    const { data: { user } } = await event.locals.supabase.auth.getUser();
    if (!user) redirect(303, '/login');
    if (!isReviewer(user.email, env.REVIEWERS)) error(403, 'This account is not a reviewer.');
    event.locals.reviewer = user.email;
  }
  return resolve(event);
}
