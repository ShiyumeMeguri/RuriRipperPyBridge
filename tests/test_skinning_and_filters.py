"""Bind-pose baking, and the cabmap browser's rule engine."""

from __future__ import annotations

import unittest

import numpy as np

from ..session import cabmap_state
from ..unity import skinning


class _Decoded:
    """The subset of mesh_decoder.DecodedMesh that bake_bind_pose touches."""

    def __init__(self, positions, bind_poses, bone_indices, bone_weights,
                 normals=None, tangents=None):
        self.positions = positions
        self.bind_poses = bind_poses
        self.bone_indices = bone_indices
        self.bone_weights = bone_weights
        self.normals = normals
        self.tangents = tangents


def _translation(x, y, z):
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, 3] = (x, y, z)
    return matrix


class TestBakeBindPose(unittest.TestCase):
    def test_rigid_single_bone(self):
        """One bone translated by (10, 0, 0): every vertex moves with it, since
        boneWorld @ bindpose is exactly that translation."""
        decoded = _Decoded(
            positions=np.array([[1.0, 2.0, 3.0], [0.0, 0.0, 0.0]], dtype=np.float32),
            bind_poses=np.tile(np.eye(4, dtype=np.float32), (1, 1, 1)),
            bone_indices=np.zeros((2, 1), dtype=np.int32),
            bone_weights=np.ones((2, 1), dtype=np.float32))
        self.assertTrue(skinning.bake_bind_pose(
            decoded, [{"fileID": 400}], {400: _translation(10.0, 0.0, 0.0)}))
        np.testing.assert_allclose(decoded.positions,
                                   [[11.0, 2.0, 3.0], [10.0, 0.0, 0.0]], atol=1e-6)

    def test_weights_blend_between_bones(self):
        decoded = _Decoded(
            positions=np.zeros((1, 3), dtype=np.float32),
            bind_poses=np.tile(np.eye(4, dtype=np.float32), (2, 1, 1)),
            bone_indices=np.array([[0, 1]], dtype=np.int32),
            bone_weights=np.array([[0.25, 0.75]], dtype=np.float32))
        skinning.bake_bind_pose(
            decoded, [{"fileID": 1}, {"fileID": 2}],
            {1: _translation(0.0, 0.0, 0.0), 2: _translation(4.0, 0.0, 0.0)})
        np.testing.assert_allclose(decoded.positions, [[3.0, 0.0, 0.0]], atol=1e-6)

    def test_normals_use_the_inverse_transpose(self):
        """A bone with non-uniform scale: the normal of a plane must stay
        perpendicular to it, which only the inverse transpose guarantees. A
        surface tangent along +X under scale (2, 1, 4) keeps its direction; the
        +Z normal must too, and would NOT under the plain linear part."""
        scale = np.diag([2.0, 1.0, 4.0, 1.0])
        decoded = _Decoded(
            positions=np.array([[1.0, 0.0, 0.0]], dtype=np.float32),
            bind_poses=np.tile(np.eye(4, dtype=np.float32), (1, 1, 1)),
            bone_indices=np.zeros((1, 1), dtype=np.int32),
            bone_weights=np.ones((1, 1), dtype=np.float32),
            normals=np.array([[0.0, 0.0, 1.0]], dtype=np.float32))
        skinning.bake_bind_pose(decoded, [{"fileID": 1}], {1: scale})
        np.testing.assert_allclose(decoded.positions, [[2.0, 0.0, 0.0]], atol=1e-6)
        np.testing.assert_allclose(decoded.normals, [[0.0, 0.0, 1.0]], atol=1e-6)

    def test_tangent_handedness_survives(self):
        scale = np.diag([2.0, 1.0, 1.0, 1.0])
        decoded = _Decoded(
            positions=np.zeros((1, 3), dtype=np.float32),
            bind_poses=np.tile(np.eye(4, dtype=np.float32), (1, 1, 1)),
            bone_indices=np.zeros((1, 1), dtype=np.int32),
            bone_weights=np.ones((1, 1), dtype=np.float32),
            tangents=np.array([[1.0, 0.0, 0.0, -1.0]], dtype=np.float32))
        skinning.bake_bind_pose(decoded, [{"fileID": 1}], {1: scale})
        self.assertEqual(decoded.tangents.shape, (1, 4))
        self.assertEqual(float(decoded.tangents[0, 3]), -1.0)
        np.testing.assert_allclose(decoded.tangents[0, :3], [1.0, 0.0, 0.0], atol=1e-6)

    def test_unskinned_mesh_reports_false(self):
        decoded = _Decoded(positions=np.zeros((1, 3), dtype=np.float32),
                           bind_poses=None, bone_indices=None, bone_weights=None)
        self.assertFalse(skinning.bake_bind_pose(decoded, [], {}))

    def test_missing_bone_world_falls_back_to_identity(self):
        decoded = _Decoded(
            positions=np.array([[1.0, 1.0, 1.0]], dtype=np.float32),
            bind_poses=np.tile(np.eye(4, dtype=np.float32), (1, 1, 1)),
            bone_indices=np.zeros((1, 1), dtype=np.int32),
            bone_weights=np.ones((1, 1), dtype=np.float32))
        self.assertTrue(skinning.bake_bind_pose(decoded, [{"fileID": 999}], {}))
        np.testing.assert_allclose(decoded.positions, [[1.0, 1.0, 1.0]], atol=1e-6)


def _row(**fields):
    base = {"name": "", "container": "", "type_names": "", "source": "", "deps": 0}
    base.update(fields)
    return base


class TestRuleEngine(unittest.TestCase):
    """Every ENABLED rule is a required constraint (WinForms Filter.cs parity)."""

    def test_no_rules_shows_everything(self):
        self.assertTrue(cabmap_state.row_passes_rules(_row(name="anything"), []))

    def test_include_requires_a_match(self):
        rules = [cabmap_state.Rule("name", "contains", "chr", "include")]
        self.assertTrue(cabmap_state.row_passes_rules(_row(name="chr_0013"), rules))
        self.assertFalse(cabmap_state.row_passes_rules(_row(name="env_rock"), rules))

    def test_exclude_requires_a_non_match(self):
        rules = [cabmap_state.Rule("name", "contains", "lod", "exclude")]
        self.assertTrue(cabmap_state.row_passes_rules(_row(name="wall"), rules))
        self.assertFalse(cabmap_state.row_passes_rules(_row(name="wall_lod2"), rules))

    def test_rules_are_conjunctive(self):
        rules = [cabmap_state.Rule("name", "contains", "chr", "include"),
                 cabmap_state.Rule("name", "ends_with", "_lod2", "exclude")]
        self.assertTrue(cabmap_state.row_passes_rules(_row(name="chr_body"), rules))
        self.assertFalse(cabmap_state.row_passes_rules(_row(name="chr_body_lod2"), rules))

    def test_disabled_rules_are_inert(self):
        rules = [cabmap_state.Rule("name", "is", "nothing", "include", enabled=False)]
        self.assertTrue(cabmap_state.row_passes_rules(_row(name="anything"), rules))

    def test_numeric_relations(self):
        rules = [cabmap_state.Rule("deps", "more_than", "3", "include")]
        self.assertTrue(cabmap_state.row_passes_rules(_row(deps=4), rules))
        self.assertFalse(cabmap_state.row_passes_rules(_row(deps=3), rules))

    def test_non_numeric_value_fails_a_numeric_relation(self):
        rules = [cabmap_state.Rule("deps", "less_than", "abc", "include")]
        self.assertFalse(cabmap_state.row_passes_rules(_row(deps=1), rules))

    def test_matching_is_case_insensitive(self):
        rules = [cabmap_state.Rule("name", "is", "CHR_Body", "include")]
        self.assertTrue(cabmap_state.row_passes_rules(_row(name="chr_body"), rules))

    def test_regex_relations(self):
        rules = [cabmap_state.Rule("name", "matches_regex", r"_lod\d$", "include")]
        self.assertTrue(cabmap_state.row_passes_rules(_row(name="wall_lod2"), rules))
        self.assertFalse(cabmap_state.row_passes_rules(_row(name="wall"), rules))

    def test_invalid_regex_never_raises(self):
        rules = [cabmap_state.Rule("name", "matches_regex", "([", "include")]
        self.assertFalse(cabmap_state.row_passes_rules(_row(name="wall"), rules))

    def test_has_active_query(self):
        self.assertTrue(cabmap_state.has_active_query("chr", ()))
        self.assertFalse(cabmap_state.has_active_query("   ", ()))
        rule = cabmap_state.Rule("name", "contains", "x", "include", enabled=False)
        self.assertFalse(cabmap_state.has_active_query("", [rule]))
        rule.enabled = True
        self.assertTrue(cabmap_state.has_active_query("", [rule]))

    def test_duck_typed_rules_work(self):
        """A host passes its own UI-backed objects straight in."""
        class HostRule:
            field, relation, value, action, enabled = "name", "contains", "chr", "include", True

        self.assertTrue(cabmap_state.row_passes_rules(_row(name="chr_x"), [HostRule()]))


if __name__ == "__main__":
    unittest.main()
