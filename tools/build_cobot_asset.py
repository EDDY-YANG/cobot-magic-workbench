"""Blender-only conversion of the verified upstream whole-machine URDF.

Local author review. Exact visual meshes and zero joint positions, no dynamics.
Third-party redistribution clearance is separate from project-code MIT licensing.
"""
from pathlib import Path
import bpy
import math
import json
import hashlib
import xml.etree.ElementTree as ET
from mathutils import Matrix, Euler, Vector

ROOT=Path(__file__).resolve().parents[1]
UPSTREAM=ROOT.parents[1]/"2026-10-20-18-16-AgileX-Digital-Twin-Bundle"/"01_robot_models"/"official-four-arm"
OUT=ROOT/"site"/"assets"/"robot"
OUT.mkdir(parents=True,exist_ok=True)
manifest=json.loads((UPSTREAM/"2026-10-20-official-four-arm-source-manifest.json").read_text())
for item in manifest["files"]:
    raw=(UPSTREAM/item["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==item["sha256"],item["path"]
urdf=UPSTREAM/"aloha_new_description"/"urdf"/"aloha_new.urdf"
robot=ET.parse(urdf).getroot()
links=robot.findall("link")
joints=robot.findall("joint")
assert len(links)==50 and len(joints)==49

def transform(element):
    if element is None:return Matrix.Identity(4)
    xyz=[float(x) for x in element.get("xyz","0 0 0").split()]
    rpy=[float(x) for x in element.get("rpy","0 0 0").split()]
    return Matrix.Translation(Vector(xyz)) @ Euler(rpy,"XYZ").to_matrix().to_4x4()

parents={j.find("child").get("link") for j in joints}
roots=[l.get("name") for l in links if l.get("name") not in parents]
assert len(roots)==1
world={roots[0]:Matrix.Identity(4)}
remaining=joints.copy()
while remaining:
    count=len(remaining)
    for j in remaining[:]:
        parent=j.find("parent").get("link")
        if parent not in world:continue
        world[j.find("child").get("link")]=world[parent] @ transform(j.find("origin"))
        remaining.remove(j)
    assert len(remaining)<count,"Disconnected URDF"
assert len(world)==50
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene
scene.unit_settings.system="METRIC"
scene.unit_settings.scale_length=1.0

cache={}
def load_mesh_scene(path):
    if path in cache:return cache[path]
    before=set(bpy.data.objects)
    if path.suffix.lower()==".dae":
        tree=ET.parse(path)
        ns={"c":"http://www.collada.org/2005/11/COLLADASchema"}
        unit=tree.find("c:asset/c:unit",ns)
        axis=tree.find("c:asset/c:up_axis",ns)
        assert float(unit.get("meter","1"))==1 and axis.text.strip()=="Z_UP",path
        bpy.ops.wm.collada_import(filepath=str(path),import_units=True,fix_orientation=False,
                                  auto_connect=False,find_chains=False)
    else:
        raise ValueError("Unexpected visual format: "+str(path))
    imported=list(set(bpy.data.objects)-before)
    bpy.context.view_layer.update()
    meshes=[(o.data,o.matrix_world.copy()) for o in imported if o.type=="MESH"]
    assert meshes,path
    # Keep mesh datablocks and their source scene transformations for instances.
    for mesh,_ in meshes:mesh.use_fake_user=True
    for obj in imported:bpy.data.objects.remove(obj,do_unlink=True)
    cache[path]=meshes
    return meshes

objects=[]
visual_receipts=[]
for link in links:
    name=link.get("name")
    for index,visual in enumerate(link.findall("visual")):
        mesh=visual.find("geometry/mesh")
        assert mesh is not None,(name,"non-mesh visual needs explicit review")
        uri=mesh.get("filename")
        assert uri.startswith("package://aloha_new_description/")
        path=UPSTREAM/uri.removeprefix("package://")
        matrix=world[name] @ transform(visual.find("origin"))
        scale=[float(x) for x in mesh.get("scale","1 1 1").split()]
        matrix=matrix @ Matrix.Diagonal(Vector(scale+[1]))
        source=load_mesh_scene(path)
        visual_receipts.append({"link":name,"visual_index":index,
            "mesh":path.relative_to(UPSTREAM).as_posix(),
            "mesh_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_objects":len(source),"world_visual_matrix":[list(row) for row in matrix]})
        for part,(data,local) in enumerate(source):
            obj=bpy.data.objects.new(f"{name}__v{index}__part{part}",data)
            scene.collection.objects.link(obj)
            obj.matrix_world=matrix @ local
            obj["urdf_link"]=name
            objects.append(obj)
bpy.context.view_layer.update()
assert len(visual_receipts)==48
for prefix in ("fl","fr","bl","br"):
    assert sum(1 for x in visual_receipts if x["link"].startswith(prefix+"_"))>=9
for name in ("base_link","body_Link","right_wheel_link","left_wheel_link"):
    assert any(x["link"]==name for x in visual_receipts)
bounds=[obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
lo=Vector([min(v[i] for v in bounds) for i in range(3)])
hi=Vector([max(v[i] for v in bounds) for i in range(3)])
center=(lo+hi)/2
span=max(hi-lo)
assert .5<span<5,(lo,hi)
bpy.ops.object.select_all(action="DESELECT")
for obj in objects:obj.select_set(True)
glb=OUT/"cobot-magic-source-zero.glb"
bpy.ops.export_scene.gltf(filepath=str(glb),export_format="GLB",use_selection=True,
    export_yup=True,export_animations=False,export_extras=True,
    export_apply=False,export_cameras=False,export_lights=False)
# Studio rendering of exactly the assembled objects; no proxy frame or robot.
scene.render.engine="BLENDER_EEVEE_NEXT"
scene.render.resolution_x=1400
scene.render.resolution_y=1000
scene.render.resolution_percentage=100
scene.render.image_settings.file_format="PNG"
scene.render.film_transparent=False
scene.world.use_nodes=True
scene.world.node_tree.nodes["Background"].inputs[0].default_value=(1,1,1,1)
scene.world.node_tree.nodes["Background"].inputs[1].default_value=.75
scene.view_settings.view_transform="Standard"
scene.view_settings.look="Medium High Contrast"
def aim(obj,point):
    obj.rotation_euler=(point-obj.location).to_track_quat("-Z","Y").to_euler()
cam_data=bpy.data.cameras.new("Presentation camera")
cam=bpy.data.objects.new("Presentation camera",cam_data)
scene.collection.objects.link(cam)
cam.location=center+Vector((2.5,-3.2,1.65))*span
aim(cam,center)
cam_data.type="ORTHO"
cam_data.ortho_scale=span*1.52
scene.camera=cam
for position,power,size in [((2,-3,4),1300,4),((-3,-1,3),900,3),((0,4,3),1000,3)]:
    data=bpy.data.lights.new("Studio light","AREA")
    data.energy=power
    data.shape="DISK"
    data.size=size
    light=bpy.data.objects.new("Studio light",data)
    scene.collection.objects.link(light)
    light.location=center+Vector(position)*span
    aim(light,center)
scene.render.filepath=str(OUT/"cobot-magic-source-zero.png")
bpy.ops.render.render(write_still=True)
receipt={
 "source_repository":"https://github.com/agilexrobotics/mobile_aloha_sim",
 "source_commit":"f799ae0192c0c2bbd502ec4d9bbec8c2633acd1a",
 "source_urdf":"aloha_new_description/urdf/aloha_new.urdf",
 "source_urdf_sha256":hashlib.sha256(urdf.read_bytes()).hexdigest(),
 "verified_source_files":len(manifest["files"]),
 "links":len(links),"joints":len(joints),"visual_instances":len(visual_receipts),
 "unique_mesh_files":len(cache),"mesh_objects":len(objects),
 "joint_configuration":"All movable joint values are zero in the source URDF coordinates.",
 "transform_convention":"URDF parent-to-child origin; Rz(yaw) Ry(pitch) Rx(roll); DAE Z_UP and meter=1; glTF standard Y-up export.",
 "source_bounds_m":{"min":list(lo),"max":list(hi)},
 "geometry_decimation":False,"geometry_replacement":False,
 "source_layout_matches_physical_installation":"NOT_CALIBRATED",
 "physics_simulation_executed":False,"live_robot_feedback_used":False,
 "package_license_declaration":"BSD",
 "upstream_full_license_text":"Not present in the archived source bundle; redistribution review pending.",
 "public_distribution":"PENDING_ASSET_LICENSE_REVIEW",
 "exporter":bpy.app.version_string,
 "glb_bytes":glb.stat().st_size,"glb_sha256":hashlib.sha256(glb.read_bytes()).hexdigest(),
 "visuals":visual_receipts}
(OUT/"asset-provenance.json").write_text(json.dumps(receipt,indent=2),encoding="utf8")
print("ASSET_RESULT",json.dumps({k:v for k,v in receipt.items() if k!="visuals"}))
