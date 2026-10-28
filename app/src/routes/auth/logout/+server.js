import { redirect } from '@sveltejs/kit';

// Outside /review, so someone taken off REVIEWERS can still clear their session
export async function POST({ locals }) {
  await locals.supabase.auth.signOut();
  redirect(303, '/login');
}
