#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-review/B1_4_om080jogani_20260928T190246288349Z_B1.4
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id ef052e01-2f86-49f9-9750-73bab71b9590 --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_4_om080jogani_20260928T190246288349Z/review/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_4_om080jogani_20260928T190246288349Z/review/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_4_om080jogani_20260928T190246288349Z/review/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_4_om080jogani_20260928T190246288349Z/review/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_4_om080jogani_20260928T190246288349Z/review/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
