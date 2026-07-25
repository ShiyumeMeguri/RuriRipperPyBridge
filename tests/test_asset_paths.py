"""Addressable-path rules and LOD-sibling selection."""

from __future__ import annotations

import unittest

from ..unity import asset_paths


class TestExpectedMeshName(unittest.TestCase):
    def test_sub_object_suffix(self):
        self.assertEqual(
            asset_paths.expected_mesh_name("assets/models/s_building.fbx##building_COL1_UM01"),
            "building_col1_um01")

    def test_bare_mesh_file(self):
        self.assertEqual(asset_paths.expected_mesh_name("assets/meshes/Rock_02.mesh"), "rock_02")

    def test_no_extension(self):
        self.assertEqual(asset_paths.expected_mesh_name("assets/meshes/Rock"), "rock")


class TestLodRank(unittest.TestCase):
    def test_numbered_levels(self):
        self.assertEqual(asset_paths.lod_rank("a/b.fbx##piece_lod0"), 0)
        self.assertEqual(asset_paths.lod_rank("a/b.fbx##piece_lod2"), 2)

    def test_unsuffixed_is_as_good_as_lod0(self):
        self.assertEqual(asset_paths.lod_rank("a/b.fbx##piece"), -1)

    def test_collision_is_last(self):
        self.assertEqual(asset_paths.lod_rank("a/b.fbx##piece_col1_um01"), 1000)


class TestSelectBestLod(unittest.TestCase):
    @staticmethod
    def _row(path, x=0.0, y=0.0, z=0.0):
        return {"asset_path": path, "px": x, "py": y, "pz": z}

    def test_lod0_wins_over_its_siblings(self):
        rows = [self._row("a/b.fbx##wall_lod2"),
                self._row("a/b.fbx##wall_lod0"),
                self._row("a/b.fbx##wall_col1_um01")]
        kept = asset_paths.select_best_lod(rows)
        self.assertEqual([r["asset_path"] for r in kept], ["a/b.fbx##wall_lod0"])

    def test_an_instance_with_no_lod0_is_not_dropped(self):
        """The regression this function exists for: a piece whose only shipped
        variants are _lod2/_col1 is real, visible geometry."""
        rows = [self._row("a/b.fbx##shell_lod2"), self._row("a/b.fbx##shell_col1_um01")]
        kept = asset_paths.select_best_lod(rows)
        self.assertEqual([r["asset_path"] for r in kept], ["a/b.fbx##shell_lod2"])

    def test_different_positions_are_different_instances(self):
        rows = [self._row("a/b.fbx##wall_lod0", 0.0, 0.0, 0.0),
                self._row("a/b.fbx##wall_lod0", 10.0, 0.0, 0.0)]
        self.assertEqual(len(asset_paths.select_best_lod(rows)), 2)

    def test_float_noise_between_siblings_collapses(self):
        rows = [self._row("a/b.fbx##wall_lod0", 1.0001, 0.0, 0.0),
                self._row("a/b.fbx##wall_lod1", 1.0002, 0.0, 0.0)]
        kept = asset_paths.select_best_lod(rows)
        self.assertEqual([r["asset_path"] for r in kept], ["a/b.fbx##wall_lod0"])


class TestPrefabPaths(unittest.TestCase):
    def test_is_full_prefab_path(self):
        self.assertTrue(asset_paths.is_full_prefab_path("a/Prefabs/P_thing_01.prefab"))
        self.assertFalse(asset_paths.is_full_prefab_path("a/models/s_x.fbx##sub"))

    def test_prefab_asset_stem(self):
        self.assertEqual(
            asset_paths.prefab_asset_stem("a/Prefabs/P_anm_com_satellite+1_001_01.prefab"),
            "p_anm_com_satellite+1_001_01")


if __name__ == "__main__":
    unittest.main()
