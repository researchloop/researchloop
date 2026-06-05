#!/bin/bash
# Mock claude CLI for integration tests.
# Accepts the same flags as the real claude CLI but just emits
# minimal valid stream-json output and exits immediately.

PROMPT=""
OUTPUT_FORMAT="text"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p) PROMPT="$2"; shift 2;;
        --output-format) OUTPUT_FORMAT="$2"; shift 2;;
        --resume) shift 2;;
        --dangerously-skip-permissions) shift;;
        --allowedTools) shift; while [[ $# -gt 0 && ! "$1" =~ ^- ]]; do shift; done;;
        --disallowedTools) shift; while [[ $# -gt 0 && ! "$1" =~ ^- ]]; do shift; done;;
        *) shift;;
    esac
done

SESSION_ID="mock-session-$(date +%s)-$$"

if [ "$OUTPUT_FORMAT" = "stream-json" ]; then
    # Emit streaming JSON events that the job template's parser expects.
    # 1. An assistant event with a tool_use (so it shows in the log)
    echo '{"type":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"echo mock test running"}}]}'
    # 2. An assistant event with text output
    echo '{"type":"assistant","content":[{"type":"text","text":"Mock research completed successfully."}]}'
    # 3. A result event with session_id
    echo "{\"type\":\"result\",\"session_id\":\"$SESSION_ID\",\"result\":\"Mock sprint step completed.\"}"
elif [ "$OUTPUT_FORMAT" = "json" ]; then
    echo "{\"session_id\":\"$SESSION_ID\",\"result\":\"Mock sprint step completed.\"}"
else
    echo "Mock sprint step completed."
fi

# Write output files to the current working directory.
# The job script cd's to SPRINT_DIR before invoking claude.
echo "Mock summary: integration test sprint completed successfully." > summary.txt 2>/dev/null || true
echo -e "# Findings\n\nMock findings from integration test." > findings.md 2>/dev/null || true
echo -e "## Log\n\n[$(date -u +%H:%M:%S)] Mock step completed" > progress.md 2>/dev/null || true

exit 0
