from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.pool import NullPool

from app.core import database


def test_asyncpg_statement_caches_are_disabled_for_pgbouncer(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected_engine = object()
    settings = SimpleNamespace(
        database_echo=False,
        database_use_null_pool=True,
        sqlalchemy_async_dsn="postgresql+asyncpg://test:test@localhost/test",
    )

    def _create_engine(dsn: str, **kwargs: object) -> object:
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return expected_engine

    database.get_engine.cache_clear()
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    monkeypatch.setattr(database, "create_async_engine", _create_engine)

    try:
        assert database.get_engine() is expected_engine
    finally:
        database.get_engine.cache_clear()

    assert captured["dsn"] == settings.sqlalchemy_async_dsn
    engine_kwargs = captured["kwargs"]
    assert isinstance(engine_kwargs, dict)
    assert engine_kwargs["poolclass"] is NullPool

    connect_args = engine_kwargs["connect_args"]
    assert isinstance(connect_args, dict)
    assert connect_args["statement_cache_size"] == 0
    assert connect_args["prepared_statement_cache_size"] == 0
    assert connect_args["server_settings"] == {"application_name": "orch"}
