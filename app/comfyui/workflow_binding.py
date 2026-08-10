import copy

def validate_workflow(workflow: dict):
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError("workflow must be a non-empty mapping")
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or not node.get("class_type") or not isinstance(node.get("inputs"), dict):
            raise ValueError(f"invalid node {node_id}")
    return True

def bind_workflow(workflow: dict, bindings: dict):
    bound = copy.deepcopy(workflow)
    for node in bound.values():
        for key, value in list(node["inputs"].items()):
            if isinstance(value, str) and value in bindings:
                node["inputs"][key] = bindings[value]
            elif key in bindings:
                node["inputs"][key] = bindings[key]
    validate_workflow(bound)
    return bound
