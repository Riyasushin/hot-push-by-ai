"""Pipeline package — each module is one ingestion step.

Steps are duck-typed: each exposes a class with ``name: str`` and
``run(conn) -> dict``. The CLI dispatches to them directly. There used to be
a ``Step`` Protocol here but nothing referenced it as a type annotation, so
it was removed (see refactor 2026-05-07).
"""
