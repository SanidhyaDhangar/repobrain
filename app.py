"""HTTP API and the single-page UI. Run: uv run uvicorn app:app --reload"""
import hmac
import json
import os
import queue
import re
import threading
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from openai import APIError
from pydantic import BaseModel

import agent
import ingest
from graph import DBDown, Run, db, graph, repos

app = FastAPI(title="RepoBrain")
threading.Thread(target=db, daemon=True).start()  # connect (or boot the embedded fallback) before the first request
STATIC = Path(__file__).parent / "static"
POST_OWNERS = {o.strip().lower() for o in os.environ.get("POST_OWNERS", "").split(",") if o.strip()}
REVIEW_KEY = os.environ.get("REVIEW_KEY", "")
ingest_lock = threading.Lock()  # ponytail: one ingest at a time per process, a job queue if many users ingest at once


@app.exception_handler(APIError)
def llm_error(request, exc):
    return JSONResponse({"detail": f"LLM call failed: {exc.message}"}, status_code=502)


@app.exception_handler(DBDown)
def db_error(request, exc):
    return JSONResponse({"detail": f"FalkorDB error: {exc}"}, status_code=503)


def repo_name(raw: str) -> str:
    repo = raw.strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise HTTPException(400, "Use owner/name, e.g. pallets/flask")
    return repo


def sse(work):
    """Run work(emit) in a thread and stream each event it emits, then its result, as server-sent events."""
    events = queue.Queue()

    def run():
        try:
            events.put({"type": "done", **work(events.put)})
        except Exception as e:
            events.put({"type": "error", "detail": getattr(e, "detail", None) or getattr(e, "message", None) or str(e)})
        events.put(None)

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while (event := events.get()) is not None:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/repos")
def list_repos():
    try:
        names = repos()
    except DBDown:  # stay up for Render's health check; every other endpoint reports the error
        return []
    out = []
    for r in names:
        g = graph(r)
        row = g.ro_query("MATCH (r:Repo) RETURN r.status, r.synced_at, r.description, r.error, r.history_from LIMIT 1").result_set
        status, synced, desc, error, hist = row[0] if row else ("unknown", None, "", None, None)
        counts = dict(g.ro_query("MATCH (n) RETURN labels(n)[0], count(*)").result_set)
        out.append({"repo": r, "status": status, "synced_at": synced, "description": desc, "error": error,
                    "history_from": hist or r, "counts": counts})
    return out


class IngestReq(BaseModel):
    repo: str


def run_ingest(repo):
    try:
        ingest.ingest(repo)
    except Exception as e:
        graph(repo).query("MERGE (r:Repo {name: $repo}) SET r.status = 'error', r.error = $e", {"repo": repo, "e": str(e)[:300]})
    finally:
        ingest_lock.release()


@app.post("/api/ingest")
def start_ingest(req: IngestReq, tasks: BackgroundTasks):
    repo = repo_name(req.repo)
    if not ingest_lock.acquire(blocking=False):
        raise HTTPException(429, "Another repo is being ingested, try again in a minute")
    try:
        graph(repo).query("MERGE (r:Repo {name: $repo}) SET r.status = 'ingesting', r.error = null", {"repo": repo})
    except Exception:
        ingest_lock.release()
        raise
    tasks.add_task(run_ingest, repo)
    return {"repo": repo, "status": "ingesting"}


class AskReq(BaseModel):
    repo: str
    question: str
    session: str = "default"


@app.post("/api/ask")
def ask(req: AskReq):
    return agent.ask(repo_name(req.repo), req.question[:2000], session=req.session[:100])


@app.post("/api/ask/stream")
def ask_stream(req: AskReq):
    repo = repo_name(req.repo)
    return sse(lambda emit: agent.ask(repo, req.question[:2000], session=req.session[:100], emit=emit))


@app.get("/api/overview")
def overview(repo: str):
    """Starting picture: the most central functions, who calls them, and who wrote them."""
    run = Run(graph(repo_name(repo)))
    top = """CALL algo.pageRank('Function', 'CALLS') YIELD node, score WITH node, score WHERE NOT node.path CONTAINS 'test'
             WITH node ORDER BY score DESC LIMIT 12"""
    run.query(f"{top} MATCH p = (c:Function)-[:CALLS]->(node) WHERE NOT c.path CONTAINS 'test' RETURN p LIMIT 60")
    run.query(f"{top} MATCH p = (:Person)-[:AUTHORED]->(:PR)-[:MODIFIED]->(node) RETURN p LIMIT 40")
    return run.view()


@app.get("/api/memory")
def memory(repo: str):
    """What the agents have learned: Facts with what they're about and whether the code changed since."""
    g = graph(repo_name(repo))
    run = Run(g)
    run.query("MATCH p = (:Fact)-[:ABOUT]->() RETURN p LIMIT 150")
    facts = g.ro_query("""MATCH (m:Fact)-[a:ABOUT]->(t)
                          OPTIONAL MATCH (t)<-[:MODIFIED|TOUCHED]-(pr:PR) WHERE pr.merged_at > m.created_at
                          WITH m, collect(DISTINCT coalesce(t.qname, t.path, '@' + t.login, '#' + toString(t.number))) AS about,
                               max(CASE WHEN (a.hash IS NOT NULL AND a.hash <> t.hash) OR pr IS NOT NULL THEN 1 ELSE 0 END) AS stale
                          RETURN m.text, m.agent, m.session, m.created_at, about, stale = 1
                          ORDER BY m.created_at DESC LIMIT 100""").result_set
    counts = g.ro_query("""OPTIONAL MATCH (e:Episode) WITH count(e) AS episodes
                           OPTIONAL MATCH (t:Task) RETURN episodes, count(t) AS tasks""").result_set[0]
    return {"facts": [dict(zip(("text", "agent", "session", "created_at", "about", "stale"), f)) for f in facts],
            "episodes": counts[0], "tasks": counts[1], "graph": run.view()}


@app.get("/api/prs")
def open_prs(repo: str):
    """Open PRs to pick from in the review tab."""
    with ingest.gh() as c:
        r = c.get(f"/repos/{repo_name(repo)}/pulls", params={"state": "open", "per_page": 15})
    return [{"number": p["number"], "title": p["title"], "author": p["user"]["login"]} for p in r.json()] if r.is_success else []


class ReviewReq(BaseModel):
    repo: str
    pr: int
    post: bool = False
    key: str = ""  # must match REVIEW_KEY to post a comment


def run_review(req: ReviewReq, emit=None):
    repo = repo_name(req.repo)
    if req.post and (repo.split("/")[0].lower() not in POST_OWNERS
                     or not REVIEW_KEY or not hmac.compare_digest(req.key, REVIEW_KEY)):
        raise HTTPException(403, "Posting needs the review key and a repo owner listed in POST_OWNERS")
    with ingest.gh() as c:
        pr = c.get(f"/repos/{repo}/pulls/{req.pr}")
        if not pr.is_success:
            raise HTTPException(404, f"PR #{req.pr} not found in {repo}")
        files = c.get(f"/repos/{repo}/pulls/{req.pr}/files", params={"per_page": 100}).json()
        paths = [f["filename"] for f in files]
        funcs = graph(repo).ro_query("MATCH (f:Function) WHERE f.path IN $paths RETURN f.qname, f.path, f.line, f.end_line",
                                     {"paths": paths}).result_set
        # an open PR's diff is against the base branch, which is what the graph holds, so line ranges are exact
        changed = sorted({q for f in files for n in ingest.changed_lines(f.get("patch"))
                          for q, p, start, end in funcs if p == f["filename"] and start <= n <= end})
        if emit:
            emit({"type": "changed", "title": pr.json()["title"], "changed": changed, "files": paths})
        results = agent.review_pr(repo, req.pr, pr.json()["title"], pr.json()["body"], changed, paths, emit=emit)
        # a zero-width space after "@" keeps names readable but stops GitHub notifying people who never asked to review
        comment = re.sub(r"(?<![\w`])@(?=[A-Za-z0-9])", "@\u200b", results["reviewer"]["answer"])
        posted = None
        if req.post:
            body = f"{comment}\n\n---\n<sub>RepoBrain: impact, historian and reviewer agents reasoning over a FalkorDB graph of this repo.</sub>"
            posted = c.post(f"/repos/{repo}/issues/{req.pr}/comments", json={"body": body}).raise_for_status().json()["html_url"]
    return {"changed": changed, "agents": results, "comment": comment, "posted": posted}


@app.post("/api/review")
def review(req: ReviewReq):
    return run_review(req)


@app.post("/api/review/stream")
def review_stream(req: ReviewReq):
    return sse(lambda emit: run_review(req, emit))
