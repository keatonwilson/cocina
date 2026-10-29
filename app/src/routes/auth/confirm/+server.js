import { redirect } from '@sveltejs/kit';

// The email link: {{ .SiteURL }}/auth/confirm?token_hash={{ .TokenHash }}&type=email (Supabase's
// Magic Link and Confirm signup templates). The server checks the one-time token itself, so the
// link works in any browser, unlike /auth/callback's code, which needs the requesting browser's cookie.
const TYPES = new Set(['email', 'magiclink', 'signup']);

export async function GET({ url, locals }) {
  const token_hash = url.searchParams.get('token_hash');
  const type = url.searchParams.get('type') ?? 'email';
  if (token_hash && TYPES.has(type)) {
    const { error } = await locals.supabase.auth.verifyOtp({ token_hash, type: /** @type {any} */ (type) });
    if (!error) redirect(303, '/review');
  }
  redirect(303, '/login?failed');
}
