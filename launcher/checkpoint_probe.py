"""Read saved engine snapshots with the pinned candidate core, without starting a host."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path


def import_is_pure(core_source: Path) -> None:
    """Reject a candidate decoder with executable module setup beyond its upcaster registry."""
    tree = ast.parse(core_source.read_text(encoding='utf-8'))
    if not any(isinstance(node, ast.ImportFrom) and node.module == '__future__'
               and any(alias.name == 'annotations' for alias in node.names) for node in tree.body):
        raise ValueError('the candidate decoder evaluates annotations while importing')
    allowed_imports = {'__future__', 'collections.abc', 'hashlib', 'typing'}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.module not in allowed_imports:
                raise ValueError('the candidate decoder imports executable application code')
        elif isinstance(node, ast.Import):
            if any(alias.name not in allowed_imports for alias in node.names):
                raise ValueError('the candidate decoder imports executable application code')
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if node.decorator_list or any(isinstance(part, ast.Call) for part in (
                    node.bases if isinstance(node, ast.ClassDef) else node.args.defaults)):
                raise ValueError('the candidate decoder has executable module decorators')
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if any(not isinstance(decorator, ast.Name) or decorator.id != 'property'
                               for decorator in member.decorator_list) or any(
                                   isinstance(default, ast.Call) for default in member.args.defaults):
                            raise ValueError('the candidate decoder has executable class decorators')
                    elif not (isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant)
                              and isinstance(member.value.value, str)):
                        raise ValueError('the candidate decoder has executable class setup')
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            calls = [part for part in ast.walk(value) if isinstance(part, ast.Call)] if value else []
            if calls and not (len(calls) == 1 and isinstance(value, ast.Call)
                              and isinstance(value.func, ast.Name) and value.func.id == 'UpcasterRegistry'):
                raise ValueError('the candidate decoder has executable module assignments')
        elif isinstance(node, ast.Expr):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                continue
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                    and isinstance(value.func.value, ast.Name) and value.func.value.id == 'UPCASTERS'
                    and value.func.attr == 'register' and len(value.args) == 2
                    and isinstance(value.args[0], ast.Constant) and isinstance(value.args[1], ast.Name)):
                continue
            raise ValueError('the candidate decoder has executable module statements')
        else:
            raise ValueError('the candidate decoder has unsupported module setup')


def verify(core_source: Path, entries: list[dict]) -> None:
    import_is_pure(core_source)
    spec = importlib.util.spec_from_file_location('candidate_snapshot_contract', core_source)
    if spec is None or spec.loader is None:
        raise ValueError('the candidate snapshot decoder is unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for entry in entries:
        saved = module.migrate_snapshot(json.loads(entry['snapshot']))
        if (saved.get('run_id') != entry['run_id'] or saved.get('session_id') != entry['session_id']
                or saved.get('tenant_id') != entry['tenant_id'] or not saved.get('model_name')
                or saved.get('background_task_ids')):
            raise ValueError('the candidate cannot restore the checkpoint identity or side effects')
        intents = saved.get('open_intents') or []
        if (not isinstance(intents, list) or any(not isinstance(intent, dict)
                or intent.get('state') == 'DISPATCHED' or intent.get('outcome') == 'unknown'
                for intent in intents)):
            raise ValueError('the candidate cannot confirm the checkpoint tool outcomes')


if __name__ == '__main__':
    try:
        verify(Path(sys.argv[1]), json.load(sys.stdin))
    except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
