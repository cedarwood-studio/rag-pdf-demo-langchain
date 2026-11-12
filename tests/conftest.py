"""Fixtures: a three-page PDF made on the fly and a pgvector table filled from it through LangChain.

The tests use the hash embedder so they need no model download. They do need a
Postgres with pgvector, reached through DATABASE_URL.
"""

import os
import uuid

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from ragdemo.answer import extractive_chain
from ragdemo.api import app, get_pipeline
from ragdemo.embed import HashEmbeddings
from ragdemo.ingest import ingest_file
from ragdemo.pipeline import Pipeline
from ragdemo.retrieval import make_retriever
from ragdemo.store import Store

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://rag:rag@localhost:54429/rag")

PAGES = [
    ("1. Returns", [
        "Customers may return a kettle within thirty days of delivery.",
        "The kettle must be unused and in its original box.",
    ]),
    ("2. Descaling", [
        "Descale the kettle every two months using citric acid.",
        "Fill the kettle halfway, add two spoons of citric acid, boil and rinse twice.",
    ]),
    ("3. Warranty", [
        "The heating element carries a warranty of three years.",
        "The warranty does not cover damage from limescale.",
    ]),
]


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("pdf") / "kettle-guide.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    for heading, lines in PAGES:
        c.setFont("Helvetica", 8)
        c.drawString(72, 760, "Kettle Guide")
        c.setFont("Helvetica-Bold", 16)
        c.drawString(72, 720, heading)
        c.setFont("Helvetica", 11)
        y = 690
        for line in lines:
            c.drawString(72, y, line)
            y -= 16
        c.showPage()
    c.save()
    return path


@pytest.fixture(scope="session")
def pipeline(sample_pdf):
    store = Store(DATABASE_URL, HashEmbeddings(), "test_" + uuid.uuid4().hex[:8])
    try:
        store.init_schema(reset=True)
    except sqlalchemy.exc.OperationalError as error:
        pytest.skip(f"Postgres with pgvector is not reachable at DATABASE_URL: {error}")
    ingest_file(store, sample_pdf, "kettle-guide.pdf", "Kettle Guide", "heading")
    yield Pipeline(make_retriever(store, top_k=3), extractive_chain(), "extractive", threshold=0.25, max_citations=2)
    with store.sql.begin() as conn:
        conn.execute(sqlalchemy.text(f'DROP TABLE IF EXISTS "{store.table}"'))


@pytest.fixture()
def client(pipeline):
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    yield TestClient(app)
    app.dependency_overrides.clear()
