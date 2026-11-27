"""Explicit deployment-only checkpoint migration; runtime never creates PG tables."""

import os

from langgraph.checkpoint.postgres import PostgresSaver


def main():
    dsn = os.environ.get("ARCHDECK_AGENT_CHECKPOINT_DSN")
    if not dsn:
        raise SystemExit("ARCHDECK_AGENT_CHECKPOINT_DSN is required")
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
    print("Agent checkpoint tables initialized in the explicitly configured database/schema.")


if __name__ == "__main__":
    main()
