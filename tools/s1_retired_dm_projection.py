"""Export-time removal of retired per-member instructions; no runtime hook."""
import ast

_FIELDS = frozenset({"dm_instruction", "dm_instruction_revision"})
_RULES = frozenset({"_S1_DM_INSTRUCTION_TARGETING_ADDENDUM", "_OBSERVER_DM_INSTRUCTION_RULES"})
_REVIEW_TYPE = "TARGETED_SPECIALIZED_REVIEW_DM_INSTRUCTION"
_MODULES = frozenset({
    "plugins.adventurer.models",
    "plugins.adventurer.services.adventurer_prompt_service",
    "plugins.adventurer_s1.models",
    "plugins.adventurer_s1.routes",
    *(
        "plugins.adventurer_s1.services." + name
        for name in (
            "adventurer_s1_prompt_service", "adventurer_s1_expedition_runner",
            "adventurer_s1_expedition_observer_ai_service",
            "adventurer_s1_service", "adventurer_s1_matchmaking_service",
            "adventurer_s1_tavern_service",
        )
    ),
})


def _field(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value
    if isinstance(node, ast.Constant):
        return node.value
    return None


class _RetireDmFields(ast.NodeTransformer):
    def __init__(self):
        self.changes = 0

    def visit_Assign(self, node):
        if (
            len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "dm_instruction_revision"
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "Column"
        ):
            # Existing tables require this NOT NULL column on inserts. Retain
            # its zero default under a private compatibility name, without
            # exposing a DM field or reading any historical instruction text.
            node.targets[0].id = "_retired_dm_instruction_revision"
            node.value.args.insert(0, ast.Constant("dm_instruction_revision"))
            self.changes += 1
            return self.generic_visit(node)
        if any(_field(target) in _FIELDS for target in node.targets):
            self.changes += 1
            return None
        if any(_field(target) in _RULES for target in node.targets):
            node.value = ast.copy_location(ast.Constant(""), node.value)
            self.changes += 1
        return self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if _field(node.target) in _FIELDS:
            self.changes += 1
            return None
        return self.generic_visit(node)

    def visit_Dict(self, node):
        pairs = [(key, value) for key, value in zip(node.keys, node.values) if _field(key) not in _FIELDS]
        self.changes += len(node.keys) - len(pairs)
        node.keys = [key for key, _ in pairs]
        node.values = [value for _, value in pairs]
        return self.generic_visit(node)

    def _collection(self, node):
        items = [item for item in node.elts if _field(item) not in _FIELDS | _RULES | {_REVIEW_TYPE}]
        self.changes += len(node.elts) - len(items)
        node.elts = items
        return self.generic_visit(node)

    visit_Set = _collection
    visit_List = _collection
    visit_Tuple = _collection

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Name) and node.func.id == "defer"
            and len(node.args) == 1 and _field(node.args[0]) in _FIELDS
        ):
            self.changes += 1
            return None
        keywords = [keyword for keyword in node.keywords if keyword.arg not in _FIELDS]
        self.changes += len(node.keywords) - len(keywords)
        node.keywords = keywords
        return self.generic_visit(node)

    def visit_If(self, node):
        test = node.test
        dm_read = (
            isinstance(test, ast.Call) and isinstance(test.func, ast.Name)
            and test.func.id == "getattr" and len(test.args) >= 2
            and _field(test.args[1]) in _FIELDS
        )
        legacy_dm_read = (
            isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And)
            and any(isinstance(part, ast.Attribute) and part.attr in _FIELDS
                    for part in ast.walk(test))
        ) or (
            isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute)
            and test.func.attr == 'get' and test.args and _field(test.args[0]) in _FIELDS
        )
        dm_write = (
            isinstance(test, ast.Compare) and _field(test.left) in _FIELDS
            and any(isinstance(op, ast.In) for op in test.ops)
        )
        dm_review = (
            isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id == _REVIEW_TYPE
        )
        if dm_read or legacy_dm_read or dm_write or dm_review:
            self.changes += 1
            return None
        return self.generic_visit(node)

    def visit_FunctionDef(self, node):
        if node.name in {"_s1_has_dm_instruction", "_project_targeted_dm_instruction_members"}:
            value = ast.Constant(False) if node.name == "_s1_has_dm_instruction" else ast.List(elts=[], ctx=ast.Load())
            node.body = [ast.copy_location(ast.Return(value=value), node)]
            self.changes += 1
            return node
        node = self.generic_visit(node)
        if node.name == "_restore_s1_member_generation_input":
            for statement in node.body:
                if isinstance(statement, ast.For) and isinstance(statement.target, ast.Name) and statement.target.id == "row":
                    # Remove only these retired fields from the cloned legacy
                    # input before its original exact shape/identity checks.
                    for field in sorted(_FIELDS):
                        statement.body.insert(0, ast.copy_location(ast.Expr(value=ast.Call(
                            func=ast.Attribute(value=ast.Name(id="row", ctx=ast.Load()), attr="pop", ctx=ast.Load()),
                            args=[ast.Constant(field), ast.Constant(None)], keywords=[],
                        )), statement))
                    self.changes += 1
        return node

