"""The hosted spend surface distinguishes known totals from missing usage/prices."""
from swesim import costs
from swesim.paths import atomic_json
import test_dashboard

site = test_dashboard.site


def test_spend_handles_unpriced_workflow_rows_and_reports_incomplete_usage(site):
    ws = site['ws']
    costs.record_cost(ws, site['done'], 'workflow-unknown', 'audit', 'anthropic/unpriced-model',
                      costs.Usage(input_tokens=100, output_tokens=10), idempotency_key='unpriced')
    directory = ws.root / 'trials' / site['done']
    atomic_json(directory / 'audit-usage' / 'missing.json', {'usage': None})
    response = site['client'].get('/spend')
    assert response.status_code == 200 and 'Known spend only' in response.text
    body = site['client'].get('/api/spend').json()
    assert body['accounting']['unpriced_records'] == 1
    assert body['accounting']['unknown_usage_count'] == 1 and body['accounting']['spend_complete'] is False
