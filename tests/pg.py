"""Helpers for starting throwaway Postgres 17 containers (testcontainers)."""

from testcontainers.community.postgres import PostgresContainer

POSTGRES_IMAGE = "postgres:17"
DB_NAME = DB_USER = "service_b"
DB_PASSWORD = "test-password"

# The official image ships a self-signed "snakeoil" cert; turning SSL on lets the IAM-mode
# test use sslmode=require just like Aurora, while other tests connect without SSL.
SSL_ARGS = [
    "-c", "ssl=on",
    "-c", "ssl_cert_file=/etc/ssl/certs/ssl-cert-snakeoil.pem",
    "-c", "ssl_key_file=/etc/ssl/private/ssl-cert-snakeoil.key",
]  # fmt: skip


def postgres_container() -> PostgresContainer:
    return PostgresContainer(
        POSTGRES_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME, driver="psycopg"
    )


def db_env(pg: PostgresContainer) -> dict[str, str]:
    """DB_* variables that point at `pg` from the host running pytest."""
    return {
        "DB_HOST": pg.get_container_host_ip(),
        "DB_PORT": str(pg.get_exposed_port(5432)),
        "DB_NAME": pg.dbname,
        "DB_USER": pg.username,
        "DB_AUTH": "password",
        "DB_PASSWORD": pg.password,
        "DB_SSLMODE": "disable",
    }
