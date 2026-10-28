"""Copy CarbonCheck SQLite records into an empty MySQL database."""
import argparse
import configparser
import os
import sqlite3
import sys

import pymysql
from pymysql.cursors import DictCursor


def mysql_connection():
    cnf_path = os.environ.get(
        "CARBONCHECK_MYSQL_CNF",
        os.path.expanduser("~/.config/carboncheck/mysql.cnf"),
    )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(cnf_path)
    client = parser["client"] if parser.has_section("client") else {}
    settings = {
        "host": os.environ.get("MYSQL_HOST", client.get("host", "localhost")),
        "port": int(os.environ.get("MYSQL_PORT", client.get("port", 3306))),
        "user": os.environ.get("MYSQL_USER", client.get("user", "")),
        "password": os.environ.get("MYSQL_PASSWORD", client.get("password", "")),
        "database": os.environ.get(
            "MYSQL_DATABASE", client.get("database", "carboncheck")
        ),
    }
    if not settings["user"] or not settings["password"]:
        raise RuntimeError(
            "Configure MYSQL_USER/MYSQL_PASSWORD ou CARBONCHECK_MYSQL_CNF."
        )
    return pymysql.connect(
        **settings,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=10,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=os.path.join(os.path.dirname(__file__), "instance", "carboncheck.sqlite3"),
        help="arquivo SQLite de origem",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.source):
        parser.error(f"arquivo SQLite não encontrado: {args.source}")

    source = sqlite3.connect(args.source)
    source.row_factory = sqlite3.Row
    destination = mysql_connection()
    try:
        usuarios = source.execute(
            "SELECT id, nome, email, perfil, hash, ativo FROM usuarios ORDER BY id"
        ).fetchall()
        inventarios = source.execute(
            "SELECT id, org_id, periodo, status, dados FROM inventarios ORDER BY id"
        ).fetchall()

        with destination.cursor() as cursor:
            cursor.execute(
                """SELECT table_name AS table_name FROM information_schema.tables
                   WHERE table_schema = DATABASE()"""
            )
            existing_tables = {row["table_name"] for row in cursor.fetchall()}
            for table in ("usuarios", "inventarios"):
                if table in existing_tables:
                    cursor.execute(f"SELECT COUNT(*) AS total FROM `{table}`")
                    if cursor.fetchone()["total"]:
                        raise RuntimeError(
                            f"O destino MySQL já contém dados em {table}; "
                            "migração cancelada sem alterar dados."
                        )
            cursor.execute("""CREATE TABLE IF NOT EXISTS usuarios (
                id VARCHAR(32) PRIMARY KEY, nome VARCHAR(255) NOT NULL,
                email VARCHAR(320) NOT NULL UNIQUE, perfil VARCHAR(40) NOT NULL,
                hash VARCHAR(255) NOT NULL, ativo TINYINT(1) NOT NULL DEFAULT 1
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS inventarios (
                id VARCHAR(32) PRIMARY KEY, org_id VARCHAR(32) NOT NULL,
                periodo SMALLINT NOT NULL, status VARCHAR(40) NOT NULL,
                dados LONGTEXT NOT NULL, UNIQUE KEY uq_inventarios_org_periodo (org_id, periodo),
                INDEX ix_inventarios_org (org_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            destination.begin()

            cursor.executemany(
                """INSERT INTO usuarios (id, nome, email, perfil, hash, ativo)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [
                    (
                        row["id"], row["nome"], row["email"], row["perfil"],
                        row["hash"], row["ativo"],
                    )
                    for row in usuarios
                ],
            )
            cursor.executemany(
                """INSERT INTO inventarios (id, org_id, periodo, status, dados)
                   VALUES (%s, %s, %s, %s, %s)""",
                [
                    (
                        row["id"], row["org_id"], row["periodo"],
                        row["status"], row["dados"],
                    )
                    for row in inventarios
                ],
            )

        destination.commit()
        print(
            f"Migração concluída: {len(usuarios)} usuários e "
            f"{len(inventarios)} inventários copiados."
        )
    except Exception:
        destination.rollback()
        raise
    finally:
        source.close()
        destination.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Falha na migração: {exc}", file=sys.stderr)
        sys.exit(1)
