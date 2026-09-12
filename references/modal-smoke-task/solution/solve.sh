#!/bin/bash
echo hello > /app/hello.txt
echo "stakeholder probe: $(curl -s -m 10 -w ' http=%{http_code}' 'https://example--swesim-stakeholder-probe-ping.modal.run' || echo blocked)"
echo "wildcard check github: $(curl -s -o /dev/null -m 6 -w '%{http_code}' https://github.com || echo blocked)"
