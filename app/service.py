"""Transactional SQLite-backed claim service."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .ownership import (
    OwnershipConflictError,
    config_view,
    merge_claim,
    ownership_view,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(
    conn: sqlite3.Connection, deployment: str
) -> tuple[int, dict[str, Any], dict[str, list[str]]]:
    """Load committed version, flat config values, and ordered owners."""
    meta = conn.execute(
        "SELECT version FROM deployment_state WHERE deployment = ?",
        (deployment,),
    ).fetchone()
    version = int(meta["version"]) if meta is not None else 0

    values: dict[str, Any] = {}
    for row in conn.execute(
        "SELECT field_key, value_json FROM config_field WHERE deployment = ?",
        (deployment,),
    ):
        values[row["field_key"]] = json.loads(row["value_json"])

    owners: dict[str, list[str]] = {}
    for row in conn.execute(
        """
        SELECT field_key, manager
        FROM field_owner
        WHERE deployment = ?
        ORDER BY field_key ASC, position ASC
        """,
        (deployment,),
    ):
        owners.setdefault(row["field_key"], []).append(row["manager"])

    return version, values, owners


def _save_state(
    conn: sqlite3.Connection,
    deployment: str,
    new_version: int,
    values: dict[str, Any],
    owners: dict[str, list[str]],
) -> None:
    """Replace deployment rows; caller owns the surrounding transaction."""
    now = _utc_now()

    conn.execute(
        """
        INSERT INTO deployment_state (deployment, version, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(deployment) DO UPDATE SET
            version = excluded.version,
            updated_at = excluded.updated_at
        """,
        (deployment, new_version, now),
    )

    # Delete owners first because of the composite foreign key.
    conn.execute("DELETE FROM field_owner WHERE deployment = ?", (deployment,))
    conn.execute("DELETE FROM config_field WHERE deployment = ?", (deployment,))

    for field_key, value in values.items():
        conn.execute(
            """
            INSERT INTO config_field
                (deployment, field_key, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (deployment, field_key, json.dumps(value, ensure_ascii=False), now),
        )

        for position, manager in enumerate(owners.get(field_key, [])):
            conn.execute(
                """
                INSERT INTO field_owner
                    (deployment, field_key, manager, position, granted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (deployment, field_key, manager, position, now),
            )


def state_payload(
    deployment: str,
    version: int,
    values: dict[str, Any],
    owners: dict[str, list[str]],
) -> dict[str, Any]:
    config = config_view(values)
    ownership = ownership_view(owners)
    return {
        "deployment": deployment,
        "version": version,
        "config": config,
        "merged_config": config,
        "ownership": ownership,
    }


def get_state(conn: sqlite3.Connection, deployment: str) -> dict[str, Any]:
    version, values, owners = load_state(conn, deployment)
    return state_payload(deployment, version, values, owners)


def conflict_payload(error: OwnershipConflictError, deployment: str) -> dict[str, Any]:
    state = state_payload(
        deployment, error.version, error.values, error.owners
    )
    message = (
        "声明与最新已生效状态存在归属冲突，整份声明未生效。"
        "请查看 conflicts 中的字段和管理方；如确需改值，可使用 takeover=true 显式接管。"
    )
    detail = {
        "code": "ownership_conflict",
        "message": message,
        "conflicts": error.conflicts,
    }
    return {
        "accepted": False,
        "code": "ownership_conflict",
        "message": message,
        "conflicts": error.conflicts,
        "detail": detail,
        **state,
    }


def submit_claim(
    conn: sqlite3.Connection,
    deployment: str,
    manager: str,
    declared_values: dict[str, Any],
    *,
    takeover: bool,
) -> dict[str, Any]:
    """Run one claim in a serializable SQLite write transaction.

    ``BEGIN IMMEDIATE`` takes the database write lock before reading state.
    Therefore concurrent writers wait, then read the latest committed config
    and ownership before judging conflicts. Config and ownership are replaced
    and committed together.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        version, values, owners = load_state(conn, deployment)
        try:
            result = merge_claim(
                values,
                owners,
                manager,
                declared_values,
                takeover=takeover,
            )
        except OwnershipConflictError as error:
            error.version = version
            raise

        new_version = version + 1
        _save_state(
            conn,
            deployment,
            new_version,
            result.values,
            result.owners,
        )
        conn.execute("COMMIT")
    except OwnershipConflictError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise

    payload = state_payload(
        deployment, new_version, result.values, result.owners
    )
    payload.update(
        {
            "accepted": True,
            "manager": manager,
            "takeover": takeover,
            "changes": result.changes,
            "message": "声明已按最新已生效状态校验，并与归属一起原子提交。",
        }
    )
    return payload


def reset_state(conn: sqlite3.Connection, deployment: str) -> dict[str, Any]:
    """Delete all rehearsal rows for one deployment."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM field_owner WHERE deployment = ?", (deployment,))
        conn.execute("DELETE FROM config_field WHERE deployment = ?", (deployment,))
        conn.execute(
            "DELETE FROM deployment_state WHERE deployment = ?", (deployment,)
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "deployment": deployment,
        "version": 0,
        "config": {},
        "merged_config": {},
        "ownership": {},
        "reset": True,
    }
