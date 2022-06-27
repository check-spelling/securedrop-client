# -*- coding: utf-8 -*-

import os
import re
import subprocess
from os import path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from securedrop_client.db import Base, convention

from . import conftest

MIGRATION_PATH = path.join(path.dirname(__file__), "..", "alembic", "versions")

ALL_MIGRATIONS = [
    x.split(".")[0].split("_")[0] for x in os.listdir(MIGRATION_PATH) if x.endswith(".py")
]

DATA_MIGRATIONS = ["d7c8af95bc8e"]

WHITESPACE_REGEX = re.compile(r"\s+")


def make_session_maker(home: str) -> scoped_session:
    """
    Duplicate securedrop_client.db.make_session_maker so that data migrations are decoupled
    from that implementation.
    """
    db_path = os.path.join(home, "svs.sqlite")
    engine = create_engine("sqlite:///{}".format(db_path))
    maker = sessionmaker(bind=engine)
    return scoped_session(maker)


def list_migrations(cfg_path, head):
    cfg = AlembicConfig(cfg_path)
    script = ScriptDirectory.from_config(cfg)
    migrations = [x.revision for x in script.walk_revisions(base="base", head=head)]
    migrations.reverse()
    return migrations


def upgrade(alembic_config, migration):
    subprocess.check_call(["alembic", "upgrade", migration], cwd=path.dirname(alembic_config))


def downgrade(alembic_config, migration):
    subprocess.check_call(["alembic", "downgrade", migration], cwd=path.dirname(alembic_config))


def get_schema(session):
    result = list(
        session.execute(
            text(
                """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        ORDER BY type, name, tbl_name
        """
            )
        )
    )

    return {(x[0], x[1], x[2]): x[3] for x in result}


def assert_schemas_equal(left, right):
    for (k, v) in left.items():
        if k not in right:
            raise AssertionError("Left contained {} but right did not".format(k))
        if not ddl_equal(v, right[k]):
            raise AssertionError(
                "Schema for {} did not match:\nLeft:\n{}\nRight:\n{}".format(k, v, right[k])
            )
        right.pop(k)

    if right:
        raise AssertionError("Right had additional tables: {}".format(right.keys()))


def ddl_equal(left, right):
    """
    Check the "tokenized" DDL is equivalent because, because sometimes Alembic schemas append
    columns on the same line to the DDL comes out like:

        column1 TEXT NOT NULL, column2 TEXT NOT NULL

    and SQLAlchemy comes out:

        column1 TEXT NOT NULL,
        column2 TEXT NOT NULL
    """
    # ignore the autoindex cases
    if left is None and right is None:
        return True

    left = [x for x in WHITESPACE_REGEX.split(left) if x]
    right = [x for x in WHITESPACE_REGEX.split(right) if x]

    # Strip commas and quotes
    left = [x.replace('"', "").replace(",", "") for x in left]
    right = [x.replace('"', "").replace(",", "") for x in right]

    return sorted(left) == sorted(right)


def test_alembic_head_matches_db_models(tmpdir):
    """
    This test is to make sure that our database models in `db.py` are always in sync with the schema
    generated by `alembic upgrade head`.
    """
    models_homedir = str(tmpdir.mkdir("models"))
    subprocess.check_call(["sqlite3", os.path.join(models_homedir, "svs.sqlite"), ".databases"])

    session_maker = make_session_maker(models_homedir)
    session = session_maker()
    Base.metadata.create_all(bind=session.get_bind(), checkfirst=False)
    assert Base.metadata.naming_convention == convention
    models_schema = get_schema(session)
    Base.metadata.drop_all(bind=session.get_bind())
    session.close()

    alembic_homedir = str(tmpdir.mkdir("alembic"))
    subprocess.check_call(["sqlite3", os.path.join(alembic_homedir, "svs.sqlite"), ".databases"])
    session_maker = make_session_maker(alembic_homedir)
    session = session_maker()
    alembic_config = conftest._alembic_config(alembic_homedir)
    upgrade(alembic_config, "head")
    alembic_schema = get_schema(session)
    Base.metadata.drop_all(bind=session.get_bind())
    session.close()

    # The initial migration creates the table 'alembic_version', but this is
    # not present in the schema created by `Base.metadata.create_all()`.
    alembic_schema = {k: v for k, v in alembic_schema.items() if k[2] != "alembic_version"}

    assert_schemas_equal(alembic_schema, models_schema)


@pytest.mark.parametrize("migration", ALL_MIGRATIONS)
def test_alembic_migration_upgrade(alembic_config, config, migration):
    # run migrations in sequence from base -> head
    for mig in list_migrations(alembic_config, migration):
        upgrade(alembic_config, mig)


@pytest.mark.parametrize("migration", DATA_MIGRATIONS)
def test_alembic_migration_upgrade_with_data(alembic_config, config, migration, homedir):
    """
    Upgrade to one migration before the target migration, load data, then upgrade in order to test
    that the upgrade is successful when there is data.
    """
    migrations = list_migrations(alembic_config, migration)
    if len(migrations) == 1:
        return
    upgrade(alembic_config, migrations[-2])
    mod_name = "tests.migrations.test_{}".format(migration)
    mod = __import__(mod_name, fromlist=["UpgradeTester"])
    session = make_session_maker(homedir)
    upgrade_tester = mod.UpgradeTester(homedir, session)
    upgrade_tester.load_data()
    upgrade(alembic_config, migration)
    upgrade_tester.check_upgrade()


@pytest.mark.parametrize("migration", ALL_MIGRATIONS)
def test_alembic_migration_downgrade(alembic_config, config, migration):
    # upgrade to the parameterized test case ("head")
    upgrade(alembic_config, migration)

    # run migrations in sequence from "head" -> base
    migrations = list_migrations(alembic_config, migration)
    migrations.reverse()

    for mig in migrations:
        downgrade(alembic_config, mig)


@pytest.mark.parametrize("migration", DATA_MIGRATIONS)
def test_alembic_migration_downgrade_with_data(alembic_config, config, migration, homedir):
    """
    Upgrade to the target migration, load data, then downgrade in order to test that the downgrade
    is successful when there is data.
    """
    upgrade(alembic_config, migration)
    mod_name = "tests.migrations.test_{}".format(migration)
    mod = __import__(mod_name, fromlist=["DowngradeTester"])
    session = make_session_maker(homedir)
    downgrade_tester = mod.DowngradeTester(homedir, session)
    downgrade_tester.load_data()
    downgrade(alembic_config, "-1")
    downgrade_tester.check_downgrade()


@pytest.mark.parametrize("migration", ALL_MIGRATIONS)
def test_schema_unchanged_after_up_then_downgrade(alembic_config, tmpdir, migration):
    migrations = list_migrations(alembic_config, migration)

    if len(migrations) > 1:
        target = migrations[-2]
        upgrade(alembic_config, target)
    else:
        # The first migration is the degenerate case where we don't need to
        # get the database to some base state.
        pass

    session = make_session_maker(str(tmpdir.mkdir("original")))()
    original_schema = get_schema(session)

    upgrade(alembic_config, "+1")
    downgrade(alembic_config, "-1")

    session = make_session_maker(str(tmpdir.mkdir("reverted")))()
    reverted_schema = get_schema(session)

    # The initial migration is a degenerate case because it creates the table
    # 'alembic_version', but rolling back the migration doesn't clear it.
    if len(migrations) == 1:
        reverted_schema = {k: v for k, v in reverted_schema.items() if k[2] != "alembic_version"}

    assert_schemas_equal(reverted_schema, original_schema)
