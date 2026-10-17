"""PostgreSQL backend. Same interface as db_mariadb.py; tools.py picks one via DB_ENGINE."""
import os
import psycopg
from psycopg import sql as psql
from psycopg.rows import dict_row

ENGINE = "postgres"
SQL_DIALECT = "postgres"        # sqlglot dialect used to validate/render generated SQL
DIALECT_NAME = "PostgreSQL"     # shown to the SQL-writing model

DSN = os.getenv("PG_DSN", "postgresql://postgres:postgres@localhost:5432/busapp")
SCHEMA = os.getenv("PG_SCHEMA", "public")


def conn():
    """Read-only connection (the server rejects any write in this session)."""
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True, connect_timeout=15,
                           options="-c default_transaction_read_only=on")


def fetch_meta():
    """Return (tables, columns) from the metadata tables as lists of dicts."""
    with conn() as c:
        tables = c.execute("SELECT table_name, description FROM meta_tables").fetchall()
        columns = c.execute("SELECT table_name, column_name, description, pii_type FROM meta_columns").fetchall()
    return tables, columns


def column_has_data(table, column):
    """True if at least one row has a non-NULL value in this column."""
    with conn() as c:
        q = psql.SQL("SELECT EXISTS (SELECT 1 FROM {} WHERE {} IS NOT NULL) AS x").format(
            psql.Identifier(table), psql.Identifier(column))
        return bool(c.execute(q).fetchone()["x"])


def schema_info(tables):
    """Return (columns_info, keys) for the given tables.
    columns_info: table_name, column_name, data_type; keys: table_name, column_name, constraint_type, rt, rc."""
    with conn() as c:
        info = c.execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                            WHERE table_schema = %s AND table_name = ANY(%s)""", (SCHEMA, tables)).fetchall()
        keys = c.execute("""SELECT kcu.table_name, kcu.column_name, tc.constraint_type, ccu.table_name AS rt, ccu.column_name AS rc
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
            LEFT JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
             AND tc.constraint_type = 'FOREIGN KEY'
            WHERE tc.constraint_type IN ('PRIMARY KEY','FOREIGN KEY') AND kcu.table_schema = %s
              AND kcu.table_name = ANY(%s)""", (SCHEMA, tables)).fetchall()
    return info, keys


def run_query(query, max_rows=1000):
    """Execute a (pre-validated) SELECT; return (column_names, rows_as_dicts)."""
    with conn() as c:
        cur = c.execute(query)
        rows = cur.fetchmany(max_rows)
        cols = [d.name for d in cur.description]
    return cols, rows
