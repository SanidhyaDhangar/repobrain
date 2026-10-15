# Demo video script (about 3 minutes)

Record at 1440×900 or larger, browser zoom 100%, in a dark-mode browser. Open https://repobrain-j9c4.onrender.com about a minute early, because the free server sleeps and takes about 50 s to wake up. Free Groq answers take 10–30 s; cut the waiting in editing (keep the live chips appearing, cut the dead air).

Before recording, ask the team to reset demo memory so the memory story starts clean.

---

**0:00–0:15 · The problem** (show the home screen)
> "Every team has one person who just knows: what calls what, why it was built that way, and who to ask. AI agents don't. They see one file and forget everything when the session ends. RepoBrain gives them that context as a FalkorDB graph."

**0:15–0:35 · The graph** (point at the stats, then the graph on the right)
> "We ingested Flask: 831 functions, 200 merged PRs, the issues behind them, and 86 people, all in one FalkorDB graph. On the right: the most central code by PageRank, and who wrote it."
- Click one big node (e.g. `Scaffold.route`) and show the inspector with its GitHub link.

**0:35–1:15 · Track 3: Company brain** (Ask tab)
- Click the suggestion **"Why does full_dispatch_request look the way it does?"**
> "Watch the agent work: each graph query shows up live, and the graph grows as it explores."
- When the answer lands, hover **PR #5818** and **@davidism** so the graph flies to them.
> "Every claim is a citation. Hover one and you see it in the graph. These are the exact Cypher queries it ran." (expand the queries section briefly)

**1:15–1:45 · Track 1: Acting on connected data** (Ask tab)
- Ask: **"What breaks if I change full_dispatch_request?"**
> "A multi-hop blast radius over the call graph: callers, affected tests, open issues in the radius."

**1:45–2:15 · Track 2: Memory** (click **New session**, then the Memory tab)
> "New session: the chat history is gone, but what the agent learned is in the graph."
- Show the fact cards and their **About** chips (click one to focus the node).
- Back on Ask, ask: **"I want to change full_dispatch_request. What do we already know about it?"**
> "It uses the facts from the earlier session. Each fact stores an AST fingerprint of the code it's about. If that code changes, the fact turns stale and the agent re-checks it."

**2:15–2:50 · Multi-agent PR review** (PR review tab)
- Pick open PR **#5918**. Cut the waiting in editing.
> "Three agents: impact, historian and reviewer. They don't pass messages in code. Each one writes a Note node into the graph and the next one reads it."
- Show the historian finding PR #5917 and Issue #5916, then the final review comment.
- Cut to GitHub: the same comment posted automatically by the GitHub Action on our fork's PR.

**2:50–3:00 · Close**
> "RepoBrain: code, history, people and memory in one FalkorDB graph, with every answer traceable. It also runs as an MCP server, so any agent can use it. Links are below."
