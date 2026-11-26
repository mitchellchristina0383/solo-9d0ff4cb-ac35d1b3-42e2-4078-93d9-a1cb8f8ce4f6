"""Pure ownership and claim-merge rules.

The rehearsal state has flat field keys:

* ``replicas``
* ``images.<container-name>``

A field may have several owners only when every owner submitted the same value.
Changing a field that still has another owner is a conflict unless the incoming
claim explicitly takes over; a taken-over field becomes owned only by the
submitter.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

REPLICAS_FIELD = "replicas"
IMAGE_PREFIX = "images."

CHANGE_KEYS = (
    "created",
    "updated",
    "co_managed",
    "taken_over",
    "unchanged",
    "released",
    "deleted",
)


def image_field(container_name: str) -> str:
    return f"{IMAGE_PREFIX}{container_name}"


def is_image_field(field_key: str) -> bool:
    return field_key.startswith(IMAGE_PREFIX)


def container_name(field_key: str) -> str:
    return field_key[len(IMAGE_PREFIX) :]


def empty_changes() -> dict[str, list[str]]:
    return {key: [] for key in CHANGE_KEYS}


@dataclass
class FieldConflict:
    field: str
    submitted: Any
    current: Any
    managers: list[str]
    other_managers: list[str]


class OwnershipConflictError(Exception):
    """Raised when an entire claim must be returned without changes."""

    def __init__(
        self,
        conflicts: list[FieldConflict],
        *,
        version: int,
        values: dict[str, Any],
        owners: dict[str, list[str]],
    ) -> None:
        super().__init__(f"Ownership conflict on {len(conflicts)} field(s)")
        self.conflicts = [asdict(conflict) for conflict in conflicts]
        self.version = version
        self.values = deepcopy(values)
        self.owners = {key: list(managers) for key, managers in owners.items()}


@dataclass
class MergeResult:
    values: dict[str, Any]
    owners: dict[str, list[str]]
    changes: dict[str, list[str]] = field(default_factory=empty_changes)


def merge_claim(
    current_values: dict[str, Any],
    current_owners: dict[str, list[str]],
    manager: str,
    declared_values: dict[str, Any],
    *,
    takeover: bool = False,
) -> MergeResult:
    """Merge one manager's full declaration using the latest committed state.

    This function is pure. The caller must serialize concurrent invocations for
    the same deployment (the service uses ``BEGIN IMMEDIATE``) and persist the
    returned values and owners in one SQLite transaction.
    """
    values = deepcopy(current_values)
    owners = {key: list(managers) for key, managers in current_owners.items()}
    changes = empty_changes()

    conflicts: list[FieldConflict] = []

    # First pass: collect every blocking conflict so the API can return the
    # complete list and reject the whole claim.
    for field_key, submitted in declared_values.items():
        if field_key not in values:
            continue

        current_value = values[field_key]
        current_managers = owners.get(field_key, [])
        if submitted == current_value:
            continue

        other_managers = [name for name in current_managers if name != manager]
        if other_managers and not takeover:
            conflicts.append(
                FieldConflict(
                    field=field_key,
                    submitted=submitted,
                    current=current_value,
                    managers=list(current_managers),
                    other_managers=other_managers,
                )
            )

    if conflicts:
        raise OwnershipConflictError(
            conflicts,
            version=-1,  # Filled with the real version by the transactional service.
            values=current_values,
            owners=current_owners,
        )

    # Second pass: apply declared fields.
    for field_key, submitted in declared_values.items():
        old_managers = owners.get(field_key, [])

        if field_key not in values:
            values[field_key] = submitted
            owners[field_key] = [manager]
            changes["created"].append(field_key)
            continue

        if submitted == values[field_key]:
            if manager in old_managers:
                changes["unchanged"].append(field_key)
            else:
                owners[field_key] = [*old_managers, manager]
                changes["co_managed"].append(field_key)
            continue

        # The value differs. Any remaining other manager would have produced a
        # conflict above unless explicit takeover was requested.
        other_managers = [name for name in old_managers if name != manager]
        values[field_key] = submitted
        changes["updated"].append(field_key)

        if takeover and other_managers:
            owners[field_key] = [manager]
            changes["taken_over"].append(field_key)
        elif manager in old_managers:
            owners[field_key] = list(old_managers)
        else:
            # An unowned/orphaned field can be claimed directly.
            owners[field_key] = [manager]

    # Omission is an explicit exit only for fields currently managed by this
    # manager. A field with remaining co-owners stays in the merged config.
    for field_key in list(values.keys()):
        if field_key in declared_values:
            continue

        current_managers = owners.get(field_key, [])
        if manager not in current_managers:
            continue

        remaining = [name for name in current_managers if name != manager]
        if remaining:
            owners[field_key] = remaining
            changes["released"].append(field_key)
        else:
            del values[field_key]
            owners.pop(field_key, None)
            changes["deleted"].append(field_key)

    for items in changes.values():
        items.sort()

    return MergeResult(values=values, owners=owners, changes=changes)


def config_view(flat_values: dict[str, Any]) -> dict[str, Any]:
    """Convert flat storage keys to the nested Deployment-style config."""
    view: dict[str, Any] = {}
    if REPLICAS_FIELD in flat_values:
        view[REPLICAS_FIELD] = flat_values[REPLICAS_FIELD]

    images: dict[str, Any] = {}
    for field_key in sorted(flat_values):
        if is_image_field(field_key):
            images[container_name(field_key)] = flat_values[field_key]
    if images:
        view["images"] = images
    return view


def ownership_view(flat_owners: dict[str, list[str]]) -> dict[str, Any]:
    """Convert flat ownership keys to nested ownership output."""
    view: dict[str, Any] = {}
    if REPLICAS_FIELD in flat_owners:
        view[REPLICAS_FIELD] = list(flat_owners[REPLICAS_FIELD])

    images: dict[str, list[str]] = {}
    for field_key in sorted(flat_owners):
        if is_image_field(field_key):
            images[container_name(field_key)] = list(flat_owners[field_key])
    if images:
        view["images"] = images
    return view
