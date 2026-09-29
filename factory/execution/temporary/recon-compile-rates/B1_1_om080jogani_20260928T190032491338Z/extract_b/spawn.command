#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z_B1.1
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id e6cebd9d-5200-4a2e-8b24-5d797e0baed3 --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_b/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_b/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_b/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_b/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_b/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
