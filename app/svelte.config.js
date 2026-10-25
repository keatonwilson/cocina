import adapter from '@sveltejs/adapter-vercel';

export default {
  kit: {
    adapter: adapter(),
    // .env lives at the repo root, shared with the pipeline
    env: { dir: '..' }
  }
};
