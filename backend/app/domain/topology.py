from enum import StrEnum


class CustomerType(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class GroupTopology(StrEnum):
    STANDALONE = "standalone"
    PATRONI = "patroni"
    ALWAYSON = "alwayson"


class NodeSite(StrEnum):
    PRIMARY = "primary"
    DISASTER = "disaster"


class NodeRoleHint(StrEnum):
    PRIMARY = "primary"
    REPLICA = "replica"
    UNKNOWN = "unknown"
