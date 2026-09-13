"""pytest evidence parsers: -rA terminal summary, JUnit XML, and their reconciliation.

Standard library only: this file is copied into the verifier container.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET


class EvidenceError(ValueError):
    pass


_STATES = {'PASSED': 'passed', 'FAILED': 'failed', 'ERROR': 'error', 'XFAIL': 'xfail', 'XPASS': 'xpass'}
_RA_LINE = re.compile(r'^(PASSED|FAILED|ERROR|XFAIL|XPASS) (.+)$')
_SUMMARY_LINE = re.compile(r'^=+ .* in [0-9.]+')
_COUNT = re.compile(r'(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)\b')


def junit_key(nodeid):
    """(classname, name) exactly as pytest's junitxml mangles a nodeid."""
    head, bracket, params = nodeid.partition('[')
    names = head.split('::')
    if len(names) < 2 or not names[0].endswith('.py'):
        raise EvidenceError('unsupported nodeid: ' + nodeid)
    names[0] = names[0][:-3].replace('/', '.')
    names[-1] += bracket + params
    return ('.'.join(names[:-1]), names[-1])


def parse_junit(xml_path, expected=None):
    try:
        root = ET.parse(str(xml_path)).getroot()
    except (ET.ParseError, OSError) as e:
        raise EvidenceError('unreadable JUnit report: %s' % e)
    inverse = {}
    if expected is not None:
        for nodeid in expected:
            key = junit_key(nodeid)
            if key in inverse:
                raise EvidenceError('ambiguous JUnit mapping: ' + nodeid)
            inverse[key] = nodeid
    out = {}
    for tc in root.iter('testcase'):
        key = (tc.get('classname', ''), tc.get('name', ''))
        if expected is None:
            # Convenience path for plain functions only. Grading always supplies a manifest.
            tid = key[0].replace('.', '/') + '.py::' + key[1]
        elif key in inverse:
            tid = inverse[key]
        else:
            raise EvidenceError('unexpected JUnit testcase: ' + repr(key))
        if tid in out:
            raise EvidenceError('duplicate JUnit id: ' + tid)
        children = {c.tag for c in tc}
        out[tid] = ('error' if 'error' in children else 'failed' if 'failure' in children
                    else 'skipped' if 'skipped' in children else 'passed')
    if expected is not None and set(out) != set(expected):
        raise EvidenceError('incomplete JUnit report')
    return out


def parse_pytest_ra(stdout, expected=None):
    """Statuses from -rA short-summary lines. Whole nodeids are matched, so parameter ids may contain spaces."""
    out = {}
    nodeids = sorted(expected, key=len, reverse=True) if expected is not None else None
    for raw in stdout.splitlines():
        m = _RA_LINE.match(raw.strip())
        if not m:
            continue
        state, tail = m.groups()
        if nodeids is not None:
            matches = [n for n in nodeids if tail == n or tail.startswith(n + ' - ')]
            if not matches:
                continue
            nodeid = matches[0]
        else:
            # Used only for the simple parser fixture; grading obtains exact ids from collection.
            nodeid = tail.split(' - ', 1)[0]
        if nodeid in out:
            raise EvidenceError('duplicate terminal id: ' + nodeid)
        out[nodeid] = _STATES[state]
    return out


def summary_counts(stdout):
    for line in reversed(stdout.splitlines()):
        s = line.strip()
        if _SUMMARY_LINE.match(s):
            return {('error' if k.startswith('error') else k): int(n) for n, k in _COUNT.findall(s)}
    return {}


def reconcile(stdout, xml_path, expected, rc):
    """Require JUnit, terminal summary lines, terminal counts and exit code to agree on exactly `expected`."""
    expected = list(expected)
    j = parse_junit(xml_path, expected)
    ra = parse_pytest_ra(stdout, expected)
    counts = summary_counts(stdout)
    if not counts:
        raise EvidenceError('missing terminal summary')
    # JUnit records xfail as skipped and a non-strict xpass as passed.
    terminal = {'passed': counts.get('passed', 0) + counts.get('xpassed', 0),
                'failed': counts.get('failed', 0),
                'error': counts.get('error', 0),
                'skipped': counts.get('skipped', 0) + counts.get('xfailed', 0)}
    for key, n in terminal.items():
        observed = sum(v == key for v in j.values())
        if observed != n:
            raise EvidenceError('summary disagrees: %s (junit %d, terminal %d)' % (key, observed, n))
    for tid, status in j.items():
        r = ra.get(tid)
        if status in ('passed', 'failed', 'error'):
            if r == status:
                continue
            if status == 'passed' and r == 'xpass':
                j[tid] = 'xpass'
                continue
            raise EvidenceError('terminal/JUnit id disagreement: ' + tid)
        if status == 'skipped':
            if r == 'xfail':
                j[tid] = 'xfail'
            elif r is not None:
                raise EvidenceError('terminal/JUnit id disagreement: ' + tid)
    expected_rc = 1 if any(v in ('failed', 'error') for v in j.values()) else 0
    if rc != expected_rc:
        raise EvidenceError('exit code disagrees: rc=%s, evidence implies %d' % (rc, expected_rc))
    return j
