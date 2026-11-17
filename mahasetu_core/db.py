"""Database access. Every statement is schema-qualified (mahasetu.*) so pooled sessions need no search_path."""
from contextlib import contextmanager

from sqlalchemy import create_engine, text


class Database:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True, pool_timeout=5, connect_args={"connect_timeout": 5},
                                    hide_parameters=True)

    @contextmanager
    def begin(self):
        with self.engine.begin() as conn:
            yield conn

    def dispose(self):
        self.engine.dispose()


def one(conn, sql, **params):
    return conn.execute(text(sql), params).mappings().first()


def many(conn, sql, **params):
    return conn.execute(text(sql), params).mappings().all()


def run(conn, sql, **params):
    return conn.execute(text(sql), params)
