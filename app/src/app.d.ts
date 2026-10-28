import type { SupabaseClient } from '@supabase/supabase-js';

declare global {
  namespace App {
    interface Locals {
      /** Cookie-session client (publishable key), set on /review, /login and /auth */
      supabase: SupabaseClient;
      /** The signed-in reviewer's email, set on /review once checked against REVIEWERS */
      reviewer: string;
    }
  }
}

export {};
