"""Explicit deployment-only checkpoint migration; runtime never creates PG tables."""

import os

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver


def main():
    dsn = os.environ.get("ARCHDECK_AGENT_CHECKPOINT_DSN")
    if not dsn:
        raise SystemExit("ARCHDECK_AGENT_CHECKPOINT_DSN is required")
    with psycopg.connect(dsn) as db:
        schema = db.execute("SELECT current_schema()").fetchone()[0]
        if not schema or schema in {"public", "archdeck"}:
            raise SystemExit("Explicit isolated Agent schema search_path is required")
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()

    with psycopg.connect(dsn) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS execution_registry (
            run_id varchar(80) PRIMARY KEY, fence bigint NOT NULL, request_hash char(64) NOT NULL,
            owner uuid NOT NULL, generation uuid NOT NULL, lease_until timestamptz NOT NULL,
            cancelled boolean NOT NULL DEFAULT false, result jsonb,
            model_attempts integer NOT NULL DEFAULT 0 CHECK(model_attempts BETWEEN 0 AND 2))""")
        db.execute("""CREATE TABLE IF NOT EXISTS execution_receipts (
            run_id varchar(80) NOT NULL REFERENCES execution_registry(run_id),
            fence bigint NOT NULL, generation uuid NOT NULL, result_sha256 char(64) NOT NULL,
            accepted_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(run_id,fence,generation))""")
        db.execute("""CREATE TABLE IF NOT EXISTS execution_cancellations (
            run_id varchar(80) PRIMARY KEY, fence bigint NOT NULL)""")
    print("Agent checkpoint tables initialized in the explicitly configured database/schema.")


if __name__ == "__main__":
    main()
