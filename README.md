# RepoBrain

**An engineering agent that knows your codebase the way your most senior engineer does: what calls what, why it was built that way, and who to ask.**

Point RepoBrain at any public GitHub repo. It builds one FalkorDB graph from the code (files, functions, calls, imports), its history (merged PRs, the issues they fixed, open issues), the people behind it, and everything its agents learn. LLM agents then answer questions and review PRs by traversing that graph. Every answer shows the exact Cypher queries and the subgraph behind it.

**Live demo:** https://repobrain-j9c4.onrender.com (free tier: the first request after idle takes about a minute to wake up)

**Demo video:** https://youtu.be/vztPZL29JSs

![RepoBrain answering "What breaks if I change full_dispatch_request, and who should review it?" with the live trace of graph tools on the left and the evidence subgraph on the right](docs/img/desktop.jpg)

*Each chip is a Cypher tool call the agent made. The graph grows as it explores, and hovering a cited PR or person focuses that node. On phones the graph becomes its own tab:*

<p><img src="docs/img/phone-answer.jpg" width="260" alt="RepoBrain answer on a phone"> <img src="docs/img/phone-graph.jpg" width="260" alt="Evidence graph tab on a phone"></p>

Built for **Graph Hacks: Context for AI Agents** (WeMakeDevs × FalkorDB, Oct 2026). Entered in all three tracks:

| Track | What RepoBrain does |
|---|---|
| Agents that act on connected data | "What breaks if I change X?": multi-hop blast radius over the call graph, PageRank for critical code, graph tools exposed over **MCP**, and a 3-agent PR review that **posts its findings as a PR comment** |
| Agent memory & coordination | Episodes (every Q&A) and Facts (distilled lessons) persist in the graph across sessions, linked to the code they're about. A fact is **flagged stale when that code's AST fingerprint changes**. Impact → historian → reviewer agents hand off through `Task`/`Note` nodes. Every repo is its own isolated graph |
| Company brain | "Who knows this code?", "Why is it like this?": traces code → PR → issue → person, finds knowledge silos, and cites every source |

## How the graph does the work

```
 GitHub repo ──► ingest.py ──► FalkorDB (one graph per repo) ◄──► agent.py (LLM tool loop, tools = Cypher)
 (code + PRs +                  code ∪ history ∪ people ∪ memory        ▲            │
  issues + people)                                                      │            ▼
                                                     mcp_server.py ─────┘      app.py + static/index.html
                                                     (any MCP client)          (chat, live subgraph, PR review, memory)
```

Without the graph there is no product: every agent tool is a Cypher query, memory is graph nodes, and agent handoffs are graph nodes.

## Graph data model

```
(:File {path, loc, test, hash})-[:DEFINES]->(:Function {qname, name, path, line, end_line, doc, hash})
(:Function)-[:CALLS]->(:Function)          (:File)-[:IMPORTS]->(:File)
(:Person {login})-[:AUTHORED|REVIEWED]->(:PR {number, title, body, url, merged_at})
(:PR)-[:TOUCHED]->(:File)                  (:PR)-[:MODIFIED]->(:Function)
(:PR)-[:FIXES]->(:Issue {number, title, body, state, created_at})
(:Person)-[:OPENED]->(:Issue)              (:Issue)-[:MENTIONS]->(:Function|:File)

# memory and coordination
(:Episode {question, answer, session, agent, created_at})-[:USED]->(any entity)
(:Fact {text, agent, session, created_at})-[:ABOUT {hash}]->(any entity)
(:Task {key, pr, title})-[:TARGETS]->(:Function)     (:Note {agent, text})-[:FOR]->(:Task)
(:Repo {name, status, synced_at, history_from})
```

- Each repo lives in its own graph (`owner__name`) inside one FalkorDB instance.
- `hash` is a fingerprint of the function's or file's AST, so formatting doesn't count as a change but code does.
- A fork has no PR history of its own, so it inherits its upstream's history.

## Agent tools: each one is Cypher

All of these are in [`agent.py`](agent.py) and are also served over MCP by [`mcp_server.py`](mcp_server.py).

| Tool | Graph work |
|---|---|
| `search` | `CONTAINS` on functions/files + **full-text index** (`db.idx.fulltext.queryNodes`) on PRs and issues |
| `impact` | `(:Function)-[:CALLS*1..3]->(target)` blast radius, split into tests and non-tests, plus open issues that mention anything in the radius |
| `experts` | `(:Person)-[:AUTHORED\|REVIEWED]->(:PR)-[:MODIFIED\|TOUCHED]->(target)`, ranked |
| `history` | `(:Person)-[:AUTHORED]->(:PR)-[:MODIFIED]->(target)` + `(:PR)-[:FIXES]->(:Issue)`, with the text explaining *why* |
| `critical` | `CALL algo.pageRank('Function', 'CALLS')` + who has ever authored changes (knowledge silos) |
| `connect` | Shortest undirected chain between any two entities. FalkorDB's `shortestPath` is directed-only, so it widens a `[*k]` search one hop at a time |
| `recall` / `remember` | Read/write `Fact` nodes. `remember` stores the target's `hash` on the `ABOUT` edge; a fact is stale when `a.hash <> t.hash` or a later PR modified the target |
| `task_notes` | Read the notes earlier agents left on the shared `Task` |
| `cypher` | Read-only (`GRAPH.RO_QUERY`) escape hatch, with the schema above in its description |

Facts also come back automatically inside `impact`, `experts` and `history` results, so every agent sees what earlier sessions learned. After each answer, any durable facts the agent didn't save itself are distilled into `Fact` nodes (episodic → semantic memory).

## PR review: three agents, one shared graph

`POST /api/review` maps the PR's diff hunks onto function line ranges in the graph, then runs three agents in sequence. Each agent reads the others' work from the graph, not from Python memory:

1. **Impact agent**: blast radius (`impact`, `critical`), then writes a `Note` on the `Task`.
2. **Historian**: reads the notes, then `history` on the changed code, flagging anything the PR might undo.
3. **Reviewer**: reads both notes, picks human reviewers with `experts`, and writes the PR comment, which is posted to GitHub when enabled.

See it on a real pull request: [SanidhyaDhangar/flask#1](https://github.com/SanidhyaDhangar/flask/pull/1), where the GitHub Action in [`examples/repobrain-review.yml`](examples/repobrain-review.yml) posted the review automatically.

## Run it locally

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), a FalkorDB instance, and a [Groq](https://console.groq.com) API key (any OpenAI-compatible endpoint works).

```bash
docker run -p 6379:6379 -it --rm falkordb/falkordb:latest   # or use FalkorDB Cloud

cp .env.example .env    # then fill in the values
uv run --env-file .env python ingest.py pallets/flask        # ~1 minute
uv run --env-file .env uvicorn app:app --port 8000           # open http://localhost:8000
uv run python test_ingest.py                                  # parser self-check
```

### Use it from Claude Desktop, Cursor or any MCP client

```json
{ "mcpServers": { "repobrain": {
    "command": "uv", "args": ["run", "--directory", "/path/to/repobrain", "--env-file", ".env", "python", "mcp_server.py"] } } }
```

| Env var | Meaning |
|---|---|
| `FALKORDB_URL` | `falkor://user:pass@host:port` (`falkors://` for TLS). If it is unreachable on Linux, the app starts an embedded FalkorDB (Redis 8 from conda-forge + the module from `falkordblite`) preloaded with `snapshot.rdb`; the live demo runs this way |
| `LLM_API_KEY` | Groq API key |
| `LLM_MODEL` / `LLM2_MODEL` | primary model (default `openai/gpt-oss-120b`), fallback when rate limited (default `openai/gpt-oss-20b`) |
| `LLM_BASE_URL` / `LLM2_BASE_URL` / `LLM2_API_KEY` | OpenAI-compatible endpoints; default to Groq and the primary key |
| `GITHUB_TOKEN` | any token; raises the GitHub API rate limit, and is needed to post PR comments |
| `POST_OWNERS`, `REVIEW_KEY` | posting a PR comment needs the repo owner listed in `POST_OWNERS` and the request's `key` matching `REVIEW_KEY` |
| `MAX_PRS` | merged PRs to ingest (default 200) |

## Known limits

- Only Python code is parsed (stdlib `ast`). Calls are resolved by name: same file, then imported files, then a unique match anywhere in the repo.
- For historical PRs, links to functions come from names in hunk headers and changed `def` lines, because line numbers drift after a merge. Open PRs under review use exact line ranges.

## AI assistance disclosure

Built with help from Claude Code (Anthropic) as a coding assistant, as allowed by the hackathon rules. The team designed and reviewed the graph model, queries and architecture.
