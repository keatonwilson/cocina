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
      // 429 (a second click inside Supabase's one-minute cooldown, or the hourly email cap): answer as
      // for anyone, so the page never tells reviewers apart, and leave the reason in the server log
      if (error?.status === 429) console.warn('sign-in link rate-limited:', error.message);
      else if (error) return fail(502, { email, message: `Couldn't send the link: ${error.message}` });
    }
    return { sent: email };
  }
};
