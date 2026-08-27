-- Runs once, on first container start.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE DATABASE doctask_test OWNER doctask;
\connect doctask_test
CREATE EXTENSION IF NOT EXISTS vector;
