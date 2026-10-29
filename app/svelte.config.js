import adapter from '@sveltejs/adapter-vercel';

export default {
  kit: {
    // Vercel's Node 22 runtime, named so builds also run on newer local and CI Node
    adapter: adapter({ runtime: 'nodejs22.x' }),
    // .env lives at the repo root, shared with the pipeline
    env: { dir: '..' }
  }
};
