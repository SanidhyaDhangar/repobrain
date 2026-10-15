"""HTTP API and the single-page UI. Run: uv run uvicorn app:app --reload"""
import os
import re
import threading
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import agent
import ingest
from graph import Run, graph, repos

app = FastAPI(title="RepoBrain")
STATIC = Path(__file__).parent / "static"
POST_OWNERS = {o.strip().lower() for o in os.environ.get("POST_OWNERS", "").split(",") if o.strip()}
ingest_lock = threading.Lock()  # ponytail: one ingest at a time per process, a job queue if many users ingest at once


def repo_name(raw: str) -> str:
    repo = raw.strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise HTTPException(400, "Use owner/name, e.g. pallets/flask")
    return repo


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/repos")
def list_repos():
    out = []
    for r in repos():
        row = graph(r).ro_query("MATCH (r:Repo) RETURN r.status, r.synced_at, r.description, r.error LIMIT 1").result_set
        status, synced, desc, error = row[0] if row else ("unknown", None, "", None)
        out.append({"repo": r, "status": status, "synced_at": synced, "description": desc, "error": error})
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


@app.get("/api/overview")
def overview(repo: str):
    """Starting picture: the most central functions, who calls them, and who wrote them."""
    run = Run(graph(repo_name(repo)))
    top = """CALL algo.pageRank('Function', 'CALLS') YIELD node, score WITH node, score WHERE NOT node.path CONTAINS 'test'
             WITH node ORDER BY score DESC LIMIT 12"""
    run.query(f"{top} MATCH p = (:Function)-[:CALLS]->(node) RETURN p LIMIT 60")
    run.query(f"{top} MATCH p = (:Person)-[:AUTHORED]->(:PR)-[:MODIFIED]->(node) RETURN p LIMIT 40")
    return run.view()


class ReviewReq(BaseModel):
    repo: str
    pr: int
    post: bool = False


@app.post("/api/review")
def review(req: ReviewReq):
    repo = repo_name(req.repo)
    if req.post and repo.split("/")[0].lower() not in POST_OWNERS:
        raise HTTPException(403, "Posting comments is only enabled for repos listed in POST_OWNERS")
    with ingest.gh() as c:
        pr = c.get(f"/repos/{repo}/pulls/{req.pr}")
        if not pr.is_success:
            raise HTTPException(404, f"PR #{req.pr} not found in {repo}")
        files = c.get(f"/repos/{repo}/pulls/{req.pr}/files", params={"per_page": 100}).json()
        paths = [f["filename"] for f in files]
        funcs = graph(repo).ro_query("MATCH (f:Function) WHERE f.path IN $paths RETURN f.qname, f.name, f.path",
                                     {"paths": paths}).result_set
        changed = [q for f in files for q in ingest.changed_functions(
            f.get("patch"), [{"qname": q, "name": n} for q, n, p in funcs if p == f["filename"]])]
        results = agent.review_pr(repo, req.pr, pr.json()["title"], changed)
        comment = results["reviewer"]["answer"]
        posted = None
        if req.post:
            body = f"{comment}\n\n---\n<sub>RepoBrain: impact, historian and reviewer agents reasoning over a FalkorDB graph of this repo.</sub>"
            posted = c.post(f"/repos/{repo}/issues/{req.pr}/comments", json={"body": body}).raise_for_status().json()["html_url"]
    return {"changed": changed, "agents": results, "comment": comment, "posted": posted}
