"""MCP server for PostgreSQL.  Run:  python mcp_server_postgres.py
Config: PG_DSN (default postgresql://postgres:postgres@localhost:5432/busapp), PG_SCHEMA (default public)."""
import os
os.environ["DB_ENGINE"] = "postgres"   # must be set before `tools` is imported

from mcp.server.fastmcp import FastMCP
import tools as T

mcp = FastMCP("db-agent-postgres")
for fn in (T.find_intent, T.search_metadata, T.plan_question, T.get_schema, T.generate_sql, T.validate_sql, T.run_readonly_query, T.make_chart):
    mcp.tool()(fn)

if __name__ == "__main__":
    T.build_index()
    mcp.run()
