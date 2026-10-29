import { redirect } from '@sveltejs/kit';

// A link from Supabase's default template (a PKCE ?code=, passed on by /auth/confirm) lands here.
// It only works in the browser that asked for it, which holds the code verifier; the repo's
// template (supabase/templates/sign_in.html) uses /auth/confirm, which works anywhere.
export async function GET({ url, locals }) {
  const code = url.searchParams.get('code');
  if (code) {
    const { error } = await locals.supabase.auth.exchangeCodeForSession(code);
    if (!error) redirect(303, '/review');
  }
  redirect(303, '/login?failed');
}
