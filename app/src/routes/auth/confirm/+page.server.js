// The email link: {{ .RedirectTo }}?token_hash={{ .TokenHash }}&type=email (Supabase's Magic Link
// and Confirm signup templates; RedirectTo is the emailRedirectTo the login action sends, so the
// link returns to the deployment that asked). The server checks the one-time token itself, so any
// browser works. Opening the link only shows a button: mail scanners and link prefetchers that GET
// it can't use the token up; the POST does.
import { fail, redirect } from '@sveltejs/kit';

const TYPES = new Set(['email', 'magiclink', 'signup']);

const params = (url) => {
  const type = url.searchParams.get('type') ?? 'email';
  return { token_hash: url.searchParams.get('token_hash'), type: TYPES.has(type) ? type : null };
};

export const load = ({ url }) => {
  const { token_hash, type } = params(url);
  return { usable: Boolean(token_hash && type) };
};

export const actions = {
  default: async ({ url, locals }) => {
    const { token_hash, type } = params(url);
    if (!token_hash || !type) return fail(400, { message: 'This link is incomplete.' });
    const { error } = await locals.supabase.auth.verifyOtp({ token_hash, type: /** @type {any} */ (type) });
    if (error) return fail(400, { message: 'This link has expired or was already used.' });
    redirect(303, '/review');
  }
};
