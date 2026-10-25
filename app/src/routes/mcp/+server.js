// The read-only MCP server: published knowledge only, with credit on every answer.
// Stateless Streamable HTTP (JSON responses), so it runs as a plain serverless function.
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js';
import { createClient } from '@supabase/supabase-js';
import { z } from 'zod';
import { dev } from '$app/environment';
import { env } from '$env/dynamic/private';

// The anon key: RLS and the read functions only ever return published rows
const db = () =>
  createClient(env.COCINA_SUPABASE_URL, env.COCINA_SUPABASE_ANON_KEY, { auth: { persistSession: false } });

async function rpc(name, args = {}) {
  const { data, error } = await db().rpc(name, args);
  if (error) throw new Error(error.message);
  return data;
}

// Every answer names the knowledge version it came from, so a trace records what was used
/** @returns {Promise<import('@modelcontextprotocol/sdk/types.js').CallToolResult>} */
async function reply(data, isError = false) {
  const body = { kb_version: await rpc('kb_version'), ...data };
  return { content: [{ type: 'text', text: JSON.stringify(body) }], ...(isError ? { isError } : {}) };
}

const CREDIT_NOTE = 'Show credit (authors or publisher, with links) wherever you use these facts.';

/** One entry per work cited on a card, with the facts it supports. */
function sourcesOf(card) {
  const works = new Map();
  for (const claim of card.claims) {
    for (const { quote, locator, ...work } of claim.sources) {
      const key = work.url || `${work.publisher}|${work.title}`;
      if (!works.has(key)) works.set(key, { ...work, supports: [] });
      works.get(key).supports.push(`${claim.predicate}: ${claim.value}`);
    }
  }
  return [...works.values()];
}

function server() {
  const mcp = new McpServer({ name: 'cocina', version: '0.1.0' });

  mcp.registerTool(
    'search',
    {
      description:
        'Find Mexican food concepts (dishes, menu categories, salsas, chiles, proteins, techniques, regions) ' +
        'by name, alias or misspelling. Returns slugs to pass to get_entity.',
      inputSchema: {
        query: z.string().min(1).max(200),
        kind: z.string().optional().describe('dish, menu_category, protein_prep, cut, salsa, chile, ingredient, technique, tortilla or region')
      }
    },
    async ({ query, kind }) => reply({ results: await rpc('search_entities', { q: query, p_kind: kind ?? null }) })
  );

  mcp.registerTool(
    'get_entity',
    {
      description: `What a food is: sourced claims, other names and links, each claim with its quotes and credit. ${CREDIT_NOTE}`,
      inputSchema: { slug: z.string().min(1).max(100) }
    },
    async ({ slug }) => {
      const card = await rpc('get_entity', { p_slug: slug });
      return card ? reply({ card, note: CREDIT_NOTE }) : reply({ error: `No published entry for "${slug}"` }, true);
    }
  );

  mcp.registerTool(
    'related',
    {
      description: 'Foods linked to this one (variants, ingredients, accompaniments, regions), up to 2 hops.',
      inputSchema: {
        slug: z.string().min(1).max(100),
        relation: z.string().optional().describe('e.g. variant_of, uses, served_with, originates_in'),
        hops: z.number().int().min(1).max(2).optional()
      }
    },
    async ({ slug, relation, hops }) =>
      reply({ related: await rpc('related', { p_slug: slug, p_predicate: relation ?? null, p_hops: hops ?? 1 }) })
  );

  mcp.registerTool(
    'sources',
    {
      description: `The works and people an entry relies on, each with the facts it supports. ${CREDIT_NOTE}`,
      inputSchema: { slug: z.string().min(1).max(100) }
    },
    async ({ slug }) => {
      const card = await rpc('get_entity', { p_slug: slug });
      return card ? reply({ sources: sourcesOf(card) }) : reply({ error: `No published entry for "${slug}"` }, true);
    }
  );

  return mcp;
}

async function handle({ request }) {
  // ponytail: one shared bearer token until we decide whether the knowledge is public
  if (!env.MCP_TOKEN && !dev) return new Response('MCP_TOKEN is not configured', { status: 500 });
  if (env.MCP_TOKEN && request.headers.get('authorization') !== `Bearer ${env.MCP_TOKEN}`) {
    return new Response('Unauthorized', { status: 401 });
  }
  const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  await server().connect(transport);
  return transport.handleRequest(request);
}

export const POST = handle;
export const GET = handle;
export const DELETE = handle;
