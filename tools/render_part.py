"""CAD-part QA render: GLB -> PNG via Blender WORKBENCH with studio lighting + cavity + outline.

The placement render preset renders parts as flat silhouettes (Workbench fallback ignores scene lights;
elevation 12 is near edge-on for squat parts; dark background) — useless for visual QA. This renders like a
CAD viewport instead: studio shading, cavity (edges/teeth/grooves pop), object outline, light background,
30-degree elevation. Deterministic and CPU-only (no EEVEE/GPU context).

    blender --background --python render_part.py -- <in.glb> <out.png> [res] [azim_deg] [elev_deg]
"""
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
glb, png = Path(argv[0]), Path(argv[1])
res = int(argv[2]) if len(argv) > 2 else 900
azim = float(argv[3]) if len(argv) > 3 else -35.0
elev = float(argv[4]) if len(argv) > 4 else 30.0

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.import_scene.gltf(filepath=str(glb))
for obj in list(bpy.data.objects):                      # VRM addon scaffolding (see placement script)
    if obj.type == "MESH" and obj.name.startswith("Icosphere"):
        bpy.data.objects.remove(obj, do_unlink=True)

pts = [o.matrix_world @ Vector(c) for o in bpy.context.scene.objects if o.type == "MESH" for c in o.bound_box]
mins = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
maxs = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
center = (mins + maxs) * 0.5
radius = max((maxs - mins).length / 2.0, 1e-6)

cam_data = bpy.data.cameras.new("Cam")
cam = bpy.data.objects.new("Cam", cam_data)
bpy.context.collection.objects.link(cam)
bpy.context.scene.camera = cam
az, el = math.radians(azim), math.radians(elev)
direction = Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
cam.location = center + direction * (radius * 1.25 / math.sin(cam_data.angle / 2.0))
cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()

scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
sh = scene.display.shading
sh.light = 'STUDIO'
sh.color_type = 'SINGLE'
sh.single_color = (0.55, 0.62, 0.72)
sh.show_cavity = True
try:
    sh.cavity_type = 'BOTH'
except TypeError:
    pass
sh.show_object_outline = True
try:
    sh.background_type = 'WORLD'
except TypeError:
    pass
try:
    scene.display.render_aa = '8'
except TypeError:
    pass
scene.world = scene.world or bpy.data.worlds.new("World")
scene.world.use_nodes = True
bg = scene.world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[0].default_value = (0.92, 0.93, 0.95, 1.0)
    bg.inputs[1].default_value = 1.0
scene.render.resolution_x = res
scene.render.resolution_y = res
png.parent.mkdir(parents=True, exist_ok=True)
scene.render.filepath = str(png)
scene.render.image_settings.file_format = "PNG"
bpy.ops.render.render(write_still=True)
print(f"RENDER_DONE {png}")
