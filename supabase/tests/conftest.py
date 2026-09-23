"""The database the RLS suite runs against (implementation-plan.md 9.1).

9.1 lists the "RLS isolation suite" as an integration test gated on "every CI
run touching DB/API", with the reason stated plainly: "Home videos are
sensitive; cross-user access is the worst-case bug." So these tests need a
real Postgres with the real migrations applied -- a mock would be testing the
mock, and the thing under test is the database's own access control rather
than any code we wrote around it.

Where that Postgres comes from is the caller's business. `ROOMFITTR_TEST_DB_URL`
points at it; without one the suite skips rather than fails, so a checkout with
no Docker still runs the other 685 tests.

    docker run -d --name roomfittr-test-db -p 55432:5432 \\
      -e POSTGRES_PASSWORD=postgres public.ecr.aws/supabase/postgres:17.6.1.167
    docker exec roomfittr-test-db psql -U supabase_admin -d postgres \\
      -c "alter role supabase_admin with password 'postgres'"
    export ROOMFITTR_TEST_DB_URL=postgresql://supabase_admin:postgres@localhost:55432/postgres
    uv run pytest supabase/tests -v

It has to be the superuser connection. `postgres` is not a superuser in that
image, and the migrations put a trigger on `auth.users` -- which only its
owner may do. Hosted Supabase already grants the migration role that right,
so this is a property of the bare image rather than of the schema.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg is needed for the RLS suite")

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"

DB_URL = os.environ.get("ROOMFITTR_TEST_DB_URL", "")

pytestmark = pytest.mark.skipif(not DB_URL, reason="set ROOMFITTR_TEST_DB_URL to run the RLS suite")


def _prepare_auth(cur: Any) -> None:
    """Meet the `auth` schema where the image leaves it.

    A `supabase/postgres` image ships the real thing: `auth.users`,
    `auth.uid()` reading `request.jwt.claim.sub`, and the `anon` /
    `authenticated` / `service_role` roles. So the tests exercise the same
    `auth.uid()` production does, and `set local` on that GUC is the same
    mechanism PostgREST uses rather than a stand-in for it.

    Two accommodations, both about the image rather than about production:

    - The schema is owned by `supabase_admin`, so it cannot be dropped and
      recreated between runs. Its rows are deleted instead.
    - `auth.users.is_anonymous` arrived with anonymous sign-in and the
      GoTrue migrations baked into this image predate it. Production
      Supabase has the column -- 1.1 depends on it -- so it is added when
      missing rather than designed around.
    """
    cur.execute("create schema if not exists extensions")
    cur.execute("alter table auth.users add column if not exists is_anonymous boolean")
    cur.execute("update auth.users set is_anonymous = false where is_anonymous is null")
    cur.execute("alter table auth.users alter column is_anonymous set default false")
    cur.execute("alter table auth.users alter column is_anonymous set not null")

    # Leftovers from an earlier run. The profile rows went with `public`.
    cur.execute("delete from auth.users")

    for role in ("anon", "authenticated", "service_role"):
        cur.execute(
            f"""
            do $$
            begin
              if not exists (select 1 from pg_roles where rolname = '{role}') then
                create role {role} nologin;
              end if;
            end
            $$
            """
        )


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[str]:
    """A database with every migration applied, built once per session.

    Built from scratch rather than reset between tests: the suite only reads
    and writes rows it creates, and rebuilding the schema per test would take
    longer than the whole suite does.
    """
    if not DB_URL:
        pytest.skip("no ROOMFITTR_TEST_DB_URL")

    with psycopg.connect(DB_URL, autocommit=True) as conn, conn.cursor() as cur:
        # A clean slate, so a rerun after a failed migration is not a
        # confusing cascade of "already exists".
        cur.execute("drop schema if exists public cascade")
        cur.execute("create schema public")
        _prepare_auth(cur)
        cur.execute("grant usage on schema public to anon, authenticated")

        for path in sorted(MIGRATIONS.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            try:
                cur.execute(sql)
            except Exception as error:  # noqa: BLE001
                raise AssertionError(f"{path.name}: {error}") from error

    yield DB_URL


@pytest.fixture
def db(migrated_db: str) -> Iterator[Any]:
    """A superuser connection, for setting up rows the tests then attack.

    Every test starts from an empty database. That is not tidiness: several
    of these tests assert on a *count* -- "the worker can see no rooms",
    "the purge job sees exactly the one room marked for deletion" -- and a
    row left behind by an earlier test turns a passing assertion into a
    failing one, or worse, a failing one into a passing one.
    """
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        _truncate(conn)
        yield conn


def _truncate(conn: Any) -> None:
    """Empty every table, respecting the one FK that refuses to cascade.

    `placed_items.product_id` is ON DELETE RESTRICT by design (6.2: products
    are "never hard-deleted while referenced"), so the user tree has to go
    before the catalog -- which is exactly the ordering the purge job has to
    get right too.
    """
    with conn.cursor() as cur:
        # Cascades: auth.users -> profiles -> rooms -> scans -> everything.
        cur.execute("delete from auth.users")
        cur.execute("truncate public.placed_items cascade")
        cur.execute("truncate public.price_history cascade")
        cur.execute("truncate public.products cascade")
        cur.execute("truncate public.product_pages cascade")
        cur.execute("truncate public.crawl_runs cascade")
        cur.execute("truncate public.retailers restart identity cascade")
        cur.execute("truncate public.jobs cascade")
        cur.execute("truncate public.usage_counters cascade")
