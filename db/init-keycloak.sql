-- Отдельная БД под Keycloak в том же Postgres-инстансе, что и cost_journal.
-- Выполняется автоматически при первой инициализации тома (docker-entrypoint-initdb.d).
CREATE DATABASE keycloak OWNER ape;
