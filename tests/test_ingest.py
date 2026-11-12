"""An interrupted ingest must not be mistaken for a finished one."""

import sqlalchemy

from ragdemo.embed import HashEmbeddings
from ragdemo.store import Store


def test_collection_is_complete_only_after_it_is_marked(pipeline):
    store = pipeline.retriever.store
    assert store.count() == 3  # the fixture stored chunks without finishing an ingest
    assert store.is_complete() is False
    store.mark_complete()
    store.mark_complete()  # marking twice is harmless
    assert store.is_complete() is True
    assert Store(store.url, HashEmbeddings(), "another").is_complete() is False


def test_resetting_a_collection_clears_the_mark(pipeline):
    store = Store(pipeline.retriever.store.url, HashEmbeddings(), pipeline.collection + "_scratch")
    store.init_schema(reset=True)
    store.mark_complete()
    store.init_schema(reset=True)
    try:
        assert store.count() == 0
        assert store.is_complete() is False
    finally:
        with store.sql.begin() as conn:
            conn.execute(sqlalchemy.text(f'DROP TABLE IF EXISTS "{store.table}"'))
