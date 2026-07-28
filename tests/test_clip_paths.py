"""Re-anchoring animation curve bindings onto a skeleton."""

from __future__ import annotations

import unittest
import zlib

from ..unity import clip_paths

PATH_TO_BONE = {
    "chr_0013_postmodel/Root": "Root",
    "chr_0013_postmodel/Root/Bip001": "Bip001",
    "chr_0013_postmodel/Root/Bip001/Bip001 Spine": "Bip001 Spine",
}


def _crc(text):
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


class TestSuffixCrcTable(unittest.TestCase):
    def setUp(self):
        self.table = clip_paths.build_suffix_crc_table(PATH_TO_BONE)

    def test_every_suffix_resolves_to_its_full_path(self):
        self.assertEqual(self.table[_crc("Root/Bip001")],
                         "chr_0013_postmodel/Root/Bip001")
        self.assertEqual(self.table[_crc("Bip001")],
                         "chr_0013_postmodel/Root/Bip001")
        self.assertEqual(self.table[_crc("chr_0013_postmodel/Root")],
                         "chr_0013_postmodel/Root")

    def test_known_unity_hash(self):
        """Ground truth: crc32(b"Root") is what the game's own
        path_0xB6C65665_* placeholder carries."""
        self.assertEqual(_crc("Root"), 0xB6C65665)

    def test_longest_suffix_wins_a_collision(self):
        table = clip_paths.build_suffix_crc_table({"a/b": "b", "b": "b"})
        self.assertEqual(table[_crc("b")], "a/b")


class TestEntryCrc(unittest.TestCase):
    def test_hashed_placeholder_is_read_literally(self):
        self.assertEqual(clip_paths.entry_crc("path_0xB6C65665_WvpMuNH"), 0xB6C65665)

    def test_plain_path_is_hashed(self):
        self.assertEqual(clip_paths.entry_crc("Root/Bip001"), _crc("Root/Bip001"))


class TestRepair(unittest.TestCase):
    def test_hashed_paths_are_restored(self):
        clip = {"m_RotationCurves": [
            {"path": "path_0x{0:X}_junk".format(_crc("Root/Bip001"))},
            {"path": "path_0xDEADBEEF_junk"},
        ]}
        repaired, unmatched = clip_paths.repair_hashed_clip_paths(clip, PATH_TO_BONE)
        self.assertEqual((repaired, unmatched), (1, 1))
        self.assertEqual(clip["m_RotationCurves"][0]["path"],
                         "chr_0013_postmodel/Root/Bip001")

    def test_a_differently_nested_string_path_is_re_anchored(self):
        clip = {"m_PositionCurves": [{"path": "Root/Bip001"}]}
        repaired, unmatched = clip_paths.repair_hashed_clip_paths(clip, PATH_TO_BONE)
        self.assertEqual((repaired, unmatched), (1, 0))
        self.assertEqual(clip["m_PositionCurves"][0]["path"],
                         "chr_0013_postmodel/Root/Bip001")

    def test_already_matching_paths_are_untouched(self):
        clip = {"m_ScaleCurves": [{"path": "chr_0013_postmodel/Root"}]}
        repaired, unmatched = clip_paths.repair_hashed_clip_paths(clip, PATH_TO_BONE)
        self.assertEqual((repaired, unmatched), (0, 0))

    def test_null_valued_path_is_skipped(self):
        """`path:` with no value parses to an existing key holding None -- the
        root-level curve case, which must not be treated as unmatched."""
        clip = {"m_FloatCurves": [{"path": None}]}
        self.assertEqual(clip_paths.repair_hashed_clip_paths(clip, PATH_TO_BONE), (0, 0))


class TestMatchRatio(unittest.TestCase):
    def test_ratio_counts_transform_curves_only(self):
        clip = {
            "m_RotationCurves": [{"path": "Root/Bip001"}, {"path": "Nope/Missing"}],
            "m_FloatCurves": [{"path": "whatever", "attribute": "Spine Front-Back"}],
        }
        ratio, total = clip_paths.clip_path_match_ratio(clip, PATH_TO_BONE)
        self.assertEqual(total, 2)
        self.assertAlmostEqual(ratio, 0.5)

    def test_no_transform_curves(self):
        self.assertEqual(clip_paths.clip_path_match_ratio({}, PATH_TO_BONE), (0.0, 0))


class TestHumanoidDetection(unittest.TestCase):
    """Detection keys on the root channels, not the muscle taxonomy: every humanoid clip carries
    RootT/RootQ, and the ~95 muscle names live with the solver that consumes them (C#), not here.
    Reaching this predicate at all means the C# humanoid pass did not run."""

    def test_root_motion_curve_counts(self):
        clip = {"m_FloatCurves": [{"attribute": "RootT.x"}]}
        self.assertTrue(clip_paths.clip_is_humanoid(clip))

    def test_root_orientation_curve_counts(self):
        clip = {"m_FloatCurves": [{"attribute": "RootQ.w"}]}
        self.assertTrue(clip_paths.clip_is_humanoid(clip))

    def test_generic_clip_is_not_humanoid(self):
        clip = {"m_FloatCurves": [{"attribute": "blendShape.smile"}],
                "m_RotationCurves": [{"path": "Root"}]}
        self.assertFalse(clip_paths.clip_is_humanoid(clip))


if __name__ == "__main__":
    unittest.main()
