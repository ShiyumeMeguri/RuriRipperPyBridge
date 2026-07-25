"""Unity's humanoid muscle taxonomy, as data.

Unity's Mecanim system describes a humanoid pose as ~95 normalised "muscle"
floats plus a root-motion frame, instead of per-bone transforms. Which
attribute name drives which human bone on which axis, the BoneType enum order
that indexes an Avatar's own arrays, the fixed twist-solve pairs and the
mass-centre formula are all Unity's own definitions -- domain data, not
technique, and not tied to any host.

They live here rather than inside a retargeter for two reasons: a host's
retargeter needs a matrix library and so cannot be shared, while
``is_muscle``/``is_root`` are needed by anything that merely has to RECOGNISE a
humanoid clip (see unity.clip_paths.clip_is_humanoid) and must not drag one in.

Ground truth for the tables below is Unity's own native implementation
(mecanim::human), read directly rather than curve-fitted; the comments say
which function each came from.
"""

from __future__ import annotations


# --- muscle -> (human bone, degree-of-freedom axis) -------------------------
#
# dof axis: 0 = X (twist), 1 = Y, 2 = Z.  The "Twist"/"Turn" muscle drives X;
# the primary bend drives Y; the secondary bend drives Z.  Bones without a twist
# (shoulder/hand) leave X unused (the avatar locks that axis' limit to 0).  The
# exact Y/Z assignment and signs are validated against the clip's IK markers.
MUSCLE_DOF = {
    "Spine Front-Back": ("Spine", 2),
    "Spine Left-Right": ("Spine", 1),
    "Spine Twist Left-Right": ("Spine", 0),
    "Chest Front-Back": ("Chest", 2),
    "Chest Left-Right": ("Chest", 1),
    "Chest Twist Left-Right": ("Chest", 0),
    "UpperChest Front-Back": ("UpperChest", 2),
    "UpperChest Left-Right": ("UpperChest", 1),
    "UpperChest Twist Left-Right": ("UpperChest", 0),
    "Neck Nod Down-Up": ("Neck", 2),
    "Neck Tilt Left-Right": ("Neck", 1),
    "Neck Turn Left-Right": ("Neck", 0),
    "Head Nod Down-Up": ("Head", 2),
    "Head Tilt Left-Right": ("Head", 1),
    "Head Turn Left-Right": ("Head", 0),
    "Left Upper Leg Front-Back": ("LeftUpperLeg", 2),
    "Left Upper Leg In-Out": ("LeftUpperLeg", 1),
    "Left Upper Leg Twist In-Out": ("LeftUpperLeg", 0),
    "Left Lower Leg Stretch": ("LeftLowerLeg", 2),
    "Left Lower Leg Twist In-Out": ("LeftLowerLeg", 0),
    "Left Foot Up-Down": ("LeftFoot", 2),
    "Left Foot Twist In-Out": ("LeftFoot", 1),
    "Left Toes Up-Down": ("LeftToes", 1),
    "Right Upper Leg Front-Back": ("RightUpperLeg", 2),
    "Right Upper Leg In-Out": ("RightUpperLeg", 1),
    "Right Upper Leg Twist In-Out": ("RightUpperLeg", 0),
    "Right Lower Leg Stretch": ("RightLowerLeg", 2),
    "Right Lower Leg Twist In-Out": ("RightLowerLeg", 0),
    "Right Foot Up-Down": ("RightFoot", 2),
    "Right Foot Twist In-Out": ("RightFoot", 1),
    "Right Toes Up-Down": ("RightToes", 1),
    "Left Shoulder Down-Up": ("LeftShoulder", 2),
    "Left Shoulder Front-Back": ("LeftShoulder", 1),
    "Left Arm Down-Up": ("LeftUpperArm", 2),
    "Left Arm Front-Back": ("LeftUpperArm", 1),
    "Left Arm Twist In-Out": ("LeftUpperArm", 0),
    "Left Forearm Stretch": ("LeftLowerArm", 2),
    "Left Forearm Twist In-Out": ("LeftLowerArm", 0),
    "Left Hand Down-Up": ("LeftHand", 2),
    "Left Hand In-Out": ("LeftHand", 1),
    "Right Shoulder Down-Up": ("RightShoulder", 2),
    "Right Shoulder Front-Back": ("RightShoulder", 1),
    "Right Arm Down-Up": ("RightUpperArm", 2),
    "Right Arm Front-Back": ("RightUpperArm", 1),
    "Right Arm Twist In-Out": ("RightUpperArm", 0),
    "Right Forearm Stretch": ("RightLowerArm", 2),
    "Right Forearm Twist In-Out": ("RightLowerArm", 0),
    "Right Hand Down-Up": ("RightHand", 2),
    "Right Hand In-Out": ("RightHand", 1),
    # Eyes/jaw follow the same per-bone axis pattern as the head group: the primary
    # (vertical) DoF drives Z, the secondary (horizontal) DoF drives Y, no twist.
    "Left Eye Down-Up": ("LeftEye", 2),
    "Left Eye In-Out": ("LeftEye", 1),
    "Right Eye Down-Up": ("RightEye", 2),
    "Right Eye In-Out": ("RightEye", 1),
    "Jaw Close": ("Jaw", 2),
    "Jaw Left-Right": ("Jaw", 1),
}

# Finger muscles ("LeftHand.Thumb.1 Stretched" ...): 2 hands x 5 fingers x 4 DoF = 40.
# 1/2/3 Stretched curl the proximal/intermediate/distal phalange about Z; Spread swings the
# proximal about Y (Unity muscle taxonomy, mirrored from the C# AvatarMuscleReferential).
# Keys into the same _axes table as the body via Unity's HumanDescription human names
# ("Left Thumb Proximal" ...).
FINGER_NAMES = ("Thumb", "Index", "Middle", "Ring", "Little")
PHALANGE_NAMES = ("Proximal", "Intermediate", "Distal")

def _build_finger_dof():
    for side, hand in (("Left", "LeftHand"), ("Right", "RightHand")):
        for finger in FINGER_NAMES:
            proximal = f"{side} {finger} Proximal"
            intermediate = f"{side} {finger} Intermediate"
            distal = f"{side} {finger} Distal"
            MUSCLE_DOF[f"{hand}.{finger}.1 Stretched"] = (proximal, 2)
            MUSCLE_DOF[f"{hand}.{finger}.Spread"] = (proximal, 1)
            MUSCLE_DOF[f"{hand}.{finger}.2 Stretched"] = (intermediate, 2)
            MUSCLE_DOF[f"{hand}.{finger}.3 Stretched"] = (distal, 2)

_build_finger_dof()

# BoneType enum order (Unity HumanBodyBones / MuscleHelper.BoneType); indexes the
# avatar's m_HumanBoneIndex array.
BONE_TYPE_NAMES = (
    "Hips", "LeftUpperLeg", "RightUpperLeg", "LeftLowerLeg", "RightLowerLeg",
    "LeftFoot", "RightFoot", "Spine", "Chest", "UpperChest", "Neck", "Head",
    "LeftShoulder", "RightShoulder", "LeftUpperArm", "RightUpperArm",
    "LeftLowerArm", "RightLowerArm", "LeftHand", "RightHand", "LeftToes",
    "RightToes", "LeftEye", "RightEye", "Jaw",
)


# The four limb segments (upper/lower arm, upper/lower leg) apply their own
# twist-DOF (axis 0) muscle at a fraction of the angle every other
# twist-driven bone (Spine/Chest/UpperChest/Neck/Head) uses at the same
# muscle value -- root-caused (not curve-fit) by reading Unity.dll directly
# (IDA Pro): ``mecanim::human::TwistSolve`` calls ``HumanFixTwist`` for 8
# fixed (parent, child, avatar field) pairs, in exactly this order, after
# every bone's own independent muscle solve:
#
#     (LeftLowerArm,  LeftHand,      m_ForeArmTwist)
#     (LeftUpperArm,  LeftLowerArm,  m_ArmTwist)
#     (RightLowerArm, RightHand,     m_ForeArmTwist)
#     (RightUpperArm, RightLowerArm, m_ArmTwist)
#     (LeftLowerLeg,  LeftFoot,      m_LegTwist)
#     (LeftUpperLeg,  LeftLowerLeg,  m_UpperLegTwist)
#     (RightLowerLeg, RightFoot,     m_LegTwist)
#     (RightUpperLeg, RightLowerLeg, m_UpperLegTwist)
#
# Each pair rescales the PARENT's own twist angle by the avatar-configurable
# factor (all four default to 0.5, confirmed for this avatar) and adjusts the
# CHILD's local rotation to keep the child's WORLD orientation unchanged (see
# ``HumanoidRetargeter.body_local_quats`` for the derivation and the exact
# order this is applied in -- order matters, since e.g. LeftLowerArm is a
# CHILD in pair 1 and a PARENT in pair 2, applied sequentially).  An isolated
# single-bone test (only that bone's own muscles active, reading that SAME
# bone back) can only ever observe the "rescale this bone's own twist" half
# of this -- never the child-compensation half, which needs the PARENT's
# twist muscle active and the CHILD's own resulting rotation read back.
TWIST_SOLVE_PAIRS = (
    ("LeftLowerArm", "LeftHand", "fore_arm_twist"),
    ("LeftUpperArm", "LeftLowerArm", "arm_twist"),
    ("RightLowerArm", "RightHand", "fore_arm_twist"),
    ("RightUpperArm", "RightLowerArm", "arm_twist"),
    ("LeftLowerLeg", "LeftFoot", "leg_twist"),
    ("LeftUpperLeg", "LeftLowerLeg", "upper_leg_twist"),
    ("RightLowerLeg", "RightFoot", "leg_twist"),
    ("RightUpperLeg", "RightLowerLeg", "upper_leg_twist"),
)

# --- root motion (Hips) reconstruction ---------------------------------------
#
# Humanoid clips do not store the Hips bone's own local transform: RootT/RootQ
# encode Unity's internal mecanim::human::Human "root reference" -- a
# mass-weighted center of mass across the 25 body bones (RootT) and an
# orientation frame built from the shoulder/hip bone positions (RootQ),
# relative to the same quantities computed once from the avatar's rest pose.
# Both were confirmed by decompiling Unity's own native
# HumanComputeBoneMassCenter/HumanComputeOrientation/HumanSetupAxes (IDA Pro
# on Unity.dll) and validated against live Unity ground truth (Animator +
# AnimationClip.SampleAnimation on a real walk-cycle clip): the
# orientation-frame formula matches Unity's own rest-pose computation to
# 0.00002 degrees, and the resulting Hips rotation and (Y, X) position track
# live Unity output to a few degrees / centimeters across a full multi-frame
# gait cycle.
#
# To recover Hips' own local transform, a PROVISIONAL pose is built with Hips
# fixed at the origin with identity rotation (every other body bone at its
# frame's absolute local rotation, via _axes_local). Placing Hips at an
# actual (T, R) instead rigidly transforms every descendant, so
# mass_center(actual) = T + R @ mass_center(provisional) and
# orientation(actual) = R @ orientation(provisional); solving both against
# the frame's RootT/RootQ (and the avatar's own rest reference m_RootX) gives
# Hips' true rotation and position directly.
#
# KNOWN LIMITATION: clips authored without a separate MotionT/MotionQ curve
# (ground-projected root motion) still bake full world-space walking
# progress into RootT itself; since that motion belongs on the character's
# root GameObject rather than on Hips, this shows up as drift along the walk
# direction when no MotionT curve exists to subtract it back out first. This
# is the same class of issue the MotionT subtraction below already exists
# for -- it is a pre-existing limitation for MotionT-less clips, not a
# regression introduced by this formula.
MASS_CENTER_FORMULA = {
    "Hips": (("LeftUpperLeg", 1.0 / 3.0), ("RightUpperLeg", 1.0 / 3.0), ("Spine", 1.0 / 3.0)),
    "LeftUpperLeg": (("LeftUpperLeg", 0.5), ("LeftLowerLeg", 0.5)),
    "RightUpperLeg": (("RightUpperLeg", 0.5), ("RightLowerLeg", 0.5)),
    "LeftLowerLeg": (("LeftLowerLeg", 0.5), ("LeftFoot", 0.5)),
    "RightLowerLeg": (("RightLowerLeg", 0.5), ("RightFoot", 0.5)),
    "Spine": (("Spine", 0.5), ("Chest", 0.5)),
    "Chest": (("Chest", 0.5), ("UpperChest", 0.5)),
    "UpperChest": (("UpperChest", 0.25), ("Neck", 0.25), ("LeftShoulder", 0.25), ("RightShoulder", 0.25)),
    "Neck": (("Neck", 0.5), ("Head", 0.5)),
    "LeftShoulder": (("LeftShoulder", 0.5), ("LeftUpperArm", 0.5)),
    "RightShoulder": (("RightShoulder", 0.5), ("RightUpperArm", 0.5)),
    "LeftUpperArm": (("LeftUpperArm", 0.5), ("LeftLowerArm", 0.5)),
    "RightUpperArm": (("RightUpperArm", 0.5), ("RightLowerArm", 0.5)),
    "LeftLowerArm": (("LeftLowerArm", 0.5), ("LeftHand", 0.5)),
    "RightLowerArm": (("RightLowerArm", 0.5), ("RightHand", 0.5)),
}
# Any body bone not listed above (feet, hands, toes, eyes, head, jaw) uses its
# own provisional position directly, matching HumanComputeBoneMassCenter's
# default case.


def is_muscle(attribute):
    """True if a float-curve attribute string is a body muscle this module drives."""
    return attribute in MUSCLE_DOF


def is_root(attribute):
    """True if a float-curve attribute is a body root-motion channel: the hips'
    pose in the animation-root frame (RootT/RootQ) or the root motion
    (MotionT/MotionQ) used to make it root-local.  None are muscles."""
    return attribute.split(".", 1)[0] in ("RootT", "RootQ", "MotionT", "MotionQ")
