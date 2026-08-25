from enum import StrEnum


class CustomerType(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class GroupTopology(StrEnum):
    STANDALONE = "standalone"
    PATRONI = "patroni"
    ALWAYSON = "alwayson"


class GroupEnvironment(StrEnum):
    PROD = "prod"
    PREPROD = "preprod"
    TEST = "test"
    DEV = "dev"


class NodeSite(StrEnum):
    PRIMARY = "primary"
    DISASTER = "disaster"


class ServerOS(StrEnum):
    LINUX = "linux"
    WINDOWS = "windows"


class NodeRoleHint(StrEnum):
    PRIMARY = "primary"
    REPLICA = "replica"
    UNKNOWN = "unknown"
