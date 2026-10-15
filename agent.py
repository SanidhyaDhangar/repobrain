"""The RepoBrain agent: an LLM tool loop where every tool is a Cypher query against the repo's FalkorDB graph.

Memory lives in the same graph: each question is an Episode linked to the code it used, durable lessons are
Facts linked to the entities they are about, and agents hand work to each other through Task/Note nodes.
"""
import json
import os
import re
from datetime import datetime, timezone

from openai import BadRequestError, OpenAI, RateLimitError

from graph import Run, graph

BASE = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
KEY = os.environ.get("LLM_API_KEY", "missing")
# Primary, then a fallback used when the primary is rate limited. On Groq's free tier each model has its own
# tokens-per-minute bucket, so falling back to a second model keeps answers coming.
PROVIDERS = [(OpenAI(base_url=BASE, api_key=KEY, max_retries=1), os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")),
             (OpenAI(base_url=os.environ.get("LLM2_BASE_URL", BASE), api_key=os.environ.get("LLM2_API_KEY", KEY), max_retries=4),
              os.environ.get("LLM2_MODEL", "openai/gpt-oss-20b"))]


def chat(usage, **kw):
    """One chat completion, falling through the providers on rate limits. Adds tokens spent to usage[0]."""
    for i, (client, model) in enumerate(PROVIDERS):
        try:
            r = client.chat.completions.create(model=model, temperature=0, **kw)
            usage[0] += r.usage.total_tokens if r.usage else 0
            return r.choices[0].message
        except RateLimitError:
            if i == len(PROVIDERS) - 1:
                raise


def tidy(text):
    return re.sub(r"\s*【[^】]*】", "", text or "").strip()


def match(p: str) -> str:
    """Cypher condition: node t is the function, file, person, PR or issue named by expression p."""
    return (f"(t:Function AND (t.qname = {p} OR t.name = {p} OR t.qname ENDS WITH {p})) OR (t:File AND t.path ENDS WITH {p})"
            f" OR (t:Person AND t.login = {p}) OR ((t:PR OR t:Issue) AND toString(t.number) = {p})")


def clean(name: str) -> str:
    return name.strip().strip("()`'\"@#").removeprefix("PR #").removeprefix("Issue #")


def words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text)) or "_"


# ---------- tools: each one is a few Cypher queries ----------

def search(run, ctx, text):
    return run.query(
        """MATCH (t:Function) WHERE toLower(t.qname) CONTAINS toLower($text)
             RETURN t AS hit, 'lines ' + toString(t.line) + '-' + toString(t.end_line) + ': ' + t.doc AS info LIMIT 8
           UNION MATCH (t:File) WHERE toLower(t.path) CONTAINS toLower($text) RETURN t AS hit, toString(t.loc) + ' lines' AS info LIMIT 5
           UNION CALL db.idx.fulltext.queryNodes('PR', $words) YIELD node RETURN node AS hit, 'merged ' + node.merged_at AS info LIMIT 6
           UNION CALL db.idx.fulltext.queryNodes('Issue', $words) YIELD node RETURN node AS hit, node.state AS info LIMIT 6""",
        {"text": text.strip(), "words": words(text)})


def facts_about(run, name):
    """Facts learned earlier about an entity. A fact is stale when the code's AST fingerprint differs from the one
    recorded when the fact was learned, or when a PR changed it since."""
    return run.query(f"""MATCH (t) WHERE {match('$name')} MATCH (m:Fact)-[a:ABOUT]->(t)
                         OPTIONAL MATCH (t)<-[:MODIFIED|TOUCHED]-(pr:PR) WHERE pr.merged_at > m.created_at
                         WITH m, a, t, collect(pr.number) AS prs
                         RETURN m.text AS fact, m.agent AS by, m.created_at AS learned,
                                (a.hash IS NOT NULL AND a.hash <> t.hash) OR size(prs) > 0 AS stale, prs AS changed_by_prs
                         ORDER BY learned DESC LIMIT 8""", {"name": clean(name)})


def with_memory(run, name, out):
    """Memory surfaces wherever an entity is looked at, not only when the model remembers to call recall."""
    facts = facts_about(run, name)
    return out if facts == "(no results)" else f"{out}\n\nFrom memory (earlier sessions):\n{facts}"


def impact(run, ctx, name):
    p = {"name": clean(name)}
    paths = run.query(f"""MATCH (t) WHERE {match('$name')} MATCH p = (c:Function)-[:CALLS*1..3]->(t)
                          WHERE NOT c.path CONTAINS 'test' RETURN p LIMIT 20""", p)
    summary = run.query(f"""MATCH (t) WHERE {match('$name')} MATCH (c:Function)-[:CALLS*1..3]->(t)
                            RETURN c.path CONTAINS 'test' AS is_test, count(DISTINCT c) AS callers,
                                   collect(DISTINCT c.path)[..12] AS files""", p)
    issues = run.query(f"""MATCH (t) WHERE {match('$name')} MATCH (c:Function)-[:CALLS*0..3]->(t)
                           MATCH p = (i:Issue {{state: 'OPEN'}})-[:MENTIONS]->(c) RETURN DISTINCT p LIMIT 5""", p)
    return with_memory(run, name, f"Call paths into it (non-test):\n{paths}\n\nCallers by kind:\n{summary}\n\n"
                                  f"Open issues touching the blast radius:\n{issues}")


def experts(run, ctx, name):
    return with_memory(run, name, run.query(f"""MATCH (t) WHERE {match('$name')}
                         MATCH p = (u:Person)-[:AUTHORED|REVIEWED]->(pr:PR)-[:MODIFIED|TOUCHED]->(t)
                         WITH u, count(DISTINCT pr) AS prs, max(pr.merged_at) AS last_change, collect(p)[0] AS example
                         RETURN u.login AS person, prs, last_change, example ORDER BY prs DESC LIMIT 6""",
                     {"name": clean(name)}))


def history(run, ctx, name):
    return with_memory(run, name, run.query(f"""MATCH (t) WHERE {match('$name')}
                         MATCH p = (a:Person)-[:AUTHORED]->(pr:PR)-[:MODIFIED|TOUCHED]->(t)
                         OPTIONAL MATCH q = (pr)-[:FIXES]->(i:Issue)
                         RETURN pr.number AS pr, a.login AS pr_author, p, q, pr.merged_at AS merged, left(pr.body, 500) AS pr_text, left(i.body, 300) AS issue_text
                         ORDER BY merged DESC LIMIT 6""", {"name": clean(name)}))


def critical(run, ctx):
    return run.query("""CALL algo.pageRank('Function', 'CALLS') YIELD node, score
                        WITH node, score WHERE NOT node.path CONTAINS 'test'
                        WITH node, score ORDER BY score DESC LIMIT 10
                        OPTIONAL MATCH (node)<-[:MODIFIED]-(:PR)<-[:AUTHORED]-(u:Person)
                        RETURN node, score, collect(DISTINCT u.login) AS authors ORDER BY score DESC""")


def connect(run, ctx, a, b):
    p = {"a": clean(a), "b": clean(b)}
    for k in range(1, 6):  # FalkorDB's shortestPath is directed-only, so widen an undirected search hop by hop
        out = run.query(f"""MATCH (t) WHERE {match('$a')} WITH t AS a LIMIT 1
                            MATCH (t) WHERE {match('$b')} WITH a, t AS b LIMIT 1
                            MATCH p = (a)-[*{k}]-(b) RETURN p LIMIT 1""", p)
        if out != "(no results)":
            return out
    return "No connection within 5 hops."


def recall(run, ctx, name):
    p = {"name": clean(name), "words": words(name)}
    facts = facts_about(run, name)
    if facts == "(no results)":
        facts = run.query("""CALL db.idx.fulltext.queryNodes('Fact', $words) YIELD node
                             RETURN node.text AS fact, node.agent AS by, node.created_at AS learned LIMIT 5""", p)
    past = run.query(f"""MATCH (t) WHERE {match('$name')} MATCH (e:Episode)-[:USED]->(t)
                         RETURN e.question AS asked, left(e.answer, 300) AS answered, e.created_at AS at
                         ORDER BY at DESC LIMIT 3""", p)
    return f"Facts (stale: true means the code changed after the fact was learned):\n{facts}\n\nPast questions about it:\n{past}"


def remember(run, ctx, fact, about):
    return run.query(f"""CREATE (m:Fact {{text: $fact, created_at: $now, agent: $agent, session: $session}})
                         WITH m UNWIND $about AS x MATCH (t) WHERE {match('x')}
                         MERGE (m)-[a:ABOUT]->(t) SET a.hash = t.hash RETURN m, t""",
                     {"fact": fact, "about": [clean(a) for a in about], "now": now(), **ctx}, write=True)


def task_notes(run, ctx):
    return run.query("""MATCH (n:Note)-[:FOR]->(t:Task {key: $task}) RETURN n.agent AS agent, left(n.text, 1400) AS note
                        ORDER BY n.created_at""", {"task": ctx.get("task", "")})


def cypher(run, ctx, query):
    return run.query(query)  # read-only: Run uses ro_query unless write=True


SCHEMA = """(:File {path, loc, test})-[:DEFINES]->(:Function {qname, name, path, line, end_line, doc})
(:Function)-[:CALLS]->(:Function), (:File)-[:IMPORTS]->(:File)
(:Person {login})-[:AUTHORED|REVIEWED]->(:PR {number, title, body, url, merged_at})
(:PR)-[:TOUCHED]->(:File), (:PR)-[:MODIFIED]->(:Function), (:PR)-[:FIXES]->(:Issue {number, title, body, state, created_at})
(:Person)-[:OPENED]->(:Issue), (:Issue)-[:MENTIONS]->(:Function|File)
(:Fact {text, agent, created_at})-[:ABOUT]->(any), (:Episode {question, answer, agent, session, created_at})-[:USED]->(any)"""

NAME = "function name, Class.method, file path, @login, or PR/issue number"
TOOLS = {
    "search": (search, "Find functions, files, PRs and issues by keyword. Use when you don't know exact names.", {"text": "keywords"}),
    "impact": (impact, "Blast radius: everything that calls this function (3 hops), affected tests, open issues in the radius.", {"name": NAME}),
    "experts": (experts, "Who knows this code: people ranked by PRs they authored or reviewed that changed it. Try the file if a function has none.", {"name": NAME}),
    "history": (history, "Why it is the way it is: PRs that changed it, newest first, with the issues they fixed and their text.", {"name": NAME}),
    "critical": (critical, "Most central functions by PageRank over the call graph, with who has ever authored changes to them (knowledge silos).", {}),
    "connect": (connect, "Shortest chain of relationships linking two entities (e.g. a person and a function).", {"a": NAME, "b": NAME}),
    "recall": (recall, "Long-term memory: facts learned earlier about an entity (flagged if code changed since) and past questions.", {"name": NAME}),
    "remember": (remember, "Save a durable, non-obvious fact (owner, reason for a decision, risk) linked to the entities it is about.",
                 {"fact": "one-sentence fact with its evidence", "about": ["entity names"]}),
    "task_notes": (task_notes, "Read what earlier agents on this task wrote.", {}),
    "cypher": (cypher, f"Run a read-only Cypher query on the graph. Schema:\n{SCHEMA}", {"query": "Cypher"}),
}


def spec(name):
    _, desc, params = TOOLS[name]
    props = {k: {"type": "array", "items": {"type": "string"}, "description": v[0]} if isinstance(v, list)
             else {"type": "string", "description": v} for k, v in params.items()}
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(params)}}}


def now():
    return datetime.now(timezone.utc).isoformat()


SYSTEM = """You are RepoBrain, an engineering agent for the GitHub repo {repo}. Its code, PR history, issues, people
and your own long-term memory are a FalkorDB graph you query through tools. {role}
Rules:
- Answer only from tool results. Never invent names, numbers or reasons; say what the graph does not show.
  Only say someone authored or reviewed a PR if a tool result shows that exact Person-AUTHORED/REVIEWED->PR link.
- Tool results may include "From memory" facts from earlier sessions: use them. If a fact has stale: true, the code
  changed after it was learned: say so, re-check it with the other tools, and `remember` the corrected fact.
- Cite evidence inline as plain text: PR #N, Issue #N, @login, file paths, Class.method names. No 【】 markers.
- Before your final answer, if you found an owner, the reason behind a design, or a risk, save it once with `remember`.
- Search once, then pass the exact qname to history/experts/impact. Use `cypher` only when no other tool fits.
- Be concise: the answer first, then a few evidence bullets."""

OUTPUT = {
    "analyst": "a concise answer first, then evidence bullets citing PR/issue numbers, @logins and function names.",
    "impact": "a risk assessment under 150 words: what calls the changed code, affected tests, open issues.",
    "historian": "under 200 words: the past PRs and issues behind the changed code and their reasons, flagging anything "
                 "this PR might undo or re-break.",
    "reviewer": "the final PR comment in GitHub markdown with sections: Risk (one line: low/medium/high and why), "
                "Blast radius, History to keep in mind (from the historian's note), Suggested reviewers (2-3, with why), "
                "Test checklist.",
}

ROLES = {
    "analyst": ("Answer the user's question about the codebase and the team behind it.", list(TOOLS)),
    "impact": ("You are the IMPACT agent on a PR review. Call `impact` on the 1-3 most important changed non-test "
               f"functions (and `critical` if useful). Then write {OUTPUT['impact']}", ["impact", "critical"]),
    "historian": ("You are the HISTORIAN agent on a PR review. Read the impact agent's note with `task_notes`, call "
                  f"`history` on the 1-3 most important changed functions. Then write {OUTPUT['historian']}",
                  ["task_notes", "history"]),
    "reviewer": ("You are the REVIEWER agent on a PR review. Read the other agents' notes with `task_notes` (once), call "
                 f"`experts` on the 1-2 main changed files. Then write {OUTPUT['reviewer']}", ["task_notes", "experts"]),
}


def ask(repo, question, session="default", agent="analyst", task=None, max_steps=8):
    g = graph(repo)
    run = Run(g)
    role, tool_names = ROLES[agent]
    ctx = {"agent": agent, "session": session, **({"task": task} if task else {})}
    msgs = [{"role": "system", "content": SYSTEM.format(repo=repo, role=role)}]
    # persistent conversation history: earlier turns of this session come back out of the graph
    prev = g.ro_query("""MATCH (e:Episode {session: $s}) RETURN e.question, e.answer ORDER BY e.created_at DESC LIMIT 4""",
                      {"s": session}).result_set
    for q, a in reversed(prev):
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": question})

    steps, answer, usage = [], "", [0]
    for _ in range(max_steps):
        try:
            m = chat(usage, messages=msgs, tools=[spec(t) for t in tool_names])
        except BadRequestError as e:  # the model sometimes emits a malformed tool call; tell it and retry
            msgs.append({"role": "user", "content": f"Your last tool call was malformed ({str(e)[:200]}). Try again."})
            continue
        if not m.tool_calls:
            answer = tidy(m.content)
            if answer:
                break
            msgs.append({"role": "user", "content": "Write your answer now."})  # gpt-oss sometimes returns nothing
            continue
        msgs.append({"role": "assistant", "content": m.content or "",
                     "tool_calls": [tc.model_dump(include={"id", "type", "function"}) for tc in m.tool_calls]})
        for tc in m.tool_calls:
            name, args, start = tc.function.name, {}, len(run.log)
            try:
                args = json.loads(tc.function.arguments or "{}")
                if name not in tool_names:
                    raise ValueError(f"unknown tool {name}")
                out = TOOLS[name][0](run, ctx, **args)
            except Exception as e:  # errors go back to the model so it can fix its call
                out = f"ERROR: {e}"
            steps.append({"tool": name, "args": args, "result": out[:3000], "cypher": run.log[start:][-4:]})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out[:3000]})
    if not answer:  # out of steps: a writer with no tools in sight answers from the evidence gathered so far
        evidence = "\n\n".join(m["content"] for m in msgs if m["role"] == "tool")[-12000:]
        try:
            answer = tidy(chat(usage, messages=[
                {"role": "system", "content": f"You write about the GitHub repo {repo} using only the evidence given. "
                                              f"You have no tools. Write {OUTPUT[agent]}"},
                {"role": "user", "content": f"{question}\n\nEvidence from the repo's knowledge graph:\n{evidence}"}]).content)
        except BadRequestError:
            pass
        answer = answer or "I couldn't finish in time. Here is what the graph returned:\n\n" + evidence[:3000]

    used = [i for i, n in run.nodes.items() if n.labels[0] in ("Function", "File", "PR", "Issue", "Person")]
    g.query("""CREATE (e:Episode {question: $q, answer: $a, session: $s, agent: $agent, created_at: $now})
               WITH e UNWIND $used AS id MATCH (n) WHERE id(n) = id MERGE (e)-[:USED]->(n)""",
            {"q": question, "a": answer, "s": session, "agent": agent, "now": now(), "used": used})
    if agent in ("analyst", "reviewer") and not any(s["tool"] == "remember" for s in steps):
        consolidate(run, ctx, question, answer, usage)
    return {"answer": answer, "steps": steps, "graph": run.view(), "tokens": usage[0]}


CONSOLIDATE = """From this Q&A about a codebase, extract at most 2 durable, non-obvious facts worth remembering in future
sessions: who owns or knows some code, why a design is the way it is, or a risk. Each fact must be stated in the answer
with its evidence (merged PR/issue numbers) and be about the existing code, its history or its people; never record
what a pending, unmerged PR under review does. Return JSON {{"facts": [{{"fact": "...", "about": ["function, file, @login or PR number"]}}]}},
or {{"facts": []}} if nothing qualifies.

Q: {q}

A: {a}"""


def consolidate(run, ctx, question, answer, usage):
    """Episodic -> semantic memory: distil durable Facts from the answer, linked to the entities they are about."""
    try:
        m = chat(usage, response_format={"type": "json_object"},
                 messages=[{"role": "user", "content": CONSOLIDATE.format(q=question, a=answer)}])
        for f in json.loads(m.content).get("facts", [])[:2]:
            remember(run, ctx, f["fact"], f["about"])
    except Exception:  # memory is best-effort; never fail the answer over it
        pass


def review_pr(repo, number, title, body, changed, paths):
    """Three agents review a PR, handing off through a Task node in the graph rather than in Python memory."""
    task = f"pr-{number}@{now()}"  # one Task per review run; earlier runs stay as history
    g = graph(repo)
    g.query("""MERGE (t:Task {key: $task}) SET t.kind = 'pr_review', t.pr = $n, t.title = $title, t.created_at = $now
               WITH t UNWIND $changed AS q MATCH (f:Function {qname: q}) MERGE (t)-[:TARGETS]->(f)""",
            {"task": task, "n": number, "title": title, "changed": changed, "now": now()})
    code = [q for q in changed if "test" not in q.split("::")[0]]
    brief = (f"PR #{number} '{title}'.\nDescription: {(body or '(none)')[:800]}\nChanged files: {', '.join(paths)}\n"
             f"Changed functions: {', '.join(code[:12]) or '(none in Python code)'}\n"
             f"Changed test functions: {len(changed) - len(code)}")
    results = {}
    for agent in ("impact", "historian", "reviewer"):
        out = ask(repo, brief, session=f"{task}:{agent}", agent=agent, task=task, max_steps=5)
        g.query("""MATCH (t:Task {key: $task}) CREATE (n:Note {agent: $agent, text: $text, created_at: $now})-[:FOR]->(t)""",
                {"task": task, "agent": agent, "text": out["answer"], "now": now()})
        results[agent] = out
    return results
