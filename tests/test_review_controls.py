"""Fallback controls must change executable Python with complete base evidence."""
from swesim import tamper
from swesim.stages import draft_spec


def test_fallback_uses_string_state_before_and_between_hunks():
    base = 'def f():\n    """Documentation.\n' + ''.join(f'    line {i}\n' for i in range(20)) + '    """\n    return True\n'
    gold = base.replace('line 2', 'values not in the set').replace('line 17', 'values == other values')
    patch = tamper.file_diff('pkg/a.py', base, gold)
    assert patch.count('@@ -') == 2
    assert draft_spec.fallback_plausible_wrong(patch, {'pkg/a.py': base}) is None


def test_fallback_changes_executable_ast_and_keeps_docstrings():
    base = 'def f():\n    """Documentation.\n' + ''.join(f'    line {i}\n' for i in range(12)) + '    """\n    return True\n'
    gold = base.replace('line 2', 'values not in the set').replace('return True', 'return False')
    patch = tamper.file_diff('pkg/a.py', base, gold)
    result = draft_spec.fallback_plausible_wrong(patch, {'pkg/a.py': base})
    assert result is not None and result[2] is True
    assert '+    values not in the set' in result[0]
    assert '+    values in the set' not in result[0]
    assert '+    return True' in result[0]


def test_fallback_rejects_unfetched_base_and_invalid_python():
    patch = tamper.file_diff('pkg/a.py', 'x = 0\n', 'x = True\ny = 2\n')
    assert draft_spec.fallback_plausible_wrong(patch, {}) is None
    broken = tamper.file_diff('pkg/a.py', 'x = 0\n', 'if True:\n')
    assert draft_spec.fallback_plausible_wrong(broken, {'pkg/a.py': 'x = 0\n'}) is None
