#!/bin/bash
# 2x2 interactive shell grid with broadcast/individual mode
# Usage: ./22.sh
# Creates dist_1..dist_4 with AGENTS.md and deploy/ at each working root.
# Controls:
#   F2 - Toggle between BROADCAST and INDIVIDUAL mode
#   F3 - Focus a pane in INDIVIDUAL mode, then restore the previous mode
#   Left click - Select a pane and forward mouse events to the application
#   Right click - No action

if [ "$#" -ne 0 ]; then
    echo "Usage: ./22.sh" >&2
    exit 1
fi

# Use a separate server: mouse bindings and clipboard options are server-wide.
TMUX_CMD=(tmux -L ctf-codex-drag)

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROBLEM_NAME="$(basename -- "$SCRIPT_DIR")"
DIR_PREFIX="$SCRIPT_DIR/dist"
SOURCE_CHALL="$SCRIPT_DIR/chall"
SOURCE_AGENTS="$SOURCE_CHALL/AGENTS.md"
SESSION_BASE="${PROBLEM_NAME//[^[:alnum:]_-]/_}"
[ -z "$SESSION_BASE" ] && SESSION_BASE="grid4"
SESSION_NAME="${SESSION_BASE}_4"
if [ ! -d "$SOURCE_CHALL" ]; then
    echo "Error: $SOURCE_CHALL does not exist." >&2
    exit 1
fi

if [ ! -f "$SOURCE_AGENTS" ]; then
    echo "Error: $SOURCE_AGENTS does not exist." >&2
    exit 1
fi

# Reattach without recreating panes or touching in-progress worker directories.
if "${TMUX_CMD[@]}" has-session -t "=$SESSION_NAME" 2>/dev/null; then
    echo "Attaching to existing tmux session '$SESSION_NAME'..."
    exec env -u TMUX "${TMUX_CMD[@]}" attach-session -t "=$SESSION_NAME"
fi

# Create worker directories with the contents of chall/ at each worker directory.
for i in $(seq 1 4); do
    target_dir="${DIR_PREFIX}_${i}"
    mkdir -p "$target_dir"
    if [ -f "$target_dir/chall/AGENTS.md" ]; then
        rm -- "$target_dir/chall/AGENTS.md"
    fi
    if [ -d "$target_dir/chall/deploy" ] \
        && diff -qr "$SOURCE_CHALL/deploy" "$target_dir/chall/deploy" >/dev/null 2>&1 \
        && ! find "$target_dir/chall" -mindepth 1 -maxdepth 1 ! -name deploy -print -quit | grep -q .; then
        rm -r -- "$target_dir/chall"
    fi
    if [ ! -d "$target_dir/deploy" ]; then
        cp -a "$SOURCE_CHALL/." "$target_dir/"
    fi
    cp -p "$SOURCE_AGENTS" "$target_dir/AGENTS.md"
done

# Build a 2x2 grid and keep explicit pane IDs in visual order.
"${TMUX_CMD[@]}" new-session -d -s "$SESSION_NAME"
top_left="$("${TMUX_CMD[@]}" display-message -p -t "$SESSION_NAME:0.0" '#{pane_id}')"
bottom_left="$("${TMUX_CMD[@]}" split-window -P -F '#{pane_id}' -t "$top_left" -v -l 50%)"
top_right="$("${TMUX_CMD[@]}" split-window -P -F '#{pane_id}' -t "$top_left" -h -l 50%)"
bottom_right="$("${TMUX_CMD[@]}" split-window -P -F '#{pane_id}' -t "$bottom_left" -h -l 50%)"
PANES=("$top_left" "$top_right" "$bottom_left" "$bottom_right")

for index in "${!PANES[@]}"; do
    worker=$((index + 1))
    "${TMUX_CMD[@]}" send-keys -t "${PANES[$index]}" "cd \"${DIR_PREFIX}_${worker}\"" C-m
done

# Mouse and keyboard behavior.
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" mouse on
"${TMUX_CMD[@]}" set-option -s set-clipboard on
"${TMUX_CMD[@]}" set-option -s escape-time 0
# Keep press, motion, and release together so Codex owns the selection.
# No tmux copy-mode fallback: applications must enable mouse reporting.
"${TMUX_CMD[@]}" bind-key -T root MouseDown1Pane 'select-pane -t = ; send-keys -M -t ='
for mouse_event in MouseDrag1Pane MouseUp1Pane MouseDragEnd1Pane DoubleClick1Pane TripleClick1Pane WheelUpPane WheelDownPane; do
    "${TMUX_CMD[@]}" bind-key -T root "$mouse_event" send-keys -M -t =
done
"${TMUX_CMD[@]}" bind-key -T root MouseDown3Pane run-shell -b true
F2_TOGGLE_COMMAND='set-option -w synchronize-panes'
FOCUS_ENTER_COMMAND='set-option -wF @sync_before_focus "#{?synchronize-panes,on,off}" ; set-option -w synchronize-panes off ; resize-pane -Z'
FOCUS_EXIT_COMMAND='resize-pane -Z ; if-shell -F "#{==:#{@sync_before_focus},on}" "set-option -w synchronize-panes on" "set-option -w synchronize-panes off" ; set-option -wu @sync_before_focus'
"${TMUX_CMD[@]}" bind-key -n F2 if-shell -F '#{window_zoomed_flag}' \
    'run-shell true' "$F2_TOGGLE_COMMAND"
"${TMUX_CMD[@]}" bind-key -n F3 if-shell -F '#{window_zoomed_flag}' \
    "$FOCUS_EXIT_COMMAND" "$FOCUS_ENTER_COMMAND"

"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" status-left-length 60
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" status-left \
    '#[fg=white,bg=#{?synchronize-panes,red,green},bold] #{?synchronize-panes, BROADCAST , INDIVIDUAL } #[default] '
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" status-right \
    '#[fg=yellow,bold] F2: Mode  F3: Focus  LMB: Select #[default]'
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" status-style 'bg=black'
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" synchronize-panes on
"${TMUX_CMD[@]}" select-pane -t "$top_left"

echo "Attaching to tmux session '$SESSION_NAME'..."
echo "  F2     : Toggle BROADCAST / INDIVIDUAL mode"
echo "  F3     : Focus pane; lock INDIVIDUAL; restore mode on exit"
echo "  LMB    : Select pane"
echo "  L-drag : Codex native selection/copy (mouse reporting required)"
echo "  Ctrl+B : tmux prefix"
echo "  Startup: cd into worker directories -> BROADCAST"
echo ""

exec env -u TMUX "${TMUX_CMD[@]}" attach-session -t "$SESSION_NAME"
