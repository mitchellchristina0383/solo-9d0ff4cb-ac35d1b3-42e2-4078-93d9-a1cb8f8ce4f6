"""Pure ownership-rule unit tests.

The HTTP-level behavior and SQLite concurrency are covered by
``scripts/verify_flow.py``; these tests document the merge rules directly.
"""

from __future__ import annotations

import unittest

from app.ownership import OwnershipConflictError, merge_claim


class OwnershipRulesTest(unittest.TestCase):
    def test_same_value_creates_co_management(self) -> None:
        first = merge_claim(
            {},
            {},
            "app",
            {"replicas": 3, "images.web": "v1"},
        )
        second = merge_claim(
            first.values,
            first.owners,
            "platform",
            {"replicas": 3, "images.web": "v1"},
        )

        self.assertEqual(second.values["replicas"], 3)
        self.assertEqual(second.owners["replicas"], ["app", "platform"])
        self.assertEqual(
            second.owners["images.web"], ["app", "platform"]
        )
        self.assertEqual(
            second.changes["co_managed"], ["images.web", "replicas"]
        )

    def test_one_owner_exit_keeps_field_last_owner_deletes(self) -> None:
        current = {"replicas": 3, "images.web": "v1"}
        owners = {
            "replicas": ["app", "platform"],
            "images.web": ["app", "platform"],
        }

        platform_exits = merge_claim(
            current,
            owners,
            "platform",
            {"images.web": "v1"},
        )
        self.assertEqual(platform_exits.changes["released"], ["replicas"])
        self.assertEqual(platform_exits.values["replicas"], 3)
        self.assertEqual(platform_exits.owners["replicas"], ["app"])
        self.assertEqual(
            platform_exits.owners["images.web"], ["app", "platform"]
        )

        app_exits_replicas = merge_claim(
            platform_exits.values,
            platform_exits.owners,
            "app",
            {"images.web": "v1"},
        )
        self.assertEqual(app_exits_replicas.changes["deleted"], ["replicas"])
        self.assertNotIn("replicas", app_exits_replicas.values)

        platform_exits_image = merge_claim(
            app_exits_replicas.values,
            app_exits_replicas.owners,
            "platform",
            {},
        )
        self.assertEqual(
            platform_exits_image.changes["released"], ["images.web"]
        )
        self.assertEqual(
            platform_exits_image.owners["images.web"], ["app"]
        )

        app_exits_image = merge_claim(
            platform_exits_image.values,
            platform_exits_image.owners,
            "app",
            {},
        )
        self.assertEqual(
            app_exits_image.changes["deleted"], ["images.web"]
        )
        self.assertEqual(app_exits_image.values, {})
        self.assertEqual(app_exits_image.owners, {})

    def test_other_owner_change_conflicts_unless_takeover(self) -> None:
        current = {"replicas": 3}
        owners = {"replicas": ["app"]}

        with self.assertRaises(OwnershipConflictError) as context:
            merge_claim(current, owners, "platform", {"replicas": 4})

        conflict = context.exception.conflicts[0]
        self.assertEqual(conflict["field"], "replicas")
        self.assertEqual(conflict["current"], 3)
        self.assertEqual(conflict["submitted"], 4)
        self.assertEqual(conflict["managers"], ["app"])

        taken = merge_claim(
            current,
            owners,
            "platform",
            {"replicas": 4},
            takeover=True,
        )
        self.assertEqual(taken.values["replicas"], 4)
        self.assertEqual(taken.owners["replicas"], ["platform"])
        self.assertEqual(taken.changes["taken_over"], ["replicas"])

    def test_co_owner_change_requires_takeover(self) -> None:
        current = {"images.web": "v1"}
        owners = {"images.web": ["app", "platform"]}

        with self.assertRaises(OwnershipConflictError) as context:
            merge_claim(current, owners, "app", {"images.web": "v2"})
        self.assertEqual(
            context.exception.conflicts[0]["other_managers"], ["platform"]
        )

        taken = merge_claim(
            current,
            owners,
            "app",
            {"images.web": "v2"},
            takeover=True,
        )
        self.assertEqual(taken.owners["images.web"], ["app"])


if __name__ == "__main__":
    unittest.main()
