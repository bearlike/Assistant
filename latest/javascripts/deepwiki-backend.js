// Wires window.mewboAskAI to DeepWiki MCP (https://mcp.deepwiki.com/mcp)
// using the *official* @modelcontextprotocol/sdk client, lazily loaded via ESM
// from esm.sh the first time the user asks a question. The SDK is maintained
// by the MCP spec authors at https://github.com/modelcontextprotocol/typescript-sdk,
// so protocol-version negotiation, SSE framing, JSON-RPC envelopes, and the
// initialize handshake stay correct as the spec evolves — we don't.
//
// Pinning to an exact SDK version keeps the wire contract reproducible. Review
// the upstream changelog before bumping.

(function () {
  'use strict';

  // Pinned: 1.29+ pulls zod via its experimental v4 namespace under esm.sh,
  // which trips a `t.custom is not a function` runtime error in the browser.
  // 1.20.0 pulls clean zod@^3.23.8 and works out of the box. Bump only after
  // verifying the upstream zod-v4 migration is stable on esm.sh.
  var SDK_VERSION      = '1.20.0';
  var SDK_BASE         = 'https://esm.sh/@modelcontextprotocol/sdk@' + SDK_VERSION;
  var DEEPWIKI_MCP_URL = 'https://mcp.deepwiki.com/mcp';
  var DEEPWIKI_ORIGIN  = 'https://deepwiki.com';
  var REPO             = 'bearlike/Assistant';
  var QUESTION_PREFIX  =
    "I'm a new user; give product-first, well-grounded answers. " +
    'Mewbo / Truss / Meeseeks / Assistant all refer to the same product.\n\n';

  // Cache the connected client so the SDK and handshake only run once per page.
  var clientPromise = null;

  function getClient() {
    if (clientPromise) return clientPromise;
    clientPromise = (async function () {
      var modules = await Promise.all([
        import(SDK_BASE + '/client/index.js'),
        import(SDK_BASE + '/client/streamableHttp.js'),
      ]);
      var Client    = modules[0].Client;
      var Transport = modules[1].StreamableHTTPClientTransport;
      var client    = new Client({ name: 'mewbo-docs', version: '0.1.0' });
      await client.connect(new Transport(new URL(DEEPWIKI_MCP_URL)));
      return client;
    })().catch(function (err) {
      // Reset so a transient SDK / network blip doesn't permanently brick Ask AI.
      clientPromise = null;
      throw err;
    });
    return clientPromise;
  }

  // DeepWiki cites `/wiki/...` and `/search/...` paths relative to deepwiki.com;
  // rewrite them so the markdown renderer's links don't 404 against the docs site.
  function absolutizeLinks(text) {
    return text.replace(/\]\((\/[^)\s]+)\)/g, '](' + DEEPWIKI_ORIGIN + '$1)');
  }

  async function askAI(query) {
    var client = await getClient();
    var result = await client.callTool({
      name: 'ask_question',
      arguments: { repoName: REPO, question: QUESTION_PREFIX + query }
    });
    var first = result && result.content && result.content[0];
    var text  = first && first.text;
    if (result && result.isError) throw new Error(text || 'DeepWiki tool error');
    if (!text) throw new Error('DeepWiki returned an empty answer');
    return absolutizeLinks(text);
  }

  window.mewboAskAI = askAI;
})();
