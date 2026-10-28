import { redirect } from '@sveltejs/kit';

// The magic link lands here with a code; trade it for a session cookie
export async function GET({ url, locals }) {
  const code = url.searchParams.get('code');
  if (code) {
    const { error } = await locals.supabase.auth.exchangeCodeForSession(code);
    if (!error) redirect(303, '/review');
  }
  redirect(303, '/login?failed');
}
