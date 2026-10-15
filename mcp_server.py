"""RepoBrain's graph tools as an MCP server, so any MCP client (Claude Desktop, Cursor, ...) can reason over the graph.

Run (stdio): uv run --env-file .env python mcp_server.py
"""
from mcp.server.mcpserver import MCPServer

import agent
from graph import Run, graph, repos

server = MCPServer("repobrain", instructions="Query a FalkorDB knowledge graph of a GitHub repo's code, PR history, "
                                             "issues, people and learned facts. Every tool takes repo as 'owner/name'.")


def run(repo):
    return Run(graph(repo))


def doc(name):
    return agent.TOOLS[name][1]


@server.tool(description="List the repos that have been ingested into the graph.")
def list_repos() -> str:
    return "\n".join(repos())


@server.tool(description=doc("search"))
def search(repo: str, text: str) -> str:
    return agent.search(run(repo), {}, text)


@server.tool(description=doc("impact"))
def impact(repo: str, name: str) -> str:
    return agent.impact(run(repo), {}, name)


@server.tool(description=doc("experts"))
def experts(repo: str, name: str) -> str:
    return agent.experts(run(repo), {}, name)


@server.tool(description=doc("history"))
def history(repo: str, name: str) -> str:
    return agent.history(run(repo), {}, name)


@server.tool(description=doc("critical"))
def critical(repo: str) -> str:
    return agent.critical(run(repo), {})


@server.tool(description=doc("connect"))
def connect(repo: str, a: str, b: str) -> str:
    return agent.connect(run(repo), {}, a, b)


@server.tool(description=doc("recall"))
def recall(repo: str, name: str) -> str:
    return agent.recall(run(repo), {}, name)


@server.tool(description=doc("remember"))
def remember(repo: str, fact: str, about: list[str]) -> str:
    return agent.remember(run(repo), {"agent": "mcp", "session": "mcp"}, fact, about)


@server.tool(description=doc("cypher"))
def cypher(repo: str, query: str) -> str:
    return agent.cypher(run(repo), {}, query)


@server.tool(description="Ask RepoBrain's own agent a question; it picks graph tools itself and cites its evidence.")
def ask(repo: str, question: str) -> str:
    return agent.ask(repo, question, session="mcp")["answer"]


if __name__ == "__main__":
    server.run()
