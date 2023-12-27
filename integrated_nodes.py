import json
import os
import sys
import traceback
import yaml

from nodes import NODE_CLASS_MAPPINGS as GLOBAL_NODE_CLASS_MAPPINGS
from nodes import NODE_DISPLAY_NAME_MAPPINGS as GLOBAL_NODE_DISPLAY_NAME_MAPPINGS
from server import PromptServer
from aiohttp import web

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = 'web'

CONFIG_FILE = 'integrated_nodes.yaml'
CONFIG_FILE_FALLBACK = 'integrated_nodes.yaml.example'
DEFAULT_CATEGORY = 'integrated'

current_config = CONFIG_FILE

class Node(object):
    def __init__(self, id, type, exported_inputs):
        cls = GLOBAL_NODE_CLASS_MAPPINGS.get(type)
        if cls is None:
            cls = NODE_CLASS_MAPPINGS.get(type)
        if cls is None:
            raise Exception(f"Unknown node type {type}")

        self.id = id
        self.cls = cls
        self.input_map = {}
        self.inputs = []
        self.outputs = []
        self.output_types = []
        self.output_names = []

        types = getattr(self.cls, "RETURN_TYPES", ())
        names = getattr(self.cls, "RETURN_NAMES", types)
        for type, name in zip(types, names):
            self.outputs.append([])
            self.output_types.append(type)
            self.output_names.append(name)

        inputs = self.cls.INPUT_TYPES()
        for name, descriptor in inputs.get("required", {}).items():
            input = RequiredInput(register=new_register(), name=name, descriptor=descriptor)
            self.input_map[name] = input.register
            self.inputs.append(input)
        for name, descriptor in inputs.get("optional", {}).items():
            input = OptionalInput(register=new_register(), name=name, descriptor=descriptor)
            self.input_map[name] = input.register
            self.inputs.append(input)
        for name, type in inputs.get("hidden", {}).items():
            # Hidden inputs are always exported
            if name not in exported_inputs:
                exported_inputs[name] = HiddenInput(register=new_register(), descriptor=type)
            elif exported_inputs[name].descriptor != type:
                warn(f"Mismatched types for hidden input {name}: {type} and {exported_inputs[name].descriptor}")
            self.input_map[name] = exported_inputs[name].register


    @property
    def output_node(self):
        return hasattr(self.cls, "OUTPUT_NODE") and self.cls.OUTPUT_NODE


    def input_by_name(self, name):
        return next(input for input in self.inputs if input.name == name)


    def assign_defaults_list(self, defaults):
        skip_next = False
        i = 0
        for value in defaults:
            if skip_next:
                skip_next = False
                continue

            while True:
                # Find the next input that isn't an input slot
                input = self.inputs[i]
                i += 1
                if not input.link and (isinstance(input.descriptor[0], list) or len(input.descriptor) >= 2):
                    break

            input.set_default_value(value)

            if input.type == "INT" and input.name in ("seed", "denoise_seed"):
                # Seed inputs get an additional control_after_generate widget, ignore its value
                skip_next = True
            elif len(input.descriptor) > 1 and input.descriptor[1].get("image_upload") is True:
                # Image upload inputs get an additional IMAGEUPLOAD widget, ignore its value
                skip_next = True


    def assign_defaults_map(self, defaults):
        for name, value in defaults.items():
            if name == "choose file to upload":
                continue
            if isinstance(value, list) and len(value) == 2:
                self.input_by_name(name).link = (value[0], value[1])
            else:
                self.input_by_name(name).set_default_value(value)


class Input(object):
    def __init__(self, register, name, descriptor):
        self.registers = [register]
        self.name = name
        self.descriptor = descriptor
        self.link = None


    def get_default_value(self):
        if len(self.descriptor) >= 2 and "default" in self.descriptor[1]:
            return self.descriptor[1]["default"]
        elif self.descriptor[0] == "STRING":
            return ""
        elif self.descriptor[0] == "INT":
            return 0
        elif self.descriptor[0] == "FLOAT":
            return 0.0
        elif self.descriptor[0] == "BOOLEAN":
            return False
        elif isinstance(self.descriptor[0], list) and len(self.descriptor[0]) > 0:
            return self.descriptor[0][0]
        return None


    def set_default_value(self, value):
        if len(self.descriptor) == 1:
            self.descriptor = (self.type, {"default": value})
        else:
            self.descriptor[1]["default"] = value

    @property
    def register(self):
        return self.registers[0]


    @property
    def type(self):
        return self.descriptor[0]


class RequiredInput(Input):
    COLLECTION = "required"


class OptionalInput(Input):
    COLLECTION = "optional"


class HiddenInput(object):
    COLLECTION = "hidden"

    def __init__(self, register, descriptor):
        self.register = register
        self.descriptor = descriptor

    @property
    def registers(self):
        return [self.register]


class Output(object):
    def __init__(self, register, name, type):
        self.register = register
        self.name = name
        self.type = type


class NodeProcessor(object):
    # Overwritten by subclasses
    NODE = None

    def __init__(self):
        self.inner = self.NODE.cls()


    @classmethod
    def map_inputs(s, state):
        params = {}
        for name, register in s.NODE.input_map.items():
            try:
                params[name] = state[register]
            except KeyError:
                # ignore missing parameters, probably optional
                pass
        return params


    @classmethod
    def validate(s, state):
        if hasattr(s.NODE.cls, "VALIDATE_INPUTS"):
            return s.NODE.cls.VALIDATE_INPUTS(**s.map_inputs(state))
        else:
            return True


    @classmethod
    def has_is_changed(s):
        return hasattr(s.NODE.cls, "IS_CHANGED")


    @classmethod
    def is_changed(s, state):
        return s.NODE.cls.IS_CHANGED(**s.map_inputs(state))


    def process(self, state, ui):
        function_name = self.NODE.cls.FUNCTION
        result = getattr(self.inner, function_name)(**self.map_inputs(state))
        if isinstance(result, dict):
            outputs = result.get("result", ())
            for key, value in result.get("ui", {}).items():
                ui.setdefault(key, []).extend(value)
        else:
            outputs = result
        for register_ids, value in zip(self.NODE.outputs, outputs):
            for register_id in register_ids:
                state[register_id] = value


class IntegratedNode(object):
    FUNCTION = "process"

    # Overwritten by subclasses
    PROCESSORS = None
    INPUTS = None
    OUTPUTS = None
    INITIAL_STATE = None

    def __init__(self):
        self.processors = [processor() for processor in self.PROCESSORS]
        pass


    @classmethod
    def construct_state(s, **kwargs):
        state = s.INITIAL_STATE.copy()
        for name, value in kwargs.items():
            try:
                input = s.INPUTS[name]
            except KeyError:
                raise Exception(f"Unexpected parameter {name}")
            for register in input.registers:
                state[register] = value

        return state


    @classmethod
    def INPUT_TYPES(s):
        types = {}
        for name, input in s.INPUTS.items():
            types.setdefault(input.COLLECTION, {})[name] = input.descriptor
        return types

    @classmethod
    @property
    def RETURN_TYPES(s):
        return [output.type for output in s.OUTPUTS]


    @classmethod
    @property
    def RETURN_NAMES(s):
        return [output.name for output in s.OUTPUTS]


    @classmethod
    def VALIDATE_INPUTS(s, **kwargs):
        state = s.construct_state(**kwargs)
        for processor in s.PROCESSORS:
            validation_result = processor.validate(state)
            if validation_result is not True:
                return validation_result
        return True


    @classmethod
    def _IS_CHANGED(s, **kwargs):
        state = s.construct_state(**kwargs)
        result = []
        for processor in s.PROCESSORS:
            if processor.has_is_changed():
                result.append(processor.is_changed(state))
        return "".join(result)


    def process(self, **kwargs):
        state = self.construct_state(**kwargs)
        ui = {}
        for processor in self.processors:
            processor.process(state, ui)

        result = []
        for output in self.OUTPUTS:
            result.append(state[output.register])
        return {
            "result": result,
            "ui": ui,
        }


def warn(warning):
    print(f"{current_config}: {warning}", file=sys.stderr)


max_register_id = 0
def new_register():
    global max_register_id
    max_register_id += 1
    return max_register_id


def create_nodes(workflow):
    if not isinstance(workflow, dict):
        raise Exception("Unknown workflow format")

    if isinstance(workflow.get("templates"), list):
        # Got a node template file, extract the actual workflow
        templates = workflow["templates"]
        if len(templates) == 0:
            raise Exception("Node templates file contains no templates")
        if len(templates) > 1:
            warn("Node templates file contains multiple templates, only the first one will be used")
        template = templates[0]
        if not isinstance(template, dict):
            raise Exception("Provided node template is not a dictionary")
        try:
            workflow = json.loads(template["data"])
        except:
            traceback.print_exc()
            raise Exception("Node template data isn't a valid JSON string")

    nodes = []
    exported_inputs = {}

    if isinstance(workflow.get("nodes"), list):
        # Got a workflow file
        links = {}
        for link in workflow.get("links", []):
            if len(link) == 6:
                # Workflow
                _, from_id, from_slot, to_id, to_slot, _ = link
            else:
                # Node template
                from_id, from_slot, to_id, to_slot, _ = link
            links.setdefault(to_id, {})[to_slot] = [from_id, from_slot]

        for id, node_workflow in enumerate(workflow["nodes"]):
            node = Node(node_workflow.get("id", id), node_workflow["type"], exported_inputs)
            for slot, link in links.get(node.id, {}).items():
                node.input_by_name(node_workflow["inputs"][slot]["name"]).link = link
            node.assign_defaults_list(node_workflow.get("widgets_values", []))
            nodes.append(node)
    else:
        # Prompt API file
        for id, node_workflow in workflow.items():
            node = Node(id, node_workflow["class_type"], exported_inputs)
            node.assign_defaults_map(node_workflow.get("inputs", {}))
            nodes.append(node)

    return nodes, exported_inputs


def connect_links(nodes):
    def node_by_id(id):
        return next(node for node in nodes if node.id == id)

    linked_inputs = set()
    dependencies = {}
    for node in nodes:
        for input in node.inputs:
            if not input.link:
                continue

            from_id, from_slot = input.link
            from_node = node_by_id(from_id)
            dependencies.setdefault(node, set()).add(from_node)

            output_type = from_node.output_types[from_slot]
            if input.type != output_type:
                raise Exception(f"Cannot connect input of type {input} to output of type {output_type}")

            from_node.outputs[from_slot].append(input.register)
            linked_inputs.add(input.register)
    return linked_inputs, dependencies


def create_node_processor(node):
    return type("NodeProcessor", (NodeProcessor,), {
        "NODE": node,
    })


def process_workflow(workflow, export_outputs, rename_outputs):
    nodes, exported_inputs = create_nodes(workflow)
    linked_inputs, dependencies = connect_links(nodes)

    exported_outputs = []
    for node in nodes:
        for targets, name, type in zip(node.outputs, node.output_names, node.output_types):
            output_id = f"{node.id} {name}"
            # Use explicitly specified exports, fall back to exporting everything not linking anywhere
            if (export_outputs is not None and output_id in export_outputs) or (export_outputs is None and len(targets) == 0):
                output = Output(register=new_register(), name=rename_outputs.get(output_id, name), type=type)
                exported_outputs.append(output)
                targets.append(output.register)

        # Inputs without incoming links are exported
        for input in node.inputs:
            if input.register in linked_inputs:
                continue

            # Find a non-conflicting name for the input
            i = 1
            name = input.name
            while name in exported_inputs:
                i += 1
                name = f"{input.name}_{i}"
            exported_inputs[name] = input

    # Find a suitable execution order for the nodes
    execution_order = []
    while len(execution_order) != len(nodes):
        for node in nodes:
            if node in execution_order:
                continue
            if all(source in execution_order for source in dependencies.get(node, set())):
                execution_order.append(node)
                break
        else:
            raise Exception("Dependency loop detected")

    processors = list(map(create_node_processor, execution_order))
    is_output_node = any(node.output_node for node in nodes)

    return (processors, exported_inputs, exported_outputs, is_output_node)


def hide_inputs(inputs, hidden):
    initial_state = {}
    for name in hidden:
        try:
            input = inputs[name]
        except KeyError:
            warn(f"Input {name} not found, not hiding")
            continue

        value = input.get_default_value()
        if input.COLLECTION == "required" and value is None:
            warn(f"Cannot hide input {name}, it is required and has no default value")
            continue

        for register in input.registers:
            initial_state[register] = value
        del inputs[name]

    return initial_state


def merge_inputs(inputs, mapping):
    if not isinstance(mapping, dict):
        warn(f"merge_inputs entry should be a dictionary but got {mapping}, ignoring")
        return

    for target, sources in mapping.items():
        if not isinstance(sources, list):
            sources = [sources]

        try:
            target_input = inputs[target]
        except KeyError:
            warn(f"Target input {target} not found, merging skipped")
            continue

        for source in sources:
            try:
                source_input = inputs[source]
            except KeyError:
                warn(f"Source input {source} not found, merging skipped")
                continue

            if target_input.type != source_input.type:
                warn(f"Cannot merge input {source} into {target}, type mismatch: {source_input.type} vs. {target_input.type}")
                continue

            target_input.registers += source_input.registers
            del inputs[source]


def rename_inputs(inputs, mapping):
    if not isinstance(mapping, dict):
        warn(f"rename_inputs entry should be a dictionary but got {mapping}, ignoring")
        return

    for old, new in mapping.items():
        if old not in inputs:
            warn(f"Cannot rename input {old}, no input with this name exists")
            continue

        if new in inputs:
            warn(f"Cannot rename input {old} to {new}, another input with this name already exists")
            continue

        inputs[new] = inputs[old]
        del inputs[old]


def create_integrated_node(name, info):
    if not isinstance(info, dict):
        warn(f"Ignoring integrated node {name}, not a dictionary")
        return

    try:
        workflow_path = info["workflow"]
    except KeyError:
        warn(f"Ignoring integrated node {name}, missing required workflow entry")
        return

    try:
        with open(os.path.join(os.path.dirname(__file__), workflow_path)) as input:
            workflow = json.load(input)
    except:
        traceback.print_exc()
        warn(f"Ignoring integrated node {name}, failed loading workflow from file {workflow_path}")
        return

    export_outputs = info.get("export_outputs")
    if export_outputs is not None and not isinstance(export_outputs, list):
        warn(f"export_outputs entry should be a list but got {export_outputs}, ignoring")
        export_outputs = None
    if export_outputs is not None:
        export_outputs = set(export_outputs)

    rename_outputs = info.get("rename_outputs", {})
    if not isinstance(rename_outputs, dict):
        warn(f"rename_outputs entry should be a dictionary but got {rename_outputs}, ignoring")
        rename_outputs = {}

    try:
        (processors, inputs, outputs, is_output_node) = process_workflow(workflow, export_outputs, rename_outputs)
    except:
        traceback.print_exc()
        warn(f"Ignoring integrated node {name}, failed processing workflow")
        return

    merge_inputs(inputs, info.get("merge_inputs", {}))
    initial_state = hide_inputs(inputs, info.get("hide_inputs", {}))
    rename_inputs(inputs, info.get("rename_inputs", {}))

    cls = type(name, (IntegratedNode,), {
        "PROCESSORS": processors,
        "INPUTS": inputs,
        "OUTPUTS": outputs,
        "INITIAL_STATE": initial_state,
        "CATEGORY": info.get("category", DEFAULT_CATEGORY),
        "OUTPUT_NODE": is_output_node,
    })

    if any(processor.has_is_changed() for processor in processors):
        cls.IS_CHANGED = cls._IS_CHANGED

    NODE_CLASS_MAPPINGS[name] = cls
    NODE_DISPLAY_NAME_MAPPINGS[name] = info.get("display_name", name)


def load_config():
    global current_config

    basedir = os.path.dirname(__file__)
    current_config = CONFIG_FILE
    path = os.path.join(basedir, current_config)
    if not os.path.exists(path):
        current_config = CONFIG_FILE_FALLBACK
        path = os.path.join(basedir, current_config)
    with open(path, 'r') as input:
        data = yaml.safe_load(input)

    if not isinstance(data, dict):
        warn("File does not contain a dictionary, ignoring")
        return

    for name, info in data.items():
        create_integrated_node(name, info)


async def add_node(request):
    global current_config

    post = await request.post()

    prompt = post.get('prompt')
    name = post.get('name')
    if prompt is None or name is None:
        return web.Response(status=400)

    display_name = post.get('displayName', name)
    category = post.get('category', DEFAULT_CATEGORY)

    basedir = os.path.dirname(__file__)
    config_path = os.path.join(basedir, CONFIG_FILE)
    if not os.path.exists(config_path):
        config_path = os.path.join(basedir, CONFIG_FILE_FALLBACK)
    with open(config_path, 'r') as input:
        data = yaml.safe_load(input)

    actual_name = name
    i = 1
    while actual_name in data or os.path.exists(os.path.join(basedir, f'{actual_name}.json')):
        # Solve naming conflicts
        i += 1
        actual_name = f'{name}_{i}'

    data[actual_name] = {
        'workflow': f'{actual_name}.json'
    }
    if display_name:
        data[actual_name]['display_name'] = display_name
    if category:
        data[actual_name]['category'] = category

    with open(os.path.join(basedir, f'{actual_name}.json'), 'w') as output:
        output.write(prompt)

    with open(os.path.join(basedir, CONFIG_FILE), 'w') as output:
        yaml.safe_dump(data, output, sort_keys=False)

    current_config = CONFIG_FILE
    create_integrated_node(actual_name, data[actual_name])
    GLOBAL_NODE_CLASS_MAPPINGS[actual_name] = NODE_CLASS_MAPPINGS[actual_name]
    GLOBAL_NODE_DISPLAY_NAME_MAPPINGS[actual_name] = NODE_DISPLAY_NAME_MAPPINGS[actual_name]

    return web.Response(body=actual_name, content_type='text/plain')


load_config()
PromptServer.instance.routes.post('/integrated_nodes/add')(add_node)
