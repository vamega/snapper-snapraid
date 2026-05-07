import sys
import unittest

sys.argv = [sys.argv[0]]

from snapper_snapraid.snapper import parse_diff_output


class ParseDiffOutputTest(unittest.TestCase):
    def test_parses_diff_with_relocated_count(self):
        self.assertEqual(
            parse_diff_output(
                """
  499927 equal
     216 added
      46 removed
       0 updated
       0 moved
       0 copied
       3 relocated
       0 restored
There are differences!
"""
            ),
            {
                "equal": 499927,
                "added": 216,
                "removed": 46,
                "updated": 0,
                "moved": 0,
                "copied": 0,
                "relocated": 3,
                "restored": 0,
            },
        )

    def test_parses_legacy_diff_without_relocated_count(self):
        self.assertEqual(
            parse_diff_output(
                """
  499927 equal
     216 added
      46 removed
       0 updated
       0 moved
       0 copied
       0 restored
There are differences!
"""
            ),
            {
                "equal": 499927,
                "added": 216,
                "removed": 46,
                "updated": 0,
                "moved": 0,
                "copied": 0,
                "relocated": 0,
                "restored": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
