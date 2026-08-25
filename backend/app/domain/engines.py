from enum import StrEnum


class DatabaseEngine(StrEnum):
    POSTGRESQL = "postgresql"
    SQLSERVER = "sqlserver"
    MONGODB = "mongodb"


DEFAULT_PORTS: dict[DatabaseEngine, int] = {
    DatabaseEngine.POSTGRESQL: 5432,
    DatabaseEngine.SQLSERVER: 1433,
    DatabaseEngine.MONGODB: 27017,
}

DEFAULT_DATABASES: dict[DatabaseEngine, str] = {
    DatabaseEngine.POSTGRESQL: "postgres",
    DatabaseEngine.SQLSERVER: "master",
    DatabaseEngine.MONGODB: "admin",
}
