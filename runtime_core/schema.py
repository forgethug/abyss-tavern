"""Create an explicitly owned schema with transactional SQLite DDL."""


def create_owned_tables(db, table_names) -> None:
    tables = [db.metadata.tables[name] for name in table_names]
    with db.engine.begin() as connection:
        # sqlite3's legacy transaction mode does not BEGIN for DDL. Without
        # this, each table/index commits separately and partial schemas survive
        # errors. Respect drivers already running a real transaction.
        if (
            connection.dialect.name == "sqlite"
            and not connection.connection.driver_connection.in_transaction
        ):
            connection.exec_driver_sql("BEGIN")
        db.metadata.create_all(bind=connection, tables=tables)
