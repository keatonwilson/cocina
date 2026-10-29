import { fail } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { isReviewer } from '$lib/inbox.js';

export const actions = {
  default: async ({ request, locals, url }) => {
    const email = String((await request.formData()).get('email') ?? '').trim().toLowerCase();
    if (!email) return fail(400, { email, message: 'Enter your email.' });
    // Links go only to reviewers; everyone gets the same answer, so the form doesn't say who reviews
    if (isReviewer(email, env.REVIEWERS)) {
      const { error } = await locals.supabase.auth.signInWithOtp({
        email, options: { emailRedirectTo: `${url.origin}/auth/callback` }
      });
      // 429: Supabase's once-a-minute cooldown, i.e. a link already went out (a second click)
      if (error?.status === 429) return { sent: email, again: true };
      if (error) return fail(502, { email, message: `Couldn't send the link: ${error.message}` });
    }
    return { sent: email };
  }
};
