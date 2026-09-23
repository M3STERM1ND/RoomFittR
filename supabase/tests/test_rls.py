"""The RLS isolation suite (implementation-plan.md 9.1, Phase 5).

Phase 5's test list: "user A cannot read/update/delete user B's rooms, scans,
layouts, items, or presign their assets; anonymous user data survives Google
linking." 9.1 runs this on every CI run touching the database and gives the
reason: "Home videos are sensitive; cross-user access is the worst-case bug."

Two things this file is careful about, because both are ways an RLS suite can
pass while the product is wide open:

- **A denied read and an empty table look identical.** Every isolation test
  below first proves the row is visible to its owner, then proves it is not
  visible to the stranger. Asserting only the second half would still pass
  against a database where the insert silently failed.
- **A narrow SELECT policy hides a wide UPDATE policy.** `update ... where
  id = $1` must read `id`, so the SELECT policy runs first and a stranger
  matches nothing however wide the UPDATE policy is. `update ... set name =
  'x'` reads no column and does not. Widening `rooms_update_own` to
  `using (true)` was measured against this suite and left it entirely green
  until the unqualified tests below were written.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

psycopg = pytest.importorskip("psycopg")

# Read directly rather than imported from `conftest`. Every worker package has
# a tests/conftest.py and none of the test directories carry an __init__.py
# (deliberately: two packages both named `tests` collide in mypy), so they all
# resolve to the module name `conftest` and an import picks whichever was
# loaded first -- which is some other package's.
DB_URL = os.environ.get("ROOMFITTR_TEST_DB_URL", "")

pytestmark = pytest.mark.skipif(not DB_URL, reason="set ROOMFITTR_TEST_DB_URL to run the RLS suite")


# ---------------------------------------------------------------------------
# Acting as somebody
# ---------------------------------------------------------------------------
@contextmanager
def acting_as(url: str, user_id: str | None, role: str = "authenticated") -> Iterator[Any]:
    """Run statements as a signed-in user, the way PostgREST does.

    `request.jwt.claim.sub` is the GUC Supabase's `auth.uid()` reads, and
    `set local` scopes it to the transaction. This is the same mechanism
    production uses rather than an approximation of it -- which matters,
    because a suite that sets `owner_id` by hand would pass against policies
    that never call `auth.uid()` at all.
    """
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if user_id is not None:
            cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (user_id,))
        cur.execute(f"set local role {role}")
        try:
            yield cur
        finally:
            conn.rollback()


def make_user(db: Any, *, anonymous: bool = False) -> str:
    """A user, their auth row, and the profile the trigger creates for them."""
    user_id = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            "insert into auth.users (id, is_anonymous) values (%s, %s)",
            (user_id, anonymous),
        )
    return user_id


def make_room(db: Any, owner: str, name: str = "Living room") -> str:
    with db.cursor() as cur:
        cur.execute(
            "insert into public.rooms (owner_id, name) values (%s, %s) returning id",
            (owner, name),
        )
        return str(cur.fetchone()[0])


def make_scan(db: Any, room: str, owner: str) -> str:
    with db.cursor() as cur:
        cur.execute(
            "insert into public.scans (room_id, owner_id) values (%s, %s) returning id",
            (room, owner),
        )
        return str(cur.fetchone()[0])


def make_layout(db: Any, room: str, owner: str) -> str:
    with db.cursor() as cur:
        cur.execute(
            """insert into public.layouts (room_id, owner_id, budget_cents)
               values (%s, %s, 300000) returning id""",
            (room, owner),
        )
        return str(cur.fetchone()[0])


@pytest.fixture
def two_users(db: Any) -> tuple[str, str]:
    return make_user(db), make_user(db)


# ---------------------------------------------------------------------------
# The profile trigger
# ---------------------------------------------------------------------------
class TestProfiles:
    def test_a_profile_appears_for_every_auth_user(self, db: Any) -> None:
        """1.1: anonymous users get "a real user ID, so RLS works
        identically" -- which is only true if the profile exists, and an
        anonymous user never goes through a sign-up flow."""
        user = make_user(db, anonymous=True)
        with db.cursor() as cur:
            cur.execute("select is_anonymous from public.profiles where id = %s", (user,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] is True

    def test_a_user_sees_only_their_own_profile(self, db: Any, two_users: tuple[str, str]) -> None:
        alice, bob = two_users
        with acting_as(DB_URL, alice) as cur:
            cur.execute("select id from public.profiles")
            ids = {str(r[0]) for r in cur.fetchall()}
        assert ids == {alice}
        assert bob not in ids

    def test_a_user_cannot_take_over_another_profile(
        self, db: Any, two_users: tuple[str, str]
    ) -> None:
        alice, bob = two_users
        with acting_as(DB_URL, alice) as cur:
            cur.execute("update public.profiles set display_name = 'pwned' where id = %s", (bob,))
            assert cur.rowcount == 0


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
class TestCrossUserReads:
    @pytest.mark.parametrize(
        "table",
        [
            "rooms",
            "scans",
            "room_models",
            "measurements",
            "detected_objects",
            "scan_artifacts",
            "scan_events",
            "layouts",
            "placed_items",
        ],
    )
    def test_a_stranger_sees_nothing(self, db: Any, two_users: tuple[str, str], table: str) -> None:
        """Every user-owned table in 6.1 and 6.2, one row each, owned by
        Alice. Bob must see none of them -- and Alice must see hers, or this
        test would pass against a database where the insert failed."""
        alice, bob = two_users
        room = make_room(db, alice)
        scan = make_scan(db, room, alice)
        _seed_all(db, alice, room, scan)

        with acting_as(DB_URL, alice) as cur:
            cur.execute(f"select count(*) from public.{table}")  # noqa: S608
            assert cur.fetchone()[0] >= 1, f"{table}: the owner cannot see their own row"

        with acting_as(DB_URL, bob) as cur:
            cur.execute(f"select count(*) from public.{table}")  # noqa: S608
            assert cur.fetchone()[0] == 0, f"{table}: a stranger can read it"

    def test_a_signed_out_visitor_sees_no_rooms(self, db: Any) -> None:
        """`anon` has no grant on any user-owned table, so this fails at the
        privilege check rather than returning an empty set -- the outer wall
        before the policy."""
        alice = make_user(db)
        make_room(db, alice)
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            acting_as(DB_URL, None, role="anon") as cur,
        ):
            cur.execute("select count(*) from public.rooms")

    def test_a_room_being_deleted_disappears_immediately(self, db: Any) -> None:
        """7.4: "Room is already hidden (status `deleting`) and RLS excludes
        it from all reads." The user must stop seeing it the moment they ask
        for deletion, not when the purge job gets round to it."""
        alice = make_user(db)
        room = make_room(db, alice)

        with acting_as(DB_URL, alice) as cur:
            cur.execute("select count(*) from public.rooms where id = %s", (room,))
            assert cur.fetchone()[0] == 1

        with db.cursor() as cur:
            cur.execute("update public.rooms set status = 'deleting' where id = %s", (room,))

        with acting_as(DB_URL, alice) as cur:
            cur.execute("select count(*) from public.rooms where id = %s", (room,))
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
class TestCrossUserWrites:
    def test_a_stranger_cannot_update_a_room(self, db: Any, two_users: tuple[str, str]) -> None:
        alice, bob = two_users
        room = make_room(db, alice, "Alice's living room")

        with acting_as(DB_URL, bob) as cur:
            cur.execute("update public.rooms set name = 'Bob was here' where id = %s", (room,))
            assert cur.rowcount == 0

        with db.cursor() as cur:
            cur.execute("select name from public.rooms where id = %s", (room,))
            assert cur.fetchone()[0] == "Alice's living room"

    def test_a_stranger_cannot_delete_a_room(self, db: Any, two_users: tuple[str, str]) -> None:
        alice, bob = two_users
        room = make_room(db, alice)

        with acting_as(DB_URL, bob) as cur:
            cur.execute("delete from public.rooms where id = %s", (room,))
            assert cur.rowcount == 0

        with db.cursor() as cur:
            cur.execute("select count(*) from public.rooms where id = %s", (room,))
            assert cur.fetchone()[0] == 1

    def test_a_stranger_cannot_delete_a_layouts_items(
        self, db: Any, two_users: tuple[str, str]
    ) -> None:
        """Phase 5 names "items" specifically: a layout is worthless if
        someone else can empty it."""
        alice, bob = two_users
        room = make_room(db, alice)
        layout = make_layout(db, room, alice)
        product = _seed_product(db)
        with db.cursor() as cur:
            cur.execute(
                """insert into public.placed_items
                     (layout_id, owner_id, product_id, category, x_mm, z_mm)
                   values (%s, %s, %s, 'sofa', 0, 0)""",
                (layout, alice, product),
            )

        with acting_as(DB_URL, bob) as cur:
            cur.execute("delete from public.placed_items where layout_id = %s", (layout,))
            assert cur.rowcount == 0

        with db.cursor() as cur:
            cur.execute("select count(*) from public.placed_items where layout_id = %s", (layout,))
            assert cur.fetchone()[0] == 1

    def test_a_stranger_cannot_rewrite_every_room_at_once(self, db: Any) -> None:
        """The unqualified form, which is the one that actually tests the
        UPDATE policy.

        `update ... where id = <x>` has to *read* `id` to evaluate the
        filter, so the SELECT policy runs first and a stranger matches no
        rows however wide the UPDATE policy is. That makes the obvious
        cross-user update test pass against a policy of `using (true)` --
        measured, not assumed: widening `rooms_update_own` to `true` left
        every other test in this file green.

        An attacker does not write the qualified form. `update rooms set
        name = 'x'` reads no column, skips the SELECT policy entirely, and
        takes every row the UPDATE policy admits.
        """
        alice, bob = make_user(db), make_user(db)
        room = make_room(db, alice, "Alice's living room")

        with acting_as(DB_URL, bob) as cur:
            cur.execute("update public.rooms set name = 'Bob was here'")
            assert cur.rowcount == 0, "a stranger rewrote rooms they cannot see"

        with db.cursor() as cur:
            cur.execute("select name from public.rooms where id = %s", (room,))
            assert cur.fetchone()[0] == "Alice's living room"

    def test_a_stranger_cannot_empty_the_table(self, db: Any) -> None:
        """The same hole in DELETE. `delete from rooms` names no column."""
        alice, bob = make_user(db), make_user(db)
        room = make_room(db, alice)

        with acting_as(DB_URL, bob) as cur:
            cur.execute("delete from public.rooms")
            assert cur.rowcount == 0, "a stranger deleted rooms they cannot see"

        with db.cursor() as cur:
            cur.execute("select count(*) from public.rooms where id = %s", (room,))
            assert cur.fetchone()[0] == 1

    @pytest.mark.parametrize(
        "table", ["scans", "room_models", "measurements", "layouts", "placed_items"]
    )
    def test_no_user_owned_table_can_be_rewritten_wholesale(self, db: Any, table: str) -> None:
        """The same unqualified check across every table a user can write.

        Parametrized rather than written out because the hole is structural:
        it appears wherever a policy's `using` is wider than its owner
        predicate, and it appears identically.
        """
        alice, bob = make_user(db), make_user(db)
        room = make_room(db, alice)
        scan = make_scan(db, room, alice)
        _seed_all(db, alice, room, scan)

        with acting_as(DB_URL, bob) as cur:
            cur.execute(f"update public.{table} set updated_at = now()")  # noqa: S608
            assert cur.rowcount == 0, f"{table}: a stranger rewrote every row"

    def test_a_user_cannot_plant_a_room_in_another_account(
        self, db: Any, two_users: tuple[str, str]
    ) -> None:
        """The `with check` half of the insert policy. Without it Alice can
        create rows that appear in Bob's account."""
        alice, bob = two_users
        with pytest.raises(psycopg.errors.InsufficientPrivilege), acting_as(DB_URL, alice) as cur:
            cur.execute("insert into public.rooms (owner_id, name) values (%s, 'gift')", (bob,))

    def test_a_user_cannot_give_their_room_away(self, db: Any, two_users: tuple[str, str]) -> None:
        """The `with check` half of the update policy. `using` alone lets the
        owner rewrite `owner_id`, which is a row they may edit becoming a row
        they may not -- and it lands in a stranger's account."""
        alice, bob = two_users
        room = make_room(db, alice)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), acting_as(DB_URL, alice) as cur:
            cur.execute("update public.rooms set owner_id = %s where id = %s", (bob, room))

    def test_a_stranger_cannot_write_a_scan_result(
        self, db: Any, two_users: tuple[str, str]
    ) -> None:
        alice, bob = two_users
        room = make_room(db, alice)
        scan = make_scan(db, room, alice)

        with acting_as(DB_URL, bob) as cur:
            cur.execute("update public.scans set status = 'failed' where id = %s", (scan,))
            assert cur.rowcount == 0

    def test_the_audit_trail_cannot_be_edited(self, db: Any) -> None:
        """6.1 calls scan_events "append-only". No update or delete policy
        exists for anyone, so a user cannot erase what happened to their scan
        -- and neither can the worker."""
        alice = make_user(db)
        room = make_room(db, alice)
        scan = make_scan(db, room, alice)
        with db.cursor() as cur:
            cur.execute(
                """insert into public.scan_events (scan_id, owner_id, message)
                   values (%s, %s, 'started')""",
                (scan, alice),
            )

        with acting_as(DB_URL, alice) as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("update public.scan_events set message = 'nothing to see'")

        with acting_as(DB_URL, alice) as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("delete from public.scan_events")


# ---------------------------------------------------------------------------
# Identity linking
# ---------------------------------------------------------------------------
class TestAnonymousLinking:
    def test_anonymous_data_survives_signing_in(self, db: Any) -> None:
        """Phase 5: "anonymous user data survives Google linking". 1.1 sells
        this as the product's own promise -- "usable without an account, sign
        in to keep things" -- so the rows must stay put and stay readable
        under the same id."""
        alice = make_user(db, anonymous=True)
        room = make_room(db, alice, "Scanned before signing up")

        with acting_as(DB_URL, alice) as cur:
            cur.execute("select name from public.rooms where id = %s", (room,))
            assert cur.fetchone()[0] == "Scanned before signing up"

        # Linking an identity flips the flag on auth.users; the user id does
        # not change, which is the whole reason this works.
        with db.cursor() as cur:
            cur.execute(
                "update auth.users set is_anonymous = false, email = %s where id = %s",
                ("alice@example.com", alice),
            )

        with acting_as(DB_URL, alice) as cur:
            cur.execute("select name from public.rooms where id = %s", (room,))
            assert cur.fetchone()[0] == "Scanned before signing up"

    def test_the_profile_mirrors_the_change(self, db: Any) -> None:
        """7.4's retention sweeper deletes anonymous profiles by
        `last_active_at`. If the mirror does not update, it deletes the rooms
        of someone who has just signed up."""
        alice = make_user(db, anonymous=True)
        with db.cursor() as cur:
            cur.execute("update auth.users set is_anonymous = false where id = %s", (alice,))
            cur.execute("select is_anonymous from public.profiles where id = %s", (alice,))
            assert cur.fetchone()[0] is False


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class TestCatalog:
    def test_anyone_may_browse_active_products(self, db: Any) -> None:
        """1.1 makes the product browser usable before sign-in."""
        _seed_product(db)
        with acting_as(DB_URL, None, role="anon") as cur:
            cur.execute("select count(*) from public.products")
            assert cur.fetchone()[0] >= 1

    def test_a_rejected_product_is_not_reachable(self, db: Any) -> None:
        """Guessing an id must not surface a listing we decided was wrong.
        9.3's R7 is about wrong dimensions reaching the user; a rejected row
        readable by id is that failure with an extra step."""
        rejected = _seed_product(db, status="rejected_dimensions")
        with acting_as(DB_URL, None, role="anon") as cur:
            cur.execute("select count(*) from public.products where id = %s", (rejected,))
            assert cur.fetchone()[0] == 0

    def test_crawl_internals_are_not_public(self, db: Any) -> None:
        """A retailer's selectors, robots notes and affiliate config are
        operational data, and 9.4 treats the crawl policy as a legal record.
        `product_pages` has no policy at all, so nobody outside the worker
        reads raw fetched HTML."""
        alice = make_user(db)
        for table in ("product_pages", "crawl_runs"):
            with (
                acting_as(DB_URL, alice) as cur,
                pytest.raises(psycopg.errors.InsufficientPrivilege),
            ):
                cur.execute(f"select count(*) from public.{table}")  # noqa: S608


# ---------------------------------------------------------------------------
# The worker role
# ---------------------------------------------------------------------------
class TestWorkerRole:
    def test_it_can_write_pipeline_results(self, db: Any) -> None:
        """6: the worker "bypasses RLS only on pipeline/catalog tables"."""
        alice = make_user(db)
        room = make_room(db, alice)
        scan = make_scan(db, room, alice)

        with acting_as(DB_URL, None, role="roomfittr_worker") as cur:
            cur.execute(
                "update public.scans set status = 'processing', stage = 'ingest' where id = %s",
                (scan,),
            )
            assert cur.rowcount == 1

    def test_it_cannot_read_a_users_room_list(self, db: Any) -> None:
        """The point of not making it BYPASSRLS. A compromised worker token
        should not be a directory of everyone's homes -- it can only touch
        rooms already marked for deletion, which is what the purge job needs
        and nothing more."""
        alice = make_user(db)
        make_room(db, alice)
        with acting_as(DB_URL, None, role="roomfittr_worker") as cur:
            cur.execute("select count(*) from public.rooms")
            assert cur.fetchone()[0] == 0

    def test_it_cannot_read_profiles_at_all(self, db: Any) -> None:
        make_user(db)
        with (
            acting_as(DB_URL, None, role="roomfittr_worker") as cur,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cur.execute("select count(*) from public.profiles")

    def test_the_purge_job_can_see_what_it_must_delete(self, db: Any) -> None:
        alice = make_user(db)
        room = make_room(db, alice)
        with db.cursor() as cur:
            cur.execute("update public.rooms set status = 'deleting' where id = %s", (room,))

        with acting_as(DB_URL, None, role="roomfittr_worker") as cur:
            cur.execute("select count(*) from public.rooms where status = 'deleting'")
            assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Helpers that seed rows
# ---------------------------------------------------------------------------
def _seed_product(db: Any, *, status: str = "active") -> str:
    suffix = uuid.uuid4().hex[:8]
    with db.cursor() as cur:
        cur.execute(
            """insert into public.retailers (name, domain, status)
               values (%s, %s, 'active')
               on conflict (domain) do update set status = 'active'
               returning id""",
            (f"Test retailer {suffix}", f"retailer-{suffix}.example"),
        )
        retailer = cur.fetchone()[0]
        cur.execute(
            """insert into public.products
                 (retailer_id, retailer_sku, canonical_url, title, category,
                  width_mm, depth_mm, height_mm, price_cents, availability, status)
               values (%s, %s, %s, 'A sofa', 'sofa', 1800, 880, 800, 49900,
                       'in_stock', %s)
               returning id""",
            (retailer, f"sku-{suffix}", f"https://retailer-{suffix}.example/p/{suffix}", status),
        )
        return str(cur.fetchone()[0])


def _seed_all(db: Any, owner: str, room: str, scan: str) -> None:
    """One row in every user-owned table, so the parametrized test has
    something to fail to read."""
    product = _seed_product(db)
    with db.cursor() as cur:
        cur.execute(
            """insert into public.room_models
                 (scan_id, owner_id, geometry, schema_version)
               values (%s, %s, '{}'::jsonb, 1) returning id""",
            (scan, owner),
        )
        room_model = cur.fetchone()[0]
        cur.execute(
            """insert into public.measurements
                 (room_model_id, owner_id, kind, target_ref, value_mm)
               values (%s, %s, 'wall_length', 'W1', 3800)""",
            (room_model, owner),
        )
        cur.execute(
            """insert into public.detected_objects
                 (scan_id, owner_id, label, label_group,
                  center_x_mm, center_z_mm, width_mm, depth_mm, height_mm)
               values (%s, %s, 'sofa', 'removable', 0, 0, 1800, 880, 800)""",
            (scan, owner),
        )
        cur.execute(
            """insert into public.scan_artifacts (scan_id, owner_id, kind, object_key)
               values (%s, %s, 'shell', 'rooms/x/shell.glb')""",
            (scan, owner),
        )
        cur.execute(
            """insert into public.scan_events (scan_id, owner_id, message)
               values (%s, %s, 'queued')""",
            (scan, owner),
        )
        cur.execute(
            """insert into public.layouts (room_id, owner_id, room_model_id, budget_cents)
               values (%s, %s, %s, 300000) returning id""",
            (room, owner, room_model),
        )
        layout = cur.fetchone()[0]
        cur.execute(
            """insert into public.placed_items
                 (layout_id, owner_id, product_id, category, x_mm, z_mm)
               values (%s, %s, %s, 'sofa', 0, 0)""",
            (layout, owner, product),
        )
