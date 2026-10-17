"""MariaDB / MySQL backend. Same interface as db_postgres.py; tools.py picks one via DB_ENGINE."""
import os
import mariadb

ENGINE = "mariadb"
SQL_DIALECT = "mysql"           # sqlglot dialect used to validate/render generated SQL
DIALECT_NAME = "MariaDB/MySQL"  # shown to the SQL-writing model


def conn():
    """Read-only session (the server rejects any write in this session)."""
    c = mariadb.connect(
        host=os.getenv("DATABASE_HOST", "localhost"),
        port=int(os.getenv("DATABASE_PORT", "3306")),
        user=os.getenv("DATABASE_USER"),
        password=os.getenv("DATABASE_PASS"),
        database=os.getenv("DATABASE_NAME"),
        connect_timeout=15,
    )
    cur = c.cursor()
    try:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
    finally:
        cur.close()
    return c


def _q(name):
    return "`" + name.replace("`", "``") + "`"


def fetch_meta():
    with conn() as c:
        cur = c.cursor(dictionary=True)
        try:
            cur.execute("SELECT table_name, description FROM meta_tables")
            tables = cur.fetchall()
            cur.execute("SELECT table_name, column_name, description, pii_type FROM meta_columns")
            columns = cur.fetchall()
        finally:
            cur.close()
    return tables, columns


def column_has_data(table, column):
    with conn() as c:
        cur = c.cursor(dictionary=True)
        try:
            cur.execute(f"SELECT EXISTS (SELECT 1 FROM {_q(table)} WHERE {_q(column)} IS NOT NULL) AS x")
            return bool(cur.fetchone()["x"])
        finally:
            cur.close()


def schema_info(tables):
    ph = ",".join(["%s"] * len(tables))
    with conn() as c:
        cur = c.cursor(dictionary=True)
        try:
            cur.execute(f"""SELECT table_name, column_name, data_type FROM information_schema.columns
                            WHERE table_schema = DATABASE() AND table_name IN ({ph})""", tuple(tables))
            info = cur.fetchall()
            cur.execute(f"""SELECT kcu.table_name AS table_name, kcu.column_name AS column_name, tc.constraint_type AS constraint_type,
                                   kcu.referenced_table_name AS rt, kcu.referenced_column_name AS rc
                            FROM information_schema.table_constraints tc
                            JOIN information_schema.key_column_usage kcu
                              ON tc.constraint_name = kcu.constraint_name
                             AND tc.table_schema = kcu.table_schema AND tc.table_name = kcu.table_name
                            WHERE tc.constraint_type IN ('PRIMARY KEY','FOREIGN KEY')
                              AND tc.table_schema = DATABASE() AND kcu.table_name IN ({ph})""", tuple(tables))
            keys = cur.fetchall()
        finally:
            cur.close()
    # normalise key casing: some servers return upper-case information_schema column names
    norm = lambda rows: [{k.lower(): v for k, v in r.items()} for r in rows]
    return norm(info), norm(keys)


def run_query(query, max_rows=1000):
    with conn() as c:
        cur = c.cursor(dictionary=True)
        try:
            cur.execute(query)
            rows = cur.fetchmany(max_rows)
            cols = [d[0] for d in cur.description]
        finally:
            cur.close()
    return cols, rows
