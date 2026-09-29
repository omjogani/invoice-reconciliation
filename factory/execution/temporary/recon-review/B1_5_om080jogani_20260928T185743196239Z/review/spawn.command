#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-review/B1_5_om080jogani_20260928T185743196239Z_B1.5
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id d992c688-5f37-467c-90c5-295c91cc8e07 --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_5_om080jogani_20260928T185743196239Z/review/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_5_om080jogani_20260928T185743196239Z/review/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_5_om080jogani_20260928T185743196239Z/review/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_5_om080jogani_20260928T185743196239Z/review/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_5_om080jogani_20260928T185743196239Z/review/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
