"""Real PostgreSQL, spawned-process tests; opt in with ARCHDECK_TEST_PG_DSN.

The supplied deployment role must create schemas/temporary roles in a dedicated test
DB. Tests delete only their randomly named schema/role, never application records.
No collector or model network calls are made.
"""

import multiprocessing as mp
import os
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_runtime import Fetcher, Provider, request_value

from archdeck_agent.checkpoint_setup import main as setup
from archdeck_agent.registry import PostgresRegistry, RegistryConflict
from archdeck_agent.runtime import ResearchRuntime, RuntimeErrorCode


@pytest.fixture(scope="module")
def pg_store():
    source = os.getenv("ARCHDECK_TEST_PG_DSN")
    if not source:
        pytest.skip("ARCHDECK_TEST_PG_DSN not configured (real PostgreSQL required)")
    schema = "agent_test_" + uuid.uuid4().hex[:16]
    role = schema + "_worker"
    password = uuid.uuid4().hex
    with psycopg.connect(source, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
        db.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    owner_dsn = make_conninfo(source, options=f"-csearch_path={schema}")
    previous = os.environ.get("ARCHDECK_AGENT_CHECKPOINT_DSN")
    os.environ["ARCHDECK_AGENT_CHECKPOINT_DSN"] = owner_dsn
    setup()
    with psycopg.connect(source, autocommit=True) as db:
        db.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        db.execute(
            sql.SQL("GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
    settings = conninfo_to_dict(owner_dsn)
    settings.update(user=role, password=password)
    worker_dsn = make_conninfo(**settings)
    try:
        yield worker_dsn, owner_dsn
    finally:
        if previous is None:
            os.environ.pop("ARCHDECK_AGENT_CHECKPOINT_DSN", None)
        else:
            os.environ["ARCHDECK_AGENT_CHECKPOINT_DSN"] = previous
        with psycopg.connect(source, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def claim_process(dsn, run_id, fence, start, queue):
    start.wait(10)
    try:
        claim = PostgresRegistry(dsn).claim(run_id, fence, "a" * 64)
        queue.put(("won", claim.generation))
    except RegistryConflict as error:
        queue.put(("rejected", str(error)))


def runtime_process(dsn, run_id, fence, entered, release, queue):
    os.environ["ARCHDECK_AGENT_DISTRIBUTED"] = "true"
    os.environ["ARCHDECK_AGENT_CHECKPOINT_DSN"] = dsn

    class BlockingProvider(Provider):
        def generate(self, context, run_id):
            entered.set()
            if not release.wait(20):
                raise RuntimeError("test_release_timeout")
            return super().generate(context, run_id)

    try:
        runtime = ResearchRuntime(fetcher_factory=Fetcher, provider_factory=BlockingProvider)
        request = request_value().model_copy(update={"runId": run_id, "leaseFence": fence})
        result = runtime.execute(request)
        queue.put(("completed", result["leaseFence"]))
    except Exception as error:
        queue.put(("rejected", str(error)))


@contextmanager
def blocked_worker(dsn, run_id, fence):
    context = mp.get_context("spawn")
    entered, release, queue = context.Event(), context.Event(), context.Queue()
    worker = context.Process(
        target=runtime_process, args=(dsn, run_id, fence, entered, release, queue)
    )
    worker.start()
    try:
        assert entered.wait(15), "fixture model was not reached"
        yield worker, release, queue
    finally:
        release.set()
        worker.join(20)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        assert worker.exitcode == 0


def test_real_processes_claim_one_owner(pg_store):
    dsn, _ = pg_store
    context = mp.get_context("spawn")
    start, queue = context.Event(), context.Queue()
    run_id = "concurrent-" + uuid.uuid4().hex
    processes = [
        context.Process(target=claim_process, args=(dsn, run_id, 1, start, queue)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(result[0] for result in results) == ["rejected", "won"]
    assert ("rejected", "execution_busy") in results


def test_actual_graph_new_fence_rejects_stale_completion_and_cancel(pg_store):
    dsn, _ = pg_store
    run_id = "fenced-" + uuid.uuid4().hex
    with blocked_worker(dsn, run_id, 1) as (_, old_release, old_queue):
        with blocked_worker(dsn, run_id, 2) as (_, release, queue):
            with pytest.raises(RegistryConflict, match="stale_fence"):
                PostgresRegistry(dsn).cancel(run_id, 1)
            release.set()
            assert queue.get(timeout=15) == ("completed", 2)
            old_release.set()
            assert old_queue.get(timeout=15) == ("rejected", "stale_or_cancelled_execution")
    with psycopg.connect(dsn) as db:
        row = db.execute(
            "SELECT fence,model_attempts,result->>'leaseFence', "
            "result->'usage'->>'modelAttemptsReserved' "
            "FROM execution_registry WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        assert row == (2, 2, "2", "2")
        assert (
            db.execute(
                "SELECT count(*) FROM execution_receipts WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT count(DISTINCT thread_id) FROM checkpoints WHERE thread_id LIKE %s",
                (run_id + ":%",),
            ).fetchone()[0]
            == 2
        )


def test_process_cancel_blocks_completion_and_early_cancel_is_durable(pg_store):
    dsn, _ = pg_store
    run_id = "cancel-" + uuid.uuid4().hex
    with blocked_worker(dsn, run_id, 1) as (_, release, queue):
        PostgresRegistry(dsn).cancel(run_id, 1)
        release.set()
        assert queue.get(timeout=15) == ("rejected", "stale_or_cancelled_execution")
    registry = PostgresRegistry(dsn)
    early = "early-" + uuid.uuid4().hex
    registry.cancel(early, 1)
    with pytest.raises(RegistryConflict, match="run_cancelled"):
        registry.claim(early, 2, "b" * 64)


def test_restart_reclaims_expiry_global_budget_and_identity(pg_store):
    dsn, owner_dsn = pg_store
    registry = PostgresRegistry(dsn)
    run_id = "restart-" + uuid.uuid4().hex
    first = registry.claim(run_id, 1, "c" * 64)
    registry.reserve_model(first)
    with psycopg.connect(owner_dsn) as db:
        db.execute(
            "UPDATE execution_registry SET lease_until=now()-interval '1 second' WHERE run_id=%s",
            (run_id,),
        )
    second = registry.claim(run_id, 1, "c" * 64)
    assert second.generation != first.generation
    assert not registry.heartbeat(first)
    with pytest.raises(RegistryConflict, match="stale_completion"):
        registry.complete(first, {"leaseFence": 1})
    registry.reserve_model(second)
    third = registry.claim(run_id, 2, "c" * 64)
    with pytest.raises(RegistryConflict, match="model_attempt_budget"):
        registry.reserve_model(third)
    with pytest.raises(RegistryConflict, match="identity_conflict"):
        registry.claim(run_id, 3, "d" * 64)
    registry.complete(third, {"leaseFence": 2, "published": False})
    assert registry.claim(run_id, 3, "c" * 64).result["leaseFence"] == 3
    with pytest.raises(RegistryConflict, match="stale_fence"):
        registry.claim(run_id, 2, "c" * 64)


def test_runtime_role_cannot_read_business_schema_or_create_objects(pg_store):
    dsn, _ = pg_store
    PostgresRegistry(dsn).probe()
    with psycopg.connect(dsn, autocommit=True) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("CREATE TABLE forbidden_runtime_ddl(id integer)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT * FROM archdeck.content_items LIMIT 0")


def test_distributed_mode_requires_postgres(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHDECK_AGENT_DISTRIBUTED", "true")
    monkeypatch.delenv("ARCHDECK_AGENT_CHECKPOINT_DSN", raising=False)
    with pytest.raises(RuntimeErrorCode, match="distributed_postgres_required"):
        ResearchRuntime(tmp_path / "unused.sqlite")


def test_crashed_graph_process_restarts_in_new_generation(pg_store):
    dsn, owner_dsn = pg_store
    run_id = "crash-" + uuid.uuid4().hex
    context = mp.get_context("spawn")
    entered, release, queue = context.Event(), context.Event(), context.Queue()
    process = context.Process(
        target=runtime_process, args=(dsn, run_id, 1, entered, release, queue)
    )
    process.start()
    try:
        assert entered.wait(15)
    finally:
        process.terminate()
        process.join(5)
    with psycopg.connect(owner_dsn) as db:
        db.execute(
            "UPDATE execution_registry SET lease_until=now()-interval '1 second' WHERE run_id=%s",
            (run_id,),
        )
    with blocked_worker(dsn, run_id, 1) as (_, restarted_release, restarted_queue):
        restarted_release.set()
        assert restarted_queue.get(timeout=15) == ("completed", 1)
    with psycopg.connect(dsn) as db:
        assert (
            db.execute(
                "SELECT model_attempts FROM execution_registry WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT count(DISTINCT thread_id) FROM checkpoints WHERE thread_id LIKE %s",
                (run_id + ":%",),
            ).fetchone()[0]
            == 2
        )


def test_actual_worker_rejects_same_fence_and_renews_heartbeat(pg_store):
    import time

    dsn, owner_dsn = pg_store
    run_id = "heartbeat-" + uuid.uuid4().hex
    with blocked_worker(dsn, run_id, 1) as (_, release, queue):
        context = mp.get_context("spawn")
        entered, other_release, other_queue = context.Event(), context.Event(), context.Queue()
        other = context.Process(
            target=runtime_process, args=(dsn, run_id, 1, entered, other_release, other_queue)
        )
        other.start()
        assert other_queue.get(timeout=15) == ("rejected", "execution_busy")
        other.join(10)
        assert other.exitcode == 0 and not entered.is_set()
        with psycopg.connect(owner_dsn) as db:
            before = db.execute(
                "SELECT lease_until FROM execution_registry WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
        time.sleep(6)
        with psycopg.connect(dsn) as db:
            after = db.execute(
                "SELECT lease_until FROM execution_registry WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
        assert after > before
        release.set()
        assert queue.get(timeout=15) == ("completed", 1)


def test_application_owner_is_rejected_as_runtime_identity(pg_store):
    _, owner_dsn = pg_store
    with pytest.raises(RegistryConflict, match="role_not_isolated"):
        PostgresRegistry(owner_dsn).probe()


def test_readiness_checks_each_required_dml_permission(pg_store):
    dsn, owner_dsn = pg_store
    role = conninfo_to_dict(dsn)["user"]
    with psycopg.connect(owner_dsn) as db:
        db.execute(
            sql.SQL("REVOKE UPDATE ON execution_registry FROM {}").format(sql.Identifier(role))
        )
    try:
        with pytest.raises(RegistryConflict, match="execution_store_permissions"):
            PostgresRegistry(dsn).probe()
    finally:
        with psycopg.connect(owner_dsn) as db:
            db.execute(
                sql.SQL("GRANT UPDATE ON execution_registry TO {}").format(sql.Identifier(role))
            )
