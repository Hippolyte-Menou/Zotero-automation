import unittest

import audit_bot


class TestModuleImports(unittest.TestCase):
    def test_reason_set(self):
        self.assertEqual(
            audit_bot.REASON_RESCUE_ELIGIBLE,
            {"score_below_threshold", "mention_filter"},
        )


if __name__ == "__main__":
    unittest.main()
