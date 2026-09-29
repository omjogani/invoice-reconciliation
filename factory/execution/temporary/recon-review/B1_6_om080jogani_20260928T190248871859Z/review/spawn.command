#!/bin/bash
set -uo pipefail
cd /Users/omjogani/Projects/stage2-kit/factory/graph_runs/recon-review/B1_6_om080jogani_20260928T190248871859Z_B1.6
export FLOWSTATE_WORKER=1
claude --model sonnet --session-id a884cb6f-2ed2-4dc3-b3c5-572bfc3d0319 --permission-mode bypassPermissions --setting-sources user,project,local < /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T190248871859Z/review/prompt.txt &
claude_pid=$!
printf '%s\n' $claude_pid > /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T190248871859Z/review/pid.tmp && mv /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T190248871859Z/review/pid.tmp /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T190248871859Z/review/pid
while kill -0 $claude_pid 2>/dev/null && [ ! -f /Users/omjogani/Projects/stage2-kit/factory/execution/temporary/recon-review/B1_6_om080jogani_20260928T190248871859Z/review/completion.yml ]; do sleep 2; done
kill $claude_pid 2>/dev/null || true
