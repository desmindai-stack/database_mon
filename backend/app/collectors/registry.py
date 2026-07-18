from app.collectors.base import BaseCollector, ConnectionTarget
from app.collectors.postgresql import PostgreSQLCollector
from app.collectors.sqlserver_mongodb import MongoDBCollector, SqlServerCollector
from app.domain.engines import DatabaseEngine


def get_collector(engine: DatabaseEngine, target: ConnectionTarget) -> BaseCollector:
    if engine == DatabaseEngine.POSTGRESQL:
        return PostgreSQLCollector(target)
    if engine == DatabaseEngine.SQLSERVER:
        return SqlServerCollector(target)
    if engine == DatabaseEngine.MONGODB:
        return MongoDBCollector(target)
    raise ValueError(f"Unsupported engine: {engine}")
