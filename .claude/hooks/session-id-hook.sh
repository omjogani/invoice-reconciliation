#!/usr/bin/env bash
# Extracts session_id from the SessionStart hook stdin JSON
# and injects it into conversation context as a system reminder.
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')

if [ -z "$SESSION_ID" ]; then
  exit 0
fi

jq -n --arg ctx "CLAUDE_CODE_SESSION_ID=$SESSION_ID" \
    '{ hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: $ctx } }'
