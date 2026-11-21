CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS routing_advisor;

CREATE TABLE IF NOT EXISTS routing_advisor.history (
    incident_id text PRIMARY KEY,
    status text NOT NULL,
    service text NOT NULL,
    infrastructure text[] NOT NULL,
    site text NOT NULL,
    title text NOT NULL,
    description text NOT NULL,
    outcome text,
    resolved_by text,
    first_assigned_group text,
    embedding vector(384) NOT NULL
);

CREATE TABLE IF NOT EXISTS routing_advisor.owners (
    service text NOT NULL,
    site text NOT NULL,
    owner_group text NOT NULL CHECK (btrim(owner_group) <> ''),
    PRIMARY KEY (service, site, owner_group)
);
