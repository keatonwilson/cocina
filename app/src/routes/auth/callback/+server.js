import { redirect } from '@sveltejs/kit';

// A link using the PKCE code (Supabase's default template) lands here: it only works in the
// browser that asked for it, which holds the code verifier. /auth/confirm works anywhere.
export async function GET({ url, locals }) {
  const code = url.searchParams.get('code');
  if (code) {
    const { error } = await locals.supabase.auth.exchangeCodeForSession(code);
    if (!error) redirect(303, '/review');
  }
  redirect(303, '/login?failed');
}
