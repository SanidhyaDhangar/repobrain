# RepoBrain: giving AI agents the context a senior engineer has, with a FalkorDB graph

*Built for Graph Hacks (WeMakeDevs × FalkorDB), Oct 2026 · [Live demo](https://repobrain-j9c4.onrender.com) · [Code](https://github.com/SanidhyaDhangar/repobrain)*

Every team has one person who just *knows*. Ask them "can I change this function?" and they say: "Careful, the request dispatcher calls it, and it was rewritten last year to pass the context explicitly. Talk to David, he did that refactor." That answer doesn't live in any single file. It's a walk across the call graph, the PR history, the issue that motivated the change, and the people involved.

AI coding agents don't have that walk. They see the file in front of them, maybe a vector search hit or two, and they forget everything when the session ends. So we built **RepoBrain**: point it at any public GitHub repo and it builds a knowledge graph in FalkorDB. Agents answer questions and review PRs by traversing that graph, and every answer shows the path it took.

## The graph

One ingest turns a repo into four connected layers:

```
(:File)-[:DEFINES]->(:Function)-[:CALLS]->(:Function)                      code
(:Person)-[:AUTHORED|REVIEWED]->(:PR)-[:MODIFIED]->(:Function)             history
(:PR)-[:FIXES]->(:Issue)<-[:OPENED]-(:Person)                              intent
(:Fact)-[:ABOUT]->(any)   (:Episode)-[:USED]->(any)   (:Note)-[:FOR]->(:Task)   memory
```

- **Code** comes from Python's own `ast` module: functions, methods, calls and imports, resolved by name.
- **History** comes from GitHub's GraphQL API: the last 200 merged PRs, their reviewers, the issues they closed, and every file in their diffs. A PR is linked to the functions it touched through the diff's hunk headers and changed `def` lines.
- **Intent** is the part most tools miss: the issue a PR fixed usually says *why* the code is the way it is.
- **Memory** is the agents' own knowledge, stored in the same graph as everything else.

Each repo gets its own graph inside one FalkorDB instance (`pallets__flask`, `psf__requests`, …). Isolated tenants cost nothing extra.

## Why a graph

Here's the question "why does `full_dispatch_request` look like this, and who do I ask?" written as one Cypher query:

```cypher
MATCH (t:Function) WHERE t.qname ENDS WITH 'Flask.full_dispatch_request'
MATCH p = (a:Person)-[:AUTHORED]->(pr:PR)-[:MODIFIED]->(t)
OPTIONAL MATCH q = (pr)-[:FIXES]->(i:Issue)
RETURN p, q, pr.merged_at, left(pr.body, 500), left(i.body, 300)
ORDER BY pr.merged_at DESC LIMIT 6
```

On Flask, that returns PR #5818 ("pass context through dispatch methods", by @davidism), which fixed Issue #5815 ("pass context internally instead of using contextvars"). Between them, the PR and the issue explain the design: pass one `AppContext` through every dispatch method instead of reading context proxies, an idea @pgjones first proposed to speed up async views. In a relational store this is four joins. In a vector store it's a guess. In the graph it's one hop pattern that runs in about 0.1 s against FalkorDB Cloud.

Every agent tool is a query like that:

| Tool | The graph work |
|---|---|
| `impact` | `(:Function)-[:CALLS*1..3]->(target)`: the blast radius, split into tests and non-tests, plus open issues that mention anything in it |
| `experts` | people ranked by PRs they authored or reviewed that changed the target |
| `history` | the PRs and issues behind the target, with their text |
| `critical` | `CALL algo.pageRank('Function', 'CALLS')`, plus who has ever changed each function, which surfaces knowledge silos |
| `connect` | the shortest chain linking any two entities, e.g. a person to a function |
| `search` | FalkorDB full-text indexes over PR and issue text, with stemming |

The LLM (gpt-oss-120b on Groq) only chooses which tool to call and writes up the result. The facts come from the graph, so the answers come with receipts.

## Memory that knows when it's out of date

Track 2 asks for memory that persists across sessions and updates over time. We store two kinds:

- **Episodes**: every question and answer, linked with `USED` edges to the code, PRs and people the agent touched. Ask a follow-up in the same session and the history comes back out of the graph.
- **Facts**: durable lessons ("@davidism owns the AppContext design, PR #5818") linked with `ABOUT` edges to what they describe. The agent saves them with a `remember` tool. If it forgets, a consolidation step distils them from its answer (episodic → semantic memory).

Facts surface automatically inside `impact`, `experts` and `history` results, so a brand-new session starts out knowing what earlier sessions learned.

The hard part is staleness. A fact about a function is only true until someone changes that function. So at ingest we store a fingerprint of each function's **AST** (a hash of `ast.dump(node)`, so reformatting doesn't count as a change). When a fact is saved, the `ABOUT` edge records the fingerprint it saw:

```cypher
MERGE (m)-[a:ABOUT]->(t) SET a.hash = t.hash
```

After a re-sync, a fact is stale when the code moved under it:

```cypher
MATCH (m:Fact)-[a:ABOUT]->(t)
RETURN m.text, a.hash <> t.hash AS stale
```

The agent is told to treat stale facts as leads to re-check, not truths to repeat. In the UI they get an amber "Stale: code changed" badge.

## Three agents, one shared graph

The PR reviewer is three agents run in sequence, and they don't pass Python objects to each other. They hand off through the graph:

1. RepoBrain maps the PR's diff hunks onto function line ranges, so it knows exactly which functions changed, and creates a `(:Task)-[:TARGETS]->(:Function)`.
2. The **impact agent** walks the call graph and writes a `(:Note)-[:FOR]->(:Task)`.
3. The **historian** reads that note *from the graph*, digs up the PRs and issues behind the changed code, and flags anything the PR might undo.
4. The **reviewer** reads both notes, picks human reviewers with `experts`, and writes the PR comment. A GitHub Action posts it.

On Flask's open PR #5918 ("automatic options as separate route"), the historian found PR #5917, which had just fixed `provide_automatic_options` override logic (Issue #5916), and PR #5496, which added the config option. That's exactly the history a reviewer of that PR needs.

## Seeing the path

"Show the path behind an answer" was the brief, so the UI streams it. While the agent works, each tool call appears as a chip and the graph grows live on the right, with new nodes flashing as they're found. In the finished answer, every `PR #`, `Issue #` and `@login` is a citation: hover it and the graph flies to that node. Expand "graph queries behind this answer" to see the exact Cypher and raw results.

All the tools are also served over **MCP** (`mcp_server.py`), so Claude Desktop, Cursor or any MCP client can query the same graph and memory.

## Things we learned about FalkorDB

- **`shortestPath` is directed-only** and has to sit in `WITH`/`RETURN`. For "how is this person connected to that function", paths zig-zag (`AUTHORED →`, `← CALLS`), so `connect` widens an undirected `[*k]` match one hop at a time and stops at the first hit. Because it stops early, the first path it finds is a shortest one.
- **Full-text indexes are great for agent search.** `db.idx.fulltext.queryNodes('PR', 'retries')` matched "retry" through stemming, which beats `CONTAINS` for natural-language questions.
- **`GRAPH.RO_QUERY` is a free safety net.** The agent's raw `cypher` tool uses it, so even a confused (or prompt-injected) model can't write to the graph through it.
- **`UNWIND $rows` batches** keep ingest fast. Flask's 831 functions, 983 calls and 200 PRs load into FalkorDB Cloud in about 30 seconds.
- **One client gotcha:** with redis-py 8, `FalkorDB.from_url` opened a RESP3 pool that dropped large replies partway through. Building the client with the constructor (RESP2) fixed it.

## Try it

- Live: https://repobrain-j9c4.onrender.com: pick `pallets/flask`, or ingest any public Python repo.
- Code, data model and every Cypher query: https://github.com/SanidhyaDhangar/repobrain

*Built by Sanidhya Dhangar and Aditya Sikarwar, with Claude Code as a coding assistant.*
