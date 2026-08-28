import unittest

from deploy.validate_readiness import validate_readiness


class ReleaseReadinessValidationTest(unittest.TestCase):
    def test_accepts_ready_and_degraded_ready(self):
        self.assertEqual(
            validate_readiness({"status": "not_ready", "reasons": ["taptap: failed"]}, {"status": "ready"}),
            "readyz passed",
        )
        self.assertEqual(
            validate_readiness({"status": "ready"}, {"status": "ready_degraded", "reasons": []}),
            "readyz passed",
        )

    def test_accepts_only_same_or_smaller_preexisting_failure_set(self):
        message = validate_readiness(
            {"status": "not_ready", "reasons": ["taptap: failed", "oppo-ui: stale"]},
            {"status": "not_ready", "reasons": ["taptap: failed"]},
        )
        self.assertIn("taptap: failed", message)

    def test_rejects_new_failure(self):
        with self.assertRaisesRegex(RuntimeError, "regressed"):
            validate_readiness(
                {"status": "not_ready", "reasons": ["taptap: failed"]},
                {"status": "not_ready", "reasons": ["taptap: failed", "oppo-ui: failed"]},
            )

    def test_rejects_failure_when_preflight_was_unavailable(self):
        with self.assertRaisesRegex(RuntimeError, "regressed"):
            validate_readiness(
                {"status": "unavailable", "reasons": ["preflight unavailable"]},
                {"status": "not_ready", "reasons": ["taptap: failed"]},
            )


if __name__ == "__main__":
    unittest.main()
