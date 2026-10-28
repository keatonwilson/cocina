// The secret key: server code only ($lib/server can't be imported by the browser).
// The inbox reads proposals and decides them as the service role, after
// hooks.server.js has checked the reviewer.
import { createClient } from '@supabase/supabase-js';
import { env } from '$env/dynamic/private';

export const serviceDb = () =>
  createClient(env.COCINA_SUPABASE_URL, env.COCINA_SUPABASE_SERVICE_KEY, { auth: { persistSession: false } });
