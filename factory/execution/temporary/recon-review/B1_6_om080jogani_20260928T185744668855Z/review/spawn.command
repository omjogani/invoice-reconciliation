#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-review/B1_6_om080jogani_20260928T185744668855Z_B1.6
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id 5ea8070c-c5d0-463a-8bf8-e3cad4d99b00 --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T185744668855Z/review/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T185744668855Z/review/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T185744668855Z/review/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T185744668855Z/review/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T185744668855Z/review/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
