-- Multi-tenant, multi-node topology: Customer -> Application -> DatabaseGroup -> Node
-- Faz 1 (feature/multi-tenant-cluster). Instance kayıtları geriye dönük uyumlu kalır;
-- group_id nullable'dır, mevcut instance'lar bir gruba bağlanmadan çalışmaya devam eder.

CREATE TABLE IF NOT EXISTS customers (
  id BIGSERIAL PRIMARY KEY,
  name VARCHAR(128) NOT NULL UNIQUE,
  type VARCHAR(16) NOT NULL DEFAULT 'public',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS applications (
  id BIGSERIAL PRIMARY KEY,
  customer_id BIGINT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name VARCHAR(128) NOT NULL,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (customer_id, name)
);

CREATE INDEX IF NOT EXISTS idx_applications_customer_id ON applications(customer_id);

CREATE TABLE IF NOT EXISTS database_groups (
  id BIGSERIAL PRIMARY KEY,
  application_id BIGINT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  name VARCHAR(128) NOT NULL,
  engine VARCHAR(32) NOT NULL DEFAULT 'postgresql',
  topology VARCHAR(32) NOT NULL DEFAULT 'standalone',
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (application_id, name)
);

CREATE INDEX IF NOT EXISTS idx_database_groups_application_id ON database_groups(application_id);

CREATE TABLE IF NOT EXISTS nodes (
  id BIGSERIAL PRIMARY KEY,
  group_id BIGINT NOT NULL REFERENCES database_groups(id) ON DELETE CASCADE,
  name VARCHAR(128) NOT NULL,
  host VARCHAR(255) NOT NULL,
  port INTEGER NOT NULL,
  site VARCHAR(16) NOT NULL DEFAULT 'primary',
  role_hint VARCHAR(16) NOT NULL DEFAULT 'unknown',
  agent_url VARCHAR(255),
  agent_token VARCHAR(255),
  options JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (group_id, name)
);

CREATE INDEX IF NOT EXISTS idx_nodes_group_id ON nodes(group_id);

ALTER TABLE instances ADD COLUMN IF NOT EXISTS group_id BIGINT REFERENCES database_groups(id);

CREATE INDEX IF NOT EXISTS idx_instances_group_id ON instances(group_id);
