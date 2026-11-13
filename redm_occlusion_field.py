bl_info = {
    'name': 'RedM Occlusion Field Editor', 'author': 'Freak Icon',
    'version': (1, 0, 0), 'blender': (4, 5, 2),
    'location': 'File > Import/Export; Sidebar > RedM Occlusion',
    'description': 'Edit entire RDR2 occlusion YMAPs from xml exported using "CodeX - by Dexyfex" ',
    'category': 'Import-Export',
}

import bpy
import copy
import hashlib
import json
import math
import os
import struct
import xml.etree.ElementTree as ET
from mathutils import Matrix, Vector
from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.props import StringProperty


FIELDS = {
    'CMapData': 0xD3593FA6, 'occludeModels': 0x7F1998DD,
    'boxOccluders': 0xED70C614, 'bmin': 0x78E9A1BF, 'bmax': 0xBFF5EFEB,
    'dataSize': 0x9199755B, 'verts': 0x072EA9EF, 'numTris': 0x8B566566,
    'numVertsInBytes': 0x32E6AB8B, 'flags': 0x67D26872,
    'CenterXYZ_CosZ': 0xCC2290C3, 'SizeXYZ_SinZ': 0x614B958A,
    'streamingExtentsMin': 0xDD22821F, 'streamingExtentsMax': 0xA22EA335,
    'entitiesExtentsMin': 0x1C75BCF1, 'entitiesExtentsMax': 0x6D074437,
    'contentFlags': 0x6A675035,
}


def matches(tag, name):
    return tag == name or tag.upper().endswith(f'0X{FIELDS[name]:08X}')


def field(parent, name, required=True):
    element = next((e for e in parent if matches(e.tag, name)), None)
    if element is None and required:
        raise ValueError(f'Missing RDR2 field: {name}')
    return element


def number(parent, name):
    element = field(parent, name)
    value = element.get('value', element.text or '0').strip()
    return int(value, 16) if value.lower().startswith('0x') else int(value)


def vector(element, size=3):
    values = tuple(float(element.get(axis, '0')) for axis in 'xyzw'[:size])
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Non-finite coordinates in occlusion data')
    return values


def set_number(parent, name, value):
    element = field(parent, name)
    element.text = None
    element.set('value', str(value))


def set_vector(element, values):
    for axis, value in zip('xyzw', values):
        element.set(axis, format(value, '.9g'))


def decode_model(item):
    data = bytes.fromhex(field(item, 'verts').text or '')
    size = number(item, 'dataSize')
    vertex_bytes = number(item, 'numVertsInBytes')
    if size != len(data) or vertex_bytes % 12 or vertex_bytes > size:
        raise ValueError('Invalid occlusion vertex buffer size')
    indices = data[vertex_bytes:]
    if len(indices) % 3:
        raise ValueError('Occlusion index buffer is not a triangle list')
    vertices = list(struct.iter_unpack('<3f', data[:vertex_bytes]))
    if not all(math.isfinite(v) for co in vertices for v in co):
        raise ValueError('Non-finite occlusion vertices')
    if indices and max(indices) >= len(vertices):
        raise ValueError('Occlusion triangle index outside vertex buffer')
    if (number(item, 'numTris') & 0x7FFF) != len(indices) // 3:
        raise ValueError('Occlusion triangle count disagrees with buffer')
    return vertices, [tuple(indices[i:i+3]) for i in range(0, len(indices), 3)]


BOX_VERTICES = [(-.5, -.5, -.5), (-.5, -.5, .5), (-.5, .5, -.5), (-.5, .5, .5),
                (.5, -.5, -.5), (.5, -.5, .5), (.5, .5, -.5), (.5, .5, .5)]
BOX_FACES = [(0, 4, 6, 2), (1, 3, 7, 5), (0, 1, 5, 4),
             (2, 6, 7, 3), (0, 2, 3, 1), (4, 5, 7, 6)]


def fingerprint(obj):
    values = ([list(v.co) for v in obj.data.vertices],
              [list(p.vertices) for p in obj.data.polygons],
              [list(row) for row in obj.matrix_world], obj.get('rdr_occ_flags', ''))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def make_object(collection, name, vertices, faces, kind, index):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj['rdr_occ_kind'] = kind
    obj['rdr_occ_index'] = index
    obj.color = (.65, .65, .65, 1) if kind == 'MODEL' else (.3, .65, 1, 1)
    obj.show_wire = True
    obj.show_all_edges = True
    return obj


def import_field(filepath, context):
    xml = open(filepath, encoding='utf-8-sig').read()
    root = ET.fromstring(xml)
    if not matches(root.tag, 'CMapData'):
        raise ValueError('Expected a complete RDR2 CMapData XML exported by CodeX')
    models = field(root, 'occludeModels', False)
    boxes = field(root, 'boxOccluders', False)
    # Validate the entire file before adding anything to the Blender scene.
    decoded = [decode_model(item) for item in models] if models is not None else []
    decoded_boxes = [(vector(field(i, 'CenterXYZ_CosZ'), 4),
                      vector(field(i, 'SizeXYZ_SinZ'), 4)) for i in boxes] if boxes is not None else []
    if not decoded and not decoded_boxes:
        raise ValueError('This YMAP contains no occlusion models or boxes')
    name = os.path.basename(filepath).split('.ymap')[0]
    collection = bpy.data.collections.new(name + ' • Occlusion')
    context.scene.collection.children.link(collection)
    source = bpy.data.texts.new(name + '.occlusion-source.xml')
    source.use_fake_user = True
    source.write(xml)
    collection['rdr_occ_source'] = source.name
    collection['rdr_occ_filename'] = os.path.basename(filepath)
    for i, (vertices, faces) in enumerate(decoded):
        obj = make_object(collection, f'{name} • Model {i:03}', vertices, faces, 'MODEL', i)
        obj['rdr_occ_flags'] = str(number(models[i], 'flags'))
        obj['rdr_occ_fingerprint'] = fingerprint(obj)
    for i, (center, size) in enumerate(decoded_boxes):
        obj = make_object(collection, f'{name} • Box {i:03}', BOX_VERTICES, BOX_FACES, 'BOX', i)
        obj.location = center[:3]
        # CodeX viewport: atan2(CenterXYZ_CosZ.w, SizeXYZ_SinZ.w).
        obj.rotation_euler.z = math.atan2(center[3], size[3])
        obj.scale = size[:3]
    context.view_layer.update()
    for obj in collection.objects:
        obj['rdr_occ_fingerprint'] = fingerprint(obj)
    context.scene.rdr_occ_collection = collection.name
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in collection.objects:
        obj.select_set(True)
    context.view_layer.objects.active = next(iter(collection.objects))
    return collection


def chunks(mesh, matrix):
    mesh.calc_loop_triangles()
    positions, triangles, lookup = [], [], {}
    for triangle in mesh.loop_triangles:
        keys = tuple(triangle.vertices)
        if len(lookup) + len(set(keys) - lookup.keys()) > 256 or len(triangles) >= 16383:
            yield positions, triangles
            positions, triangles, lookup = [], [], {}
        face = []
        for index in keys:
            if index not in lookup:
                co = tuple(matrix @ mesh.vertices[index].co)
                if not all(math.isfinite(v) for v in co):
                    raise ValueError('Non-finite edited vertex coordinates')
                lookup[index] = len(positions)
                positions.append(co)
            face.append(lookup[index])
        triangles.append(face)
    if triangles:
        yield positions, triangles


def encode_model(template, vertices, faces, flags):
    item = copy.deepcopy(template)
    data = b''.join(struct.pack('<3f', *co) for co in vertices) + bytes(i for f in faces for i in f)
    field(item, 'verts').text = '\n' + '\n'.join(data[i:i+32].hex(' ').upper() for i in range(0, len(data), 32)) + '\n'
    set_number(item, 'dataSize', len(data))
    set_number(item, 'numVertsInBytes', len(vertices) * 12)
    set_number(item, 'numTris', len(faces) | 0x8000)
    set_number(item, 'flags', flags)
    set_vector(field(item, 'bmin'), [min(co[a] for co in vertices) for a in range(3)])
    set_vector(field(item, 'bmax'), [max(co[a] for co in vertices) for a in range(3)])
    return item


def encode_box(template, obj, mesh):
    if len(mesh.vertices) != 8 or len(mesh.polygons) != 6 or any(len(p.vertices) != 4 for p in mesh.polygons):
        raise ValueError(f'{obj.name}: box topology changed. Edit model occluders for arbitrary shapes.')
    lo = Vector([min(v.co[a] for v in mesh.vertices) for a in range(3)])
    hi = Vector([max(v.co[a] for v in mesh.vertices) for a in range(3)])
    if any(hi[a] <= lo[a] for a in range(3)):
        raise ValueError(f'{obj.name}: box dimensions must be positive')
    combinations = set()
    for vertex in mesh.vertices:
        bits = []
        for a in range(3):
            if abs(vertex.co[a] - lo[a]) < 1e-5: bits.append(0)
            elif abs(vertex.co[a] - hi[a]) < 1e-5: bits.append(1)
            else: raise ValueError(f'{obj.name}: native boxes must remain rectangular')
        combinations.add(tuple(bits))
    if len(combinations) != 8:
        raise ValueError(f'{obj.name}: box corners overlap')
    for poly in mesh.polygons:
        if not any(max(mesh.vertices[i].co[a] for i in poly.vertices) - min(mesh.vertices[i].co[a] for i in poly.vertices) < 1e-5 for a in range(3)):
            raise ValueError(f'{obj.name}: box face does not lie on a cuboid side')
    matrix = obj.matrix_world
    axes = [matrix.to_3x3().col[a] for a in range(3)]
    lengths = [axis.length for axis in axes]
    if min(lengths) < 1e-8 or matrix.to_3x3().determinant() <= 0:
        raise ValueError(f'{obj.name}: box scale must be nonzero and have positive determinant')
    unit = [axis.normalized() for axis in axes]
    if abs(unit[2].z - 1) > 1e-5 or any(abs(unit[a].dot(unit[b])) > 1e-5 for a, b in ((0, 1), (0, 2), (1, 2))):
        raise ValueError(f'{obj.name}: native boxes support Z rotation only, without shear')
    center = matrix @ ((lo + hi) * .5)
    size = [(hi[a] - lo[a]) * lengths[a] for a in range(3)]
    item = copy.deepcopy(template)
    set_vector(field(item, 'CenterXYZ_CosZ'), (*center, unit[0].y))
    set_vector(field(item, 'SizeXYZ_SinZ'), (*size, unit[0].x))
    return item


def export_field(collection, filepath, context):
    if context.mode != 'OBJECT':
        raise ValueError('Switch to Object Mode before exporting')
    context.view_layer.update()
    text = bpy.data.texts.get(collection.get('rdr_occ_source', ''))
    if text is None:
        raise ValueError('Original XML is missing. Reimport the complete CodeX YMAP.')
    root = ET.fromstring(text.as_string())
    models = field(root, 'occludeModels', False)
    boxes = field(root, 'boxOccluders', False)
    originals = {'MODEL': list(models) if models is not None else [], 'BOX': list(boxes) if boxes is not None else []}
    output = {'MODEL': [], 'BOX': []}
    bounds = []
    changed = False
    depsgraph = context.evaluated_depsgraph_get()
    objects = [o for o in collection.all_objects if o.get('rdr_occ_kind') in output]
    for obj in objects:
        kind = obj['rdr_occ_kind']
        index = obj.get('rdr_occ_index', -1)
        if not 0 <= index < len(originals[kind]) or obj.type != 'MESH':
            raise ValueError(f'{obj.name}: missing source occluder template; duplicate an imported object to add geometry')
        template = originals[kind][index]
        if not obj.modifiers and fingerprint(obj) == obj.get('rdr_occ_fingerprint'):
            output[kind].append(copy.deepcopy(template))
            continue
        changed = True
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            if kind == 'MODEL':
                flags = int(obj.get('rdr_occ_flags', str(number(template, 'flags'))))
                if not 0 <= flags <= 0xFFFFFFFF:
                    raise ValueError(f'{obj.name}: flags must fit an unsigned 32-bit integer')
                for vertices, faces in chunks(mesh, evaluated.matrix_world):
                    output[kind].append(encode_model(template, vertices, faces, flags))
                    bounds.extend(vertices)
            else:
                output[kind].append(encode_box(template, evaluated, mesh))
                bounds.extend(tuple(evaluated.matrix_world @ v.co) for v in mesh.vertices)
        finally:
            evaluated.to_mesh_clear()
    for kind, container in (('MODEL', models), ('BOX', boxes)):
        if container is not None:
            container[:] = output[kind]
    if changed and bounds:
        for name, fn in (('streamingExtentsMin', min), ('streamingExtentsMax', max),
                         ('entitiesExtentsMin', min), ('entitiesExtentsMax', max)):
            element = field(root, name, False)
            if element is not None:
                original = vector(element)
                set_vector(element, [fn(original[a], *(co[a] for co in bounds)) for a in range(3)])
    flags_element = field(root, 'contentFlags', False)
    if flags_element is not None:
        flags = number(root, 'contentFlags')
        set_number(root, 'contentFlags', flags | 32 if output['MODEL'] or output['BOX'] else flags & ~32)
    xml = ET.tostring(root, encoding='unicode')
    # Validate every generated model before writing any file.
    for item in output['MODEL']:
        decode_model(item)
    with open(filepath, 'w', encoding='utf-8') as stream:
        stream.write('<?xml version="1.0" encoding="UTF-8"?>\n' + xml)
    return len(output['MODEL']), len(output['BOX'])


def active_collection(context):
    chosen = bpy.data.collections.get(context.scene.rdr_occ_collection)
    if chosen and chosen.get('rdr_occ_source'):
        return chosen
    obj = context.object
    if obj:
        for collection in obj.users_collection:
            if collection.get('rdr_occ_source'):
                return collection
    return None


class RDR_OCC_OT_convert_box(bpy.types.Operator):
    bl_idname = 'object.redm_occlusion_box_to_model'
    bl_label = 'Convert Selected Boxes to Editable Models'
    bl_description = 'Allow arbitrary mesh edits; converted models use flags 0, editable in this panel'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        collection = active_collection(context)
        if not collection or context.mode != 'OBJECT':
            self.report({'ERROR'}, 'Choose an imported field in Object Mode')
            return {'CANCELLED'}
        source = bpy.data.texts.get(collection.get('rdr_occ_source', ''))
        root = ET.fromstring(source.as_string()) if source else None
        models = field(root, 'occludeModels', False) if root is not None else None
        if models is None or not len(models):
            self.report({'ERROR'}, 'This field has no source model template to use for conversion')
            return {'CANCELLED'}
        count = 0
        for obj in context.selected_objects:
            if obj.name in collection.all_objects and obj.get('rdr_occ_kind') == 'BOX':
                obj['rdr_occ_kind'] = 'MODEL'
                obj['rdr_occ_index'] = 0
                obj['rdr_occ_flags'] = '0'
                obj['rdr_occ_fingerprint'] = ''
                count += 1
        self.report({'INFO'}, f'Converted {count} boxes to models with flags 0')
        return {'FINISHED'}


class RDR_OCC_OT_import(bpy.types.Operator, ImportHelper):
    bl_idname = 'import_scene.redm_occlusion_field'
    bl_label = 'RedM Occlusion Field (CodeX XML)'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.xml'
    filter_glob: StringProperty(default='*.xml', options={'HIDDEN'})

    def execute(self, context):
        try:
            collection = import_field(self.filepath, context)
            self.report({'INFO'}, f'Imported {len(collection.objects)} occluders as editable meshes')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class RDR_OCC_OT_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'export_scene.redm_occlusion_field'
    bl_label = 'RedM Occlusion Field (CodeX XML)'
    filename_ext = '.xml'
    filter_glob: StringProperty(default='*.xml', options={'HIDDEN'})

    def invoke(self, context, event):
        collection = active_collection(context)
        if collection:
            self.filepath = collection.get('rdr_occ_filename', 'occlusion.ymap.rsc.xml')
        return super().invoke(context, event)

    def execute(self, context):
        try:
            collection = active_collection(context)
            if collection is None:
                raise ValueError('Import a CodeX occlusion YMAP first')
            count, boxes = export_field(collection, self.filepath, context)
            self.report({'INFO'}, f'Exported whole field: {count} models, {boxes} boxes. Build native YMAP in CodeX.')
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class RDR_OCC_PT_panel(bpy.types.Panel):
    bl_label = 'RedM Occlusion Field'
    bl_idname = 'RDR_OCC_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'RedM Occlusion'

    def draw(self, context):
        layout = self.layout
        layout.operator(RDR_OCC_OT_import.bl_idname, text='Import Entire YMAP Field')
        layout.prop_search(context.scene, 'rdr_occ_collection', bpy.data, 'collections', text='Field')
        collection = active_collection(context)
        if collection:
            layout.label(text=f'{len(collection.all_objects)} objects in field')
        layout.operator(RDR_OCC_OT_export.bl_idname, text='Export Entire YMAP Field')
        layout.label(text='Exports all occluders, including hidden ones.')
        layout.label(text='Delete objects to remove occlusion.')
        layout.label(text='Duplicate imported objects to add occlusion.')
        layout.label(text='Native boxes: Z rotation and cuboid edits only.')
        layout.operator(RDR_OCC_OT_convert_box.bl_idname, text='Convert Boxes to Models')
        obj = context.object
        if obj and obj.get('rdr_occ_kind') == 'MODEL':
            layout.prop(obj, '["rdr_occ_flags"]', text='Model flags')


def menu_import(self, context):
    self.layout.operator(RDR_OCC_OT_import.bl_idname)


def menu_export(self, context):
    self.layout.operator(RDR_OCC_OT_export.bl_idname)


CLASSES = (RDR_OCC_OT_import, RDR_OCC_OT_export, RDR_OCC_OT_convert_box, RDR_OCC_PT_panel)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.rdr_occ_collection = StringProperty(name='Occlusion field')
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    del bpy.types.Scene.rdr_occ_collection
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
