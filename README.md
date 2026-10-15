# RepoBrain

**An engineering agent that knows your codebase the way your most senior engineer does: what calls what, why it was built that way, and who to ask.**

Point RepoBrain at any public GitHub repo. It builds one FalkorDB graph from the code (files, functions, calls, imports), its history (merged PRs, the issues they fixed, open issues) and the people behind it. Then an LLM agent answers questions by traversing that graph. Every answer shows the exact Cypher queries and the subgraph behind it.

Built for **Graph Hacks: Context for AI Agents** (WeMakeDevs × FalkorDB, Oct 2026). Entered in all three tracks:

| Track | What RepoBrain does |
|---|---|
| Agents that act on connected data | "What breaks if I change X?": a multi-hop blast radius over the call graph, PageRank for critical code, and a 3-agent PR review that **posts its findings as a PR comment** |
| Agent memory & coordination | Episodes and Facts persist in the graph across sessions and are linked to the code they're about. Facts are **flagged stale** when a later PR changes that code. Agents hand work off through `Task`/`Note` nodes, and every repo is its own isolated graph |
| Company brain | "Who knows this code?", "Why is it like this?": traces code → PR → issue → person, finds knowledge silos, and cites every source |

## Graph data model

```
(:File {path, loc, test})-[:DEFINES]->(:Function {qname, name, path, line, end_line, doc})
(:Function)-[:CALLS]->(:Function)          (:File)-[:IMPORTS]->(:File)
(:Person {login})-[:AUTHORED|REVIEWED]->(:PR {number, title, body, url, merged_at})
(:PR)-[:TOUCHED]->(:File)                  (:PR)-[:MODIFIED]->(:Function)
(:PR)-[:FIXES]->(:Issue {number, title, body, state, created_at})
(:Person)-[:OPENED]->(:Issue)              (:Issue)-[:MENTIONS]->(:Function|:File)

# memory and coordination
(:Episode {question, answer, session, agent, created_at})-[:USED]->(any entity)
(:Fact {text, agent, created_at})-[:ABOUT]->(any entity)
(:Task {key, pr, title})-[:TARGETS]->(:Function)     (:Note {agent, text})-[:FOR]->(:Task)
(:Repo {name, status, synced_at})
```

Each repo lives in its own graph (`owner__name`) inside one FalkorDB instance.

## Agent tools: each one is Cypher

All of these are in [`agent.py`](agent.py).

| Tool | Graph work |
|---|---|
| `search` | `CONTAINS` on functions/files + **full-text index** (`db.idx.fulltext.queryNodes`) on PRs and issues |
| `impact` | `(:Function)-[:CALLS*1..3]->(target)` blast radius, split into tests and non-tests, plus open issues that mention anything in the radius |
| `experts` | `(:Person)-[:AUTHORED\|REVIEWED]->(:PR)-[:MODIFIED\|TOUCHED]->(target)`, ranked |
| `history` | `(:PR)-[:MODIFIED]->(target)` + `(:PR)-[:FIXES]->(:Issue)`, with the text explaining *why* |
| `critical` | `CALL algo.pageRank('Function', 'CALLS')` + who has ever authored changes (knowledge silos) |
| `connect` | Shortest undirected chain between any two entities. FalkorDB's `shortestPath` is directed-only, so it widens a `[*k]` search one hop at a time |
| `recall` / `remember` | Read/write `Fact` nodes linked to entities. A fact is stale when `(:PR)-[:MODIFIED]->(entity)` has `merged_at > fact.created_at` |
| `task_notes` | Read the notes earlier agents left on the shared `Task` |
| `cypher` | Read-only (`GRAPH.RO_QUERY`) escape hatch, with the schema above in its description |

## Run it locally

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), a FalkorDB instance, and a [Groq](https://console.groq.com) API key (any OpenAI-compatible endpoint works).

```bash
docker run -p 6379:6379 -it --rm falkordb/falkordb:latest   # or use FalkorDB Cloud

cp .env.example .env    # then fill in the values
uv run --env-file .env python ingest.py pallets/flask        # ~1 minute
uv run --env-file .env uvicorn app:app --port 8000           # open http://localhost:8000
uv run python test_ingest.py                                  # parser self-check
```

| Env var | Meaning |
|---|---|
| `FALKORDB_URL` | `falkor://user:pass@host:port` (`falkors://` for TLS) |
| `LLM_API_KEY` | Groq API key |
| `LLM_MODEL` | defaults to `openai/gpt-oss-120b` |
| `LLM_BASE_URL` | defaults to Groq's OpenAI-compatible endpoint |
| `GITHUB_TOKEN` | any token; raises the GitHub API rate limit |
| `POST_OWNERS` | comma-separated repo owners the PR reviewer may comment on |
| `MAX_PRS` | merged PRs to ingest (default 200) |

## Known limits

- Only Python code is parsed (stdlib `ast`). Calls are resolved by name: same file, then imported files, then a unique match anywhere in the repo.
- PR → function links come from function names that appear in the diff, not from line ranges.

## AI assistance disclosure

Built with help from Claude Code (Anthropic) as a coding assistant, as allowed by the hackathon rules. The team designed and reviewed the graph model, queries and architecture.
