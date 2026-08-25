CREATE TABLE IF NOT EXISTS servers (
    id BIGSERIAL PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL,
    host VARCHAR(255) NOT NULL,
    os VARCHAR(16) NOT NULL DEFAULT 'linux',
    site VARCHAR(16) NOT NULL DEFAULT 'primary',
    agent_url VARCHAR(255),
    agent_token VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (customer_id, name)
);

CREATE INDEX IF NOT EXISTS ix_servers_customer_id ON servers (customer_id);

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS server_id BIGINT REFERENCES servers(id);
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS instance_name VARCHAR(128);

-- One-time backfill: a Server per distinct (customer, host) among existing nodes, then point
-- every node at it. Safe to re-run (WHERE server_id IS NULL / host column existence guards).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'nodes' AND column_name = 'host') THEN
        INSERT INTO servers (customer_id, name, host, os, site, agent_url, agent_token)
        SELECT DISTINCT c.id, c.name || '-' || n.host, n.host, 'linux',
               COALESCE((array_agg(n.site))[1], 'primary'), (array_agg(n.agent_url))[1], (array_agg(n.agent_token))[1]
        FROM nodes n
        JOIN database_groups g ON g.id = n.group_id
        JOIN applications a ON a.id = g.application_id
        JOIN customers c ON c.id = a.customer_id
        WHERE n.server_id IS NULL
        GROUP BY c.id, n.host, c.name
        ON CONFLICT (customer_id, name) DO NOTHING;

        UPDATE nodes n
        SET server_id = s.id
        FROM database_groups g, applications a, customers c, servers s
        WHERE n.group_id = g.id AND g.application_id = a.id AND a.customer_id = c.id
          AND s.customer_id = c.id AND s.host = n.host
          AND n.server_id IS NULL;

        ALTER TABLE nodes DROP COLUMN IF EXISTS host;
        ALTER TABLE nodes DROP COLUMN IF EXISTS site;
        ALTER TABLE nodes DROP COLUMN IF EXISTS agent_url;
        ALTER TABLE nodes DROP COLUMN IF EXISTS agent_token;
    END IF;
END $$;
