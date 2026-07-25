"""RuriRipperPyBridge -- the host-agnostic half of the RuriRipper importers.

Everything in this package is plain Python (stdlib + numpy). It imports no DCC
API: no ``bpy``, no ``mathutils``, no ``substance_painter``, no Qt. That is the
single rule that decides whether code belongs here, and it is what lets the
Blender add-on (RuriRipperImporter) and the Substance Painter plugin
(RuriRipperImporterSubstance) share one implementation of the parts that are
genuinely about *Unity assets* and *the Ruri.RipperHook bridge* rather than
about the host application.

Layers, low to high; each may only import from the ones above it in this list:

``math3d``   coordinate spaces. A ``Space`` is the whole Unity -> target-system
             conversion (reflection matrix, winding, UV origin, tangent
             handedness) as data, so a host picks a space instead of
             reimplementing the maths.
``unity``    the Unity asset domain: the YAML subset parser, the class-id
             registry, guid/asset resolution (disk + in-memory bridge closure),
             mesh/vertex-stream decoding, the transform hierarchy, bind-pose
             skinning, prefab renderer discovery, material property reading,
             addressable-path/LOD rules, AnimationClip curve payloads, humanoid
             muscle retargeting.
``runtime``  process plumbing: the private dependency bootstrap, the CoreCLR
             claim and the pythonnet wrappers over
             ``Ruri.RipperHook.Bridge.RipperBlenderBridge``, the columnar cabmap
             row table, a JSON settings store and workspace resolution.
``session``  long-lived, host-agnostic session state built on top of those: the
             cabmap browser model (rows, virtual folder tree, filters, sort,
             selection) and the scene-placement discovery model.

Nothing is imported eagerly from here. ``runtime.bootstrap`` in particular has
to stay importable before numpy exists (installing numpy is its job), so this
module must never pull a submodule in as a side effect of ``import``.

Consumed as a git submodule; see README.md.
"""

__version__ = "1.0.0"
