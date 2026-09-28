#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-compile-rates/B1_3_om080jogani_20260928T183535881364Z_B1.3
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id 6b055df1-f71f-4c25-bdb0-526f149246e6 --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_3_om080jogani_20260928T183535881364Z/revise_b/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_3_om080jogani_20260928T183535881364Z/revise_b/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_3_om080jogani_20260928T183535881364Z/revise_b/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_3_om080jogani_20260928T183535881364Z/revise_b/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-compile-rates/B1_3_om080jogani_20260928T183535881364Z/revise_b/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
