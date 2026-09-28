#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-compile-rates/B1_2_om080jogani_20260928T183535557157Z_B1.2
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id 2d262598-6893-460d-9e3d-a545bb5bb3e6 --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_2_om080jogani_20260928T183535557157Z/extract_a/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_2_om080jogani_20260928T183535557157Z/extract_a/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_2_om080jogani_20260928T183535557157Z/extract_a/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_2_om080jogani_20260928T183535557157Z/extract_a/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_2_om080jogani_20260928T183535557157Z/extract_a/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
