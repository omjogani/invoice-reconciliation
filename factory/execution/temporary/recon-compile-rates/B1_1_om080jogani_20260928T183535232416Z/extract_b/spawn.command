#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-compile-rates/B1_1_om080jogani_20260928T183535232416Z_B1.1
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id 0e5f03a6-ff43-4424-9135-d1dcac8061af --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T183535232416Z/extract_b/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T183535232416Z/extract_b/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T183535232416Z/extract_b/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T183535232416Z/extract_b/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T183535232416Z/extract_b/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
