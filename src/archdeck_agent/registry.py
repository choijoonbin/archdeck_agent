"""Shared PostgreSQL execution claims. Business authority remains in ArchDeck BE."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg


class RegistryConflict(ValueError):
    pass


@dataclass(frozen=True)
class Claim:
    run_id: str
    fence: int
    owner: str
    generation: str
    result: dict | None = None


class PostgresRegistry:
    def __init__(self, dsn: str, lease_seconds: int = 30):
        self.dsn = dsn
        self.lease_seconds = lease_seconds

    @contextmanager
    def db(self):
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            yield connection

    def probe(self):
        with self.db() as db:
            safe = db.execute(
                "SELECT current_schema() NOT IN ('public','archdeck') "
                "AND NOT has_schema_privilege(current_user,current_schema(),'CREATE') "
                "AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls) "
                "FROM pg_roles WHERE rolname=current_user"
            ).fetchone()[0]
            business_schema = db.execute(
                "SELECT oid FROM pg_namespace WHERE nspname='archdeck'"
            ).fetchone()
            if not safe or (
                business_schema
                and db.execute(
                    "SELECT has_schema_privilege(current_user,%s,'USAGE')", (business_schema[0],)
                ).fetchone()[0]
            ):
                raise RegistryConflict("execution_store_role_not_isolated")
            db.execute("SELECT run_id FROM execution_registry LIMIT 0")
            db.execute("SELECT thread_id FROM checkpoints LIMIT 0")
            for table in (
                "execution_registry",
                "execution_receipts",
                "execution_cancellations",
                "checkpoints",
                "checkpoint_blobs",
                "checkpoint_writes",
            ):
                allowed = db.execute(
                    "SELECT has_table_privilege(current_user,%s,'SELECT') "
                    "AND has_table_privilege(current_user,%s,'INSERT') "
                    "AND has_table_privilege(current_user,%s,'UPDATE')",
                    (table, table, table),
                ).fetchone()[0]
                if not allowed:
                    raise RegistryConflict("execution_store_permissions")

    def claim(self, run_id: str, fence: int, request_hash: str) -> Claim:
        owner, generation = str(uuid.uuid4()), str(uuid.uuid4())
        with self.db() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (run_id,))
            if db.execute(
                "SELECT 1 FROM execution_cancellations WHERE run_id=%s", (run_id,)
            ).fetchone():
                raise RegistryConflict("run_cancelled")
            db.execute(
                "INSERT INTO execution_registry"
                "(run_id,fence,request_hash,owner,generation,lease_until) "
                "VALUES (%s,%s,%s,%s,%s,clock_timestamp()) ON CONFLICT(run_id) DO NOTHING",
                (run_id, fence, request_hash, owner, generation),
            )
            previous = db.execute(
                "SELECT fence,request_hash,cancelled,result,owner,generation,"
                "lease_until>clock_timestamp() "
                "FROM execution_registry WHERE run_id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if previous[1] != request_hash:
                raise RegistryConflict("run_identity_conflict")
            if fence < previous[0]:
                raise RegistryConflict("stale_fence")
            if previous[2]:
                raise RegistryConflict("run_cancelled")
            if previous[3] is not None:
                db.execute(
                    "UPDATE execution_registry SET fence=%s WHERE run_id=%s", (fence, run_id)
                )
                result = previous[3]
                result["leaseFence"] = fence
                return Claim(run_id, fence, owner, generation, result)
            if fence == previous[0] and previous[6]:
                raise RegistryConflict("execution_busy")
            db.execute(
                "UPDATE execution_registry SET fence=%s,owner=%s,generation=%s,"
                "lease_until=clock_timestamp()+(%s * interval '1 second') WHERE run_id=%s",
                (fence, owner, generation, self.lease_seconds, run_id),
            )
        return Claim(run_id, fence, owner, generation)

    def heartbeat(self, claim: Claim) -> bool:
        with self.db() as db:
            return (
                db.execute(
                    "UPDATE execution_registry "
                    "SET lease_until=clock_timestamp()+(%s * interval '1 second') "
                    "WHERE run_id=%s AND fence=%s AND owner=%s AND generation=%s "
                    "AND cancelled=false AND lease_until>clock_timestamp() AND result IS NULL",
                    (self.lease_seconds, claim.run_id, claim.fence, claim.owner, claim.generation),
                ).rowcount
                == 1
            )

    def active(self, claim: Claim) -> bool:
        with self.db() as db:
            row = db.execute(
                "SELECT 1 FROM execution_registry WHERE run_id=%s AND fence=%s AND owner=%s "
                "AND generation=%s AND cancelled=false "
                "AND lease_until>clock_timestamp() AND result IS NULL",
                (claim.run_id, claim.fence, claim.owner, claim.generation),
            ).fetchone()
            return row is not None

    def reserve_model(self, claim: Claim):
        with self.db() as db:
            changed = db.execute(
                "UPDATE execution_registry SET model_attempts=model_attempts+1 "
                "WHERE run_id=%s AND fence=%s AND owner=%s AND generation=%s "
                "AND cancelled=false AND lease_until>clock_timestamp() AND result IS NULL "
                "AND model_attempts<2",
                (claim.run_id, claim.fence, claim.owner, claim.generation),
            ).rowcount
            if not changed:
                raise RegistryConflict("model_attempt_budget_exhausted_or_stale")

    def model_attempts(self, run_id: str) -> int:
        with self.db() as db:
            return db.execute(
                "SELECT model_attempts FROM execution_registry WHERE run_id=%s", (run_id,)
            ).fetchone()[0]

    def complete(self, claim: Claim, result: dict):
        encoded = json.dumps(result)
        with self.db() as db:
            changed = db.execute(
                "UPDATE execution_registry SET result=%s::jsonb,lease_until=clock_timestamp() "
                "WHERE run_id=%s AND fence=%s AND owner=%s AND generation=%s "
                "AND cancelled=false AND lease_until>clock_timestamp() AND result IS NULL",
                (encoded, claim.run_id, claim.fence, claim.owner, claim.generation),
            ).rowcount
            if not changed:
                raise RegistryConflict("stale_completion")
            db.execute(
                "INSERT INTO execution_receipts(run_id,fence,generation,result_sha256) "
                "VALUES (%s,%s,%s,%s)",
                (
                    claim.run_id,
                    claim.fence,
                    claim.generation,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                ),
            )

    def release(self, claim: Claim):
        with self.db() as db:
            db.execute(
                "UPDATE execution_registry SET lease_until=clock_timestamp() "
                "WHERE run_id=%s AND fence=%s "
                "AND owner=%s AND generation=%s AND result IS NULL",
                (claim.run_id, claim.fence, claim.owner, claim.generation),
            )

    def cancel(self, run_id: str, fence: int | None):
        if fence is None:
            raise RegistryConflict("cancel_fence_required")
        with self.db() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (run_id,))
            row = db.execute(
                "SELECT fence FROM execution_registry WHERE run_id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if row and row[0] > fence:
                raise RegistryConflict("stale_fence")
            db.execute(
                "INSERT INTO execution_cancellations(run_id,fence) VALUES(%s,%s) "
                "ON CONFLICT(run_id) DO UPDATE SET fence=GREATEST(execution_cancellations.fence,"
                "EXCLUDED.fence)",
                (run_id, fence),
            )
            db.execute(
                "UPDATE execution_registry SET cancelled=true,lease_until=clock_timestamp() "
                "WHERE run_id=%s AND fence<=%s AND result IS NULL",
                (run_id, fence),
            )
