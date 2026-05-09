import sys
import unittest

sys.argv = [sys.argv[0]]

from snapper_snapraid.snapper import (
    parse_diff_output,
    parse_smart_tags,
    parse_snapraid_tag_line,
    parse_snapraid_tags,
    parse_status_tags,
    should_rerun_sync_from_tags,
    smart_tags_have_failing_disk,
)


class ParseDiffOutputTest(unittest.TestCase):
    def test_parses_structured_diff_summary(self):
        self.assertEqual(
            parse_diff_output(
                """
summary:equal:499927
summary:added:216
summary:removed:46
summary:updated:0
summary:moved:0
summary:copied:0
summary:relocated:3
summary:restored:0
summary:exit:diff
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

    def test_rejects_human_diff_output(self):
        with self.assertRaises(ValueError):
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
            )


class ParseStructuredTagTest(unittest.TestCase):
    def test_parses_escaped_structured_tag_values(self):
        tag = parse_snapraid_tag_line(r"scan:add:data:dir\dname\\file\nname")

        self.assertEqual(tag.name, "scan")
        self.assertEqual(tag.values, ("add", "data", "dir:name\\file\nname"))


class ParseStatusTagsTest(unittest.TestCase):
    def test_parses_structured_status(self):
        drive_stats, scrub_stats, error_count, zero_subsecond_count, sync_in_progress = (
            parse_status_tags(
                parse_snapraid_tags(
                    """
summary:disk_file_count:d1:10
summary:disk_fragmented_file_count:d1:2
summary:disk_excess_fragment_count:d1:3
summary:disk_space_wasted:d1:1500000000
summary:disk_used:d1:2500000000
summary:disk_free:d1:3500000000
summary:disk_use_percent:d1:42
summary:file_count:10
summary:fragmented_file_count:2
summary:excess_fragment_count:3
summary:zerosubsecond_file_count:4
summary:total_wasted:1500000000
summary:total_used:2500000000
summary:total_free:3500000000
summary:total_use_percent:42
summary:scrub_oldest_days:30
summary:scrub_median_days:20
summary:scrub_newest_days:10
summary:exit:unsynced
content_info:block:100
content_info:block_bad:2
content_info:block_unsynced:5
content_info:block_unscrubbed:1
"""
                )
            )
        )

        self.assertEqual(drive_stats[0]["drive_name"], "d1")
        self.assertEqual(drive_stats[0]["wasted_gb"], "1.5")
        self.assertEqual(scrub_stats["unscrubbed"], 1)
        self.assertEqual(scrub_stats["scrub_age"], 30)
        self.assertEqual(error_count, 2)
        self.assertEqual(zero_subsecond_count, 4)
        self.assertTrue(sync_in_progress)


class ParseSmartTagsTest(unittest.TestCase):
    def test_parses_structured_smart(self):
        drive_data, global_fp = parse_smart_tags(
            parse_snapraid_tags(
                r"""
info:/dev/sda:data1
attr:/dev/sda:data1:serial:ABC\d123
attr:/dev/sda:data1:size:1000000000000
attr:/dev/sda:data1:temperature:34
attr:/dev/sda:data1:rotationrate:7200
attr:/dev/sda:data1:9:2400:960:100:100:0:Power_On_Hours:oldage:always:never
attr:/dev/sda:data1:error_protocol:2
attr:/dev/sda:data1:error_medium:3
attr:/dev/sda:data1:afr:0.02:0.0198013
summary:array_failure:0.02:0.0198013
"""
            )
        )

        self.assertEqual(global_fp, "2")
        self.assertEqual(
            drive_data[0],
            {
                "temp": "34",
                "power_on_days": "100",
                "error_count": "5",
                "fp": "2%",
                "size": "1.0",
                "serial": "ABC:123",
                "device": "/dev/sda",
                "disk": "data1",
            },
        )

    def test_detects_failing_smart_flags(self):
        self.assertTrue(
            smart_tags_have_failing_disk(
                parse_snapraid_tags("attr:/dev/sda:data1:flags:8:8")
            )
        )

    def test_detects_failing_smart_attribute(self):
        self.assertTrue(
            smart_tags_have_failing_disk(
                parse_snapraid_tags(
                    "attr:/dev/sda:data1:5:1:1:1:1:1:Reallocated_Sector_Ct:prefail:always:now"
                )
            )
        )


class SyncTagsTest(unittest.TestCase):
    def test_reruns_sync_for_soft_errors_only(self):
        self.assertTrue(
            should_rerun_sync_from_tags(
                parse_snapraid_tags(
                    """
summary:error_soft:2
summary:error_io:0
summary:error_data:0
summary:exit:warning
"""
                )
            )
        )

    def test_does_not_rerun_sync_for_io_errors(self):
        self.assertFalse(
            should_rerun_sync_from_tags(
                parse_snapraid_tags(
                    """
summary:error_soft:2
summary:error_io:1
summary:error_data:0
summary:exit:error
"""
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
