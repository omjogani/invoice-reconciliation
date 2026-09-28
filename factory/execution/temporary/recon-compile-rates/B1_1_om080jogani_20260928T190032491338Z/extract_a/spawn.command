#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z_B1.1
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id 4a8e8a41-3cf9-48d8-965a-aae39e3d8484 --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_a/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_a/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_a/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_a/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_1_om080jogani_20260928T190032491338Z/extract_a/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
