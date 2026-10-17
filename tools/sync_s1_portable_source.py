"""Deterministic one-way S1 export from committed upstream source.

Select unrestricted balance branches before writing the portable source tree.
This is source generation, never runtime matching of player/model text.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import runpy
import subprocess
from pathlib import Path

_public_projection = runpy.run_path(str(Path(__file__).with_name('s1_public_source.py')))
project_public_source = _public_projection['project_public_source']
project_ai_calls = runpy.run_path(str(Path(__file__).with_name('s1_ai_call_projection.py')))['project_ai_calls']
project_platform_source = runpy.run_path(str(Path(__file__).with_name('s1_platform_projection.py')))['project_platform_source']
modernize_python = runpy.run_path(str(Path(__file__).with_name('portable_python_compat.py')))['modernize_python']
project_lazy_ui = runpy.run_path(str(Path(__file__).with_name('s1_lazy_ui_projection.py')))['project_lazy_ui']

SCOPES = [
    ':(glob)backend/migrations/*s1*.py', ':(glob)backend/db_repository/s1_*.py',
    'backend/db_repository/postgresql_advisory_lock.py',
    'backend/utils/db_operation_tracker.py', 'backend/utils/ai_field_names.py',
    'backend/services/model_provider_transport.py', 'backend/plugins/adventurer',
    'backend/plugins/adventurer_s1', 'frontend-v2/src/assets/adventurer-s1-sponsor.jpg',
    'frontend-v2/src/plugins/adventurer_s1', 'docs/s1_prompt_authoring_rules.md',
    'docs/adventurer_design.md',
]
POLICY_MODULE = 'plugins.adventurer_s1.services.s1_balance_policy_service'
OMIT = {
    'backend/plugins/adventurer_s1/services/s1_balance_policy_service.py',
    'backend/db_repository/s1_balance_policy_repository.py',
    'frontend-v2/src/plugins/adventurer_s1/components/AdminBalanceSwitch.vue',
    'frontend-v2/src/plugins/adventurer_s1/components/SponsorTipModal.vue',
    'frontend-v2/src/assets/adventurer-s1-sponsor.jpg',
}


class SelectUnrestricted(ast.NodeTransformer):
    @staticmethod
    def constant(value):
        node = ast.Constant(value)
        node._selected_policy = True
        return node

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            if node.func.id == 'balance_rule':
                if len(node.args) != 3:
                    raise ValueError('Unrecognized balance_rule source contract')
                return self.visit(node.args[2])
            if node.func.id == 'render_balance_prompt':
                return self.visit(node.args[0])
            if node.func.id == 'balance_limits_enabled':
                return self.constant(False)
        return self.generic_visit(node)

    def visit_Name(self, node):
        if node.id == 'balance_enabled' and isinstance(node.ctx, ast.Load):
            return self.constant(False)
        return node

    def visit_Expr(self, node):
        if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                and node.value.func.id == 'bind_balance_policy'):
            return None
        return self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module == POLICY_MODULE:
            return None
        return node

    def visit_Dict(self, node):
        pairs = [(k, v) for k, v in zip(node.keys, node.values)
                 if not (isinstance(k, ast.Constant) and k.value in {
                     'balance_limits_enabled', 'ADVENTURER_S1_BALANCE_LIMITS_ENABLED',
                 })]
        node.keys, node.values = [p[0] for p in pairs], [p[1] for p in pairs]
        return self.generic_visit(node)

    def visit_UnaryOp(self, node):
        node = self.generic_visit(node)
        if isinstance(node.op, ast.Not) and getattr(node.operand, '_selected_policy', False):
            return self.constant(not node.operand.value)
        return node

    def visit_BoolOp(self, node):
        node = self.generic_visit(node)
        values = []
        for value in node.values:
            if isinstance(value, ast.Constant) and getattr(value, '_selected_policy', False):
                if isinstance(node.op, ast.And) and not value.value:
                    # All policy checks are side-effect-free. Preserve preceding
                    # operands in other expressions instead of globally folding.
                    return self.constant(False) if not values else ast.BoolOp(op=node.op, values=values+[value])
                if isinstance(node.op, ast.Or) and value.value:
                    return self.constant(True) if not values else ast.BoolOp(op=node.op, values=values+[value])
                continue
            values.append(value)
        if not values:
            return self.constant(isinstance(node.op, ast.And))
        if len(values) == 1:
            return values[0]
        node.values = values
        return node

    def visit_IfExp(self, node):
        node = self.generic_visit(node)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            return node.body if node.test.value else node.orelse
        return node

    def visit_If(self, node):
        # Only this authored administrator-setting branch is retired.
        if any(isinstance(n, ast.Compare) and any(isinstance(c, ast.Constant) and c.value == 'balance_limits_enabled' for c in n.comparators) for n in ast.walk(node.test)):
            return [self.visit(n) for n in node.orelse]
        node = self.generic_visit(node)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            return node.body if node.test.value else node.orelse
        return node

    def visit_Assign(self, node):
        if any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == 'balance_limits_enabled' for t in node.targets):
            return None
        if any(isinstance(t, ast.Name) and t.id == 'balance_enabled' for t in node.targets):
            return None
        return self.generic_visit(node)

    def visit_FunctionDef(self, node):
        node.decorator_list = [d for d in node.decorator_list if not (isinstance(d, ast.Name) and d.id in {'balance_policy_scope', 'appeal_balance_policy_scope'})]
        node = self.generic_visit(node)
        if node.name == '_validate_operation_contract_review_result':
            # Only the authored free-appeal verdict fast path retires the
            # historical-proof block. Do not prune unrelated loop bodies.
            for loop in ast.walk(node):
                if not isinstance(loop, ast.For):
                    continue
                for index, statement in enumerate(loop.body[1:], start=1):
                    previous = loop.body[index - 1]
                    if (isinstance(statement, ast.Continue)
                            and isinstance(previous, ast.Expr)
                            and ast.unparse(previous.value) == 'normalized_verdicts.append(normalized)'):
                        loop.body = loop.body[:index + 1]
                        break
        # An unrestricted fast return makes the old policy implementation dead.
        for index, statement in enumerate(node.body):
            if isinstance(statement, (ast.Return, ast.Raise)):
                node.body = node.body[:index+1]
                break
        return node


def _project_base_source(relative: str, raw: bytes) -> bytes | None:
    if relative in OMIT:
        return None
    raw = project_public_source(relative, raw)
    if relative.endswith('.py'):
        source = raw.decode('utf-8-sig')
        if not any(token in source for token in (
            'balance_rule(', 'render_balance_prompt(', 'balance_limits_enabled',
            'ADVENTURER_S1_BALANCE_LIMITS_ENABLED',
            'balance_policy_scope', 'bind_balance_policy',
        )):
            return raw
        tree = SelectUnrestricted().visit(ast.parse(source))
        ast.fix_missing_locations(tree)
        output = ast.unparse(tree) + '\n'
        ast.parse(output)
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id in {'balance_rule', 'render_balance_prompt', 'balance_limits_enabled'}
               for n in ast.walk(tree)):
            raise ValueError(f'Unresolved policy branch: {relative}')
        return output.encode('utf-8')
    if relative.endswith('/AdminHubPanel.vue'):
        source = raw.decode('utf-8-sig').replace('\r\n', '\n')
        source = source.replace('    <AdminBalanceSwitch />\n', '')
        source = source.replace("import AdminBalanceSwitch from './AdminBalanceSwitch.vue'\n", '')
        return source.encode('utf-8')
    if relative.endswith('/AdminRuntimeSettingsPanel.vue'):
        return raw.replace(b'\r\n', b'\n').replace(b'    delete payload.balance_limits_enabled\n', b'')
    return raw


def project_source(relative: str, raw: bytes) -> bytes | None:
    projected = _project_base_source(relative, raw)
    if projected is None:
        return None
    projected = project_ai_calls(relative, projected)
    projected = project_platform_source(relative, projected, strip_css=_public_projection['strip_css'])
    if projected is None:
        return None
    if relative.endswith('.py'):
        source = projected.decode('utf-8-sig')
        if relative.endswith('/services/adventurer_s1_service.py'):
            # Inline this single-use query so loader options and forced refresh
            # can be carried explicitly into Session.get by the AST projection.
            declaration = '        detail_query = AdventurerS1.query.populate_existing()\n'
            if source.count(declaration) != 1 or source.count('detail_query.options(') != 1:
                raise ValueError('Adventurer detail query source drift')
            source = source.replace(declaration, '').replace('detail_query.options(', 'AdventurerS1.query.populate_existing().options(')
        return modernize_python(source).encode('utf-8')
    return project_lazy_ui(relative, projected)


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(source), *args], text=True, encoding='utf-8').strip()


def committed_blobs(source: Path, paths: list[str]) -> dict[str, bytes]:
    """Read committed bytes in one batch, independent of checkout CRLF rules."""
    requests = ''.join('HEAD:' + relative + '\n' for relative in paths).encode('utf-8')
    response = subprocess.check_output(
        ['git', '-C', str(source), 'cat-file', '--batch'], input=requests,
    )
    stream = io.BytesIO(response)
    blobs = {}
    for relative in paths:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b'blob':
            raise ValueError(f'Invalid committed source object: {relative}')
        size = int(header[2])
        blob = stream.read(size)
        if len(blob) != size or stream.read(1) != b'\n':
            raise ValueError(f'Incomplete committed source object: {relative}')
        blobs[relative] = blob
    return blobs


def safe_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    target.relative_to(root.resolve())
    return target


def sync(source: Path, target: Path, *, check: bool) -> None:
    dirty = git(source, 'status', '--porcelain=v1', '--untracked-files=all', '--', *SCOPES)
    if dirty:
        raise ValueError('Refusing uncommitted upstream S1 scope:\n' + dirty)
    paths = git(source, 'ls-files', '--', *SCOPES).splitlines()
    blobs = committed_blobs(source, paths)
    commit = git(source, 'rev-parse', 'HEAD')
    snapshot = target / 'game-packs/s1-fantasy/source-snapshot'
    expected = {}
    provenance = {}
    for relative in paths:
        raw = blobs[relative]
        projected = project_source(relative, raw)
        provenance[relative] = {
            'upstream_sha256': hashlib.sha256(raw).hexdigest(),
            'portable_sha256': hashlib.sha256(projected).hexdigest() if projected is not None else None,
        }
        if relative.endswith('.py') and projected is not None:
            provenance[relative]['unchanged_except_python_compat'] = (
                modernize_python(raw.decode('utf-8-sig')).encode('utf-8') == projected
            )
        if projected is not None:
            expected[relative] = projected
    metadata = '\n'.join((
        'source_repository=' + git(source, 'remote', 'get-url', 'origin'),
        'source_commit=' + commit,
        'source_branch=' + git(source, 'branch', '--show-current'),
        'source_projection=public-self-hosted-v6',
        f'upstream_file_count={len(paths)}',
        f'tracked_file_count={len(expected)}',
    )) + '\n'
    manifest = '\n'.join(expected) + '\n'
    files = {**expected, 'SOURCE': metadata.encode(), 'TRACKED_FILES': manifest.encode(),
             'PROJECTION.json': (json.dumps(provenance, ensure_ascii=False, indent=2)+'\n').encode()}
    old_manifest = snapshot / 'TRACKED_FILES'
    previous = set(old_manifest.read_text(encoding='utf-8-sig').splitlines()) if old_manifest.exists() else set()
    drift = []
    for relative, data in files.items():
        destinations = [safe_path(snapshot, relative)]
        if relative.startswith('frontend-v2/'):
            destinations.append(safe_path(target, relative))
        for destination in destinations:
            if destination.exists() and destination.read_bytes() == data:
                continue
            drift.append(str(destination.relative_to(target)))
            if not check:
                destination.parent.mkdir(parents=True, exist_ok=True)
                staged = destination.with_name(destination.name + '.portable-sync')
                staged.write_bytes(data)
                staged.replace(destination)
    for relative in previous - expected.keys():
        destinations = [safe_path(snapshot, relative)]
        if relative.startswith('frontend-v2/'):
            destinations.append(safe_path(target, relative))
        for destination in destinations:
            if destination.exists():
                drift.append('removed:' + str(destination.relative_to(target)))
                if not check:
                    destination.unlink()
    if check and drift:
        raise ValueError('Portable S1 drift:\n'+'\n'.join(drift))
    print(f'{"Verified" if check else "Exported"} {len(expected)} portable S1 files from {commit}; original balance branches removed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    sync(args.source.resolve(), args.target.resolve(), check=args.check)
