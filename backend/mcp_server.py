
from mcp.server.fastmcp import FastMCP
import tools as T

mcp = FastMCP("db-agent")
for fn in (T.find_intent, T.search_metadata, T.plan_question, T.get_schema, T.generate_sql, T.validate_sql, T.run_readonly_query, T.make_chart):
    mcp.tool()(fn)

if __name__ == "__main__":
    T.build_index()
    mcp.run()
