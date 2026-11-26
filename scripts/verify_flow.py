#!/usr/bin/env python3
"""End-to-end verification using only the Python standard library.

It exercises:
1. co-management, partial exit, last-owner deletion;
2. conflict listing, whole-claim rejection, and explicit takeover;
3. concurrent same-value claims (no lost owners);
4. a concurrent takeover vs. conflicting change (latest committed state).

Usage:
    python scripts/verify_flow.py [base_url]
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
failures: list[str] = []


def check(name: str, condition: bool, detail: Any = "") -> None:
    if condition:
        print(f"PASS  {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name}\n      detail={detail!r}")


def request(
    method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"raw": raw}


def post_claim(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return request("POST", "/claims", body)


def get_state(deployment: str) -> tuple[int, dict[str, Any]]:
    query = urllib.parse.urlencode({"deployment": deployment})
    return request("GET", f"/state?{query}")


def wait_for_health() -> None:
    last_error: Exception | None = None
    for _ in range(100):
        try:
            status, body = request("GET", "/health")
            if status == 200 and body.get("status") == "ok":
                print(f"Service is ready at {BASE_URL}")
                return
        except Exception as error:  # noqa: BLE001 - retry until timeout
            last_error = error
        time.sleep(0.2)
    raise RuntimeError(f"service did not become ready: {last_error}")


def verify_co_management_and_exit() -> None:
    dep = f"coexit-{uuid.uuid4().hex[:8]}"
    print(f"\n== co-management and exit: {dep} ==")

    status, app_first = post_claim(
        {
            "manager": "app",
            "deployment": dep,
            "replicas": 3,
            "images": {"web": "nginx:1.27"},
        }
    )
    check("app creates replicas and image", status == 200, (status, app_first))
    check(
        "initial config",
        app_first.get("config")
        == {"replicas": 3, "images": {"web": "nginx:1.27"}},
        app_first,
    )
    check(
        "initial ownership",
        app_first.get("ownership")
        == {"replicas": ["app"], "images": {"web": ["app"]}},
        app_first.get("ownership"),
    )

    status, platform_same = post_claim(
        {
            "manager": "platform",
            "deployment": dep,
            "replicas": 3,
            "images": {"web": "nginx:1.27"},
        }
    )
    check("same-value claim succeeds", status == 200, (status, platform_same))
    check(
        "both fields become co-managed",
        platform_same.get("ownership")
        == {
            "replicas": ["app", "platform"],
            "images": {"web": ["app", "platform"]},
        },
        platform_same.get("ownership"),
    )
    check(
        "co_managed change list",
        platform_same.get("changes", {}).get("co_managed")
        == ["images.web", "replicas"],
        platform_same.get("changes"),
    )

    # Platform keeps its image field but omits replicas: it exits only replicas.
    status, platform_partial = post_claim(
        {
            "manager": "platform",
            "deployment": dep,
            "images": {"web": "nginx:1.27"},
        }
    )
    check(
        "partial declaration succeeds",
        status == 200,
        (status, platform_partial),
    )
    check(
        "omitted replicas is released by platform",
        platform_partial.get("changes", {}).get("released") == ["replicas"],
        platform_partial.get("changes"),
    )
    check(
        "replicas survives with remaining owner",
        platform_partial.get("ownership", {}).get("replicas") == ["app"]
        and platform_partial.get("config", {}).get("replicas") == 3,
        platform_partial,
    )
    check(
        "image remains co-managed",
        platform_partial.get("ownership", {}).get("images", {}).get("web")
        == ["app", "platform"],
        platform_partial.get("ownership"),
    )

    # App omits replicas while being its last owner: field is deleted.
    status, app_partial = post_claim(
        {
            "manager": "app",
            "deployment": dep,
            "images": {"web": "nginx:1.27"},
        }
    )
    check(
        "last owner omission deletes replicas",
        status == 200
        and app_partial.get("changes", {}).get("deleted") == ["replicas"]
        and "replicas" not in app_partial.get("config", {}),
        (status, app_partial),
    )
    check(
        "image survives replicas deletion",
        app_partial.get("config", {}).get("images", {}).get("web")
        == "nginx:1.27",
        app_partial.get("config"),
    )

    # Platform exits image while app remains.
    status, platform_exit = post_claim(
        {"manager": "platform", "deployment": dep}
    )
    check(
        "one image co-owner can exit without deleting field",
        status == 200
        and platform_exit.get("changes", {}).get("released") == ["images.web"]
        and platform_exit.get("ownership", {}).get("images", {}).get("web")
        == ["app"],
        (status, platform_exit),
    )

    # Last owner exits: merged config and ownership are empty.
    status, app_exit = post_claim({"manager": "app", "deployment": dep})
    check(
        "last image owner exit deletes field",
        status == 200
        and app_exit.get("changes", {}).get("deleted") == ["images.web"]
        and app_exit.get("config") == {}
        and app_exit.get("ownership") == {},
        (status, app_exit),
    )

    status, state = get_state(dep)
    check(
        "persisted final state is empty",
        status == 200 and state.get("config") == {} and state.get("ownership") == {},
        (status, state),
    )


def verify_conflict_and_takeover() -> None:
    dep = f"conflict-{uuid.uuid4().hex[:8]}"
    print(f"\n== conflict and takeover: {dep} ==")

    status, created = post_claim(
        {
            "manager": "app",
            "deployment": dep,
            "replicas": 3,
            "images": {"web": "v1"},
        }
    )
    check("conflict fixture created", status == 200, (status, created))
    version_after_create = created.get("version")

    status, replica_conflict = post_claim(
        {"manager": "platform", "deployment": dep, "replicas": 4}
    )
    replica_items = replica_conflict.get("conflicts", [])
    check(
        "other-manager replica change returns 409",
        status == 409
        and len(replica_items) == 1
        and replica_items[0].get("field") == "replicas"
        and replica_items[0].get("managers") == ["app"]
        and replica_items[0].get("other_managers") == ["app"],
        (status, replica_conflict),
    )

    status, image_conflict = post_claim(
        {
            "manager": "platform",
            "deployment": dep,
            "images": {"web": "v2"},
        }
    )
    check(
        "image conflict names container field and manager",
        status == 409
        and image_conflict.get("conflicts", [{}])[0].get("field")
        == "images.web"
        and image_conflict.get("conflicts", [{}])[0].get("managers") == ["app"],
        (status, image_conflict),
    )

    status, state_after_rejections = get_state(dep)
    check(
        "rejected claims do not change config or version",
        state_after_rejections.get("version") == version_after_create
        and state_after_rejections.get("config", {}).get("replicas") == 3
        and state_after_rejections.get("config", {})
        .get("images", {})
        .get("web") == "v1",
        state_after_rejections,
    )

    status, co_claim = post_claim(
        {
            "manager": "platform",
            "deployment": dep,
            "replicas": 3,
            "images": {"web": "v1"},
        }
    )
    check("same value creates co-ownership", status == 200, (status, co_claim))

    status, co_owner_change = post_claim(
        {"manager": "app", "deployment": dep, "replicas": 5}
    )
    check(
        "changing a co-managed field conflicts with other owner",
        status == 409
        and co_owner_change.get("conflicts", [{}])[0].get("field") == "replicas"
        and co_owner_change.get("conflicts", [{}])[0].get("other_managers")
        == ["platform"],
        (status, co_owner_change),
    )

    status, multi_conflict = post_claim(
        {
            "manager": "infra",
            "deployment": dep,
            "replicas": 9,
            "images": {"web": "v9"},
        }
    )
    fields = {item.get("field") for item in multi_conflict.get("conflicts", [])}
    check(
        "whole multi-field claim is returned with all conflicts",
        status == 409 and fields == {"replicas", "images.web"},
        (status, multi_conflict),
    )

    status, takeover = post_claim(
        {
            "manager": "platform",
            "deployment": dep,
            "replicas": 5,
            "images": {"web": "v1"},
            "takeover": True,
        }
    )
    check(
        "explicit takeover succeeds",
        status == 200
        and takeover.get("config", {}).get("replicas") == 5
        and takeover.get("ownership", {}).get("replicas") == ["platform"]
        and takeover.get("changes", {}).get("taken_over") == ["replicas"],
        (status, takeover),
    )
    check(
        "takeover only affects changed conflict field",
        takeover.get("ownership", {}).get("images", {}).get("web")
        == ["app", "platform"],
        takeover.get("ownership"),
    )

    status, after_takeover = post_claim(
        {"manager": "app", "deployment": dep, "replicas": 6}
    )
    check(
        "later conflict is judged against latest exclusive owner",
        status == 409
        and after_takeover.get("conflicts", [{}])[0].get("managers")
        == ["platform"],
        (status, after_takeover),
    )


def verify_concurrent_same_value_claims() -> None:
    dep = f"same-race-{uuid.uuid4().hex[:8]}"
    print(f"\n== concurrent same-value co-management: {dep} ==")

    status, created = post_claim(
        {"manager": "app", "deployment": dep, "images": {"web": "same:v1"}}
    )
    check("same-value fixture created", status == 200, (status, created))

    manager_count = 8
    barrier = threading.Barrier(manager_count)

    def submit(index: int) -> tuple[int, dict[str, Any]]:
        barrier.wait(timeout=10)
        return post_claim(
            {
                "manager": f"m{index:02d}",
                "deployment": dep,
                "images": {"web": "same:v1"},
            }
        )

    with ThreadPoolExecutor(max_workers=manager_count) as pool:
        results = list(pool.map(submit, range(manager_count)))

    statuses = [status for status, _ in results]
    check(
        "all concurrent same-value claims succeed",
        all(status == 200 for status in statuses),
        results,
    )

    status, state = get_state(dep)
    owners = state.get("ownership", {}).get("images", {}).get("web", [])
    check(
        "no concurrent owner is lost",
        status == 200
        and len(owners) == manager_count + 1
        and set(owners) == {"app", *(f"m{i:02d}" for i in range(manager_count))},
        state,
    )
    check(
        "same value remains stable",
        state.get("config", {}).get("images", {}).get("web") == "same:v1",
        state,
    )


def verify_concurrent_takeover_conflict() -> None:
    dep = f"takeover-race-{uuid.uuid4().hex[:8]}"
    print(f"\n== concurrent takeover versus conflicting change: {dep} ==")

    status, created = post_claim(
        {"manager": "app", "deployment": dep, "images": {"web": "race:v1"}}
    )
    check("race fixture created", status == 200, (status, created))

    status, co_owned = post_claim(
        {
            "manager": "platform",
            "deployment": dep,
            "images": {"web": "race:v1"},
        }
    )
    check("race fixture co-owned", status == 200, (status, co_owned))

    barrier = threading.Barrier(2)

    def submit(which: int) -> tuple[int, dict[str, Any]]:
        barrier.wait(timeout=10)
        if which == 0:
            return post_claim(
                {
                    "manager": "app",
                    "deployment": dep,
                    "images": {"web": "race:v2"},
                    "takeover": True,
                }
            )
        return post_claim(
            {
                "manager": "platform",
                "deployment": dep,
                "images": {"web": "race:v3"},
            }
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [0, 1]))

    statuses = sorted(status for status, _ in results)
    success = next((body for status, body in results if status == 200), None)
    rejected = next((body for status, body in results if status == 409), None)

    check(
        "exactly one concurrent claim succeeds and one conflicts",
        statuses == [200, 409],
        results,
    )
    check(
        "successful claim is the explicit takeover",
        success is not None
        and success.get("takeover") is True
        and success.get("config", {}).get("images", {}).get("web")
        == "race:v2"
        and success.get("ownership", {}).get("images", {}).get("web")
        == ["app"],
        success,
    )
    conflict_managers = (
        rejected.get("conflicts", [{}])[0].get("managers", [])
        if rejected
        else []
    )
    check(
        "rejected concurrent claim reports image conflict and latest owner",
        rejected is not None
        and rejected.get("conflicts", [{}])[0].get("field") == "images.web"
        and "app" in conflict_managers,
        rejected,
    )

    status, state = get_state(dep)
    check(
        "final persisted state is takeover value with exclusive owner",
        status == 200
        and state.get("config", {}).get("images", {}).get("web") == "race:v2"
        and state.get("ownership", {}).get("images", {}).get("web") == ["app"],
        state,
    )


def main() -> int:
    wait_for_health()
    verify_co_management_and_exit()
    verify_conflict_and_takeover()
    verify_concurrent_same_value_claims()
    verify_concurrent_takeover_conflict()

    print("\n" + "=" * 72)
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
