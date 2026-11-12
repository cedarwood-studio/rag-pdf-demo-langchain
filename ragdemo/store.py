"""Chunk storage and similarity search: LangChain's PGVectorStore on Postgres with pgvector.

LangChain owns the table layout, the inserts, the similarity query and the
HNSW index. Each collection is its own table, with document, title, page and
heading as real columns next to the vector.

Three things the framework does not offer are done here with plain SQL: row
counts, storage size, and the lookup of question words for the refusal check
(with a trigram index on the chunk text to keep that lookup fast).
"""

from __future__ import annotations

import re
import time

import sqlalchemy
from langchain_core.embeddings import Embeddings
from langchain_postgres import Column, PGEngine, PGVectorStore
from langchain_postgres.v2.indexes import HNSWIndex, HNSWQueryOptions

METADATA = ["document", "title", "page", "heading"]


def table_name(collection: str) -> str:
    return "chunks_" + re.sub(r"\W", "_", collection)


def _sqlalchemy_url(database_url: str) -> str:
    return database_url.replace("postgresql://", "postgresql+psycopg://", 1)


class Store:
    def __init__(self, database_url: str, embeddings: Embeddings, collection: str, use_index: bool = False) -> None:
        self.collection = collection
        self.table = table_name(collection)
        self.embeddings = embeddings
        self.use_index = use_index
        self.url = _sqlalchemy_url(database_url)
        self.sql = sqlalchemy.create_engine(self.url)
        self.engine = PGEngine.from_connection_string(self.url)
        self._vectorstore: PGVectorStore | None = None
        # Make the database match the setting: an HNSW index when use_index is on, none (exact search) when it is off.
        if self.exists():
            if use_index:
                self.create_index()
            else:
                self.drop_index()

    def wait_until_ready(self, attempts: int = 30, delay: float = 2.0) -> None:
        for attempt in range(attempts):
            try:
                with self.sql.connect():
                    return
            except sqlalchemy.exc.OperationalError:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)

    def exists(self) -> bool:
        return sqlalchemy.inspect(self.sql).has_table(self.table)

    def init_schema(self, reset: bool = False) -> None:
        """Create the collection's table. With reset, an existing table is dropped first."""
        if self.exists() and not reset:
            return
        with self.sql.begin() as conn:
            conn.execute(sqlalchemy.text("CREATE EXTENSION IF NOT EXISTS vector"))
        self.engine.init_vectorstore_table(
            self.table,
            vector_size=self.embeddings.dim,
            metadata_columns=[
                Column("document", "TEXT", nullable=False),
                Column("title", "TEXT", nullable=False),
                Column("page", "INTEGER", nullable=False),
                Column("heading", "TEXT", nullable=False),
            ],
            store_metadata=False,
            overwrite_existing=reset,
        )
        self._vectorstore = None
        self.create_text_index()

    def create_text_index(self) -> None:
        """A trigram index makes "does this word occur anywhere" a fast lookup instead of a scan of every chunk."""
        with self.sql.begin() as conn:
            conn.execute(sqlalchemy.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.execute(sqlalchemy.text(f'CREATE INDEX IF NOT EXISTS "{self.table}_content_trgm_idx" ON "{self.table}" USING gin (content gin_trgm_ops)'))

    def drop_text_index(self) -> None:
        with self.sql.begin() as conn:
            conn.execute(sqlalchemy.text(f'DROP INDEX IF EXISTS "{self.table}_content_trgm_idx"'))

    @property
    def vectorstore(self) -> PGVectorStore:
        if self._vectorstore is None:
            self._vectorstore = PGVectorStore.create_sync(
                self.engine,
                self.embeddings,
                self.table,
                metadata_columns=METADATA,
                metadata_json_column=None,
                index_query_options=HNSWQueryOptions(ef_search=100) if self.use_index else None,
            )
        return self._vectorstore

    def _scalar(self, sql: str, **params):
        with self.sql.connect() as conn:
            return conn.execute(sqlalchemy.text(sql), params).scalar()

    def count(self) -> int:
        return int(self._scalar(f'SELECT count(*) FROM "{self.table}"')) if self.exists() else 0

    def stats(self) -> dict:
        return {
            "chunks": self.count(),
            "collection_bytes": int(self._scalar(f'SELECT coalesce(sum(pg_column_size(c.*)), 0) FROM "{self.table}" c')),
            "table_with_indexes_bytes": int(self._scalar("SELECT pg_total_relation_size(CAST(:t AS regclass))", t=self.table)),
        }

    def create_index(self) -> float:
        """Build an HNSW index for cosine distance if there is none. Returns the build time in seconds."""
        start = time.perf_counter()
        if not self._scalar("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :name)", name=f"{self.table}_hnsw_idx"):
            self.vectorstore.apply_vector_index(HNSWIndex(name=f"{self.table}_hnsw_idx"))
        return time.perf_counter() - start

    def drop_index(self) -> None:
        with self.sql.begin() as conn:
            conn.execute(sqlalchemy.text(f'DROP INDEX IF EXISTS "{self.table}_hnsw_idx"'))

    def missing_terms(self, terms: list[str]) -> list[str]:
        """Terms that occur in no chunk of the collection (case-insensitive substring match)."""
        sql = f'SELECT EXISTS (SELECT 1 FROM "{self.table}" WHERE content ILIKE :pattern)'
        return [term for term in terms if not self._scalar(sql, pattern=f"%{term}%")]
