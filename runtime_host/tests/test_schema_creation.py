from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event, inspect

from runtime_core.schema import create_owned_tables


@pytest.fixture
def schema_db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'schema.db').as_posix()}")
    metadata = MetaData()
    Table("owned", metadata, Column("id", Integer, primary_key=True),
          Column("value", Integer, index=True))
    Table("other_pack", metadata, Column("id", Integer, primary_key=True))
    try:
        yield SimpleNamespace(engine=engine, metadata=metadata)
    finally:
        engine.dispose()


def test_owned_schema_is_transactional_idempotent_and_preserves_data(schema_db):
    transactions = []

    @event.listens_for(schema_db.engine, "before_cursor_execute")
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("CREATE "):
            transactions.append(connection.connection.driver_connection.in_transaction)

    create_owned_tables(schema_db, ["owned"])
    assert transactions == [True, True]  # table and index share a real transaction
    with schema_db.engine.begin() as connection:
        connection.execute(schema_db.metadata.tables["owned"].insert().values(id=1, value=7))
    create_owned_tables(schema_db, ["owned"])
    assert transactions == [True, True]
    assert inspect(schema_db.engine).get_table_names() == ["owned"]
    with schema_db.engine.connect() as connection:
        assert connection.execute(schema_db.metadata.tables["owned"].select()).all() == [(1, 7)]


def test_schema_failure_rolls_back_table_and_can_retry(schema_db):
    def fail_index(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("CREATE INDEX"):
            raise RuntimeError("simulated index failure")

    event.listen(schema_db.engine, "before_cursor_execute", fail_index)
    with pytest.raises(RuntimeError, match="simulated index failure"):
        create_owned_tables(schema_db, ["owned"])
    assert inspect(schema_db.engine).get_table_names() == []
    event.remove(schema_db.engine, "before_cursor_execute", fail_index)
    create_owned_tables(schema_db, ["owned"])
    assert inspect(schema_db.engine).get_table_names() == ["owned"]
