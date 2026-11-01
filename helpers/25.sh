#!/bin/bash
# tmux-25.sh - 2x5 interactive shell grid with broadcast/individual mode
# Usage: ./25.sh
# Creates dist_1..dist_10 with AGENTS.md and deploy/ at each working root.
# Controls:
#   F2 - Toggle between BROADCAST and INDIVIDUAL mode
#   F3 - Focus a pane in INDIVIDUAL mode, then restore the previous mode
#   Left click - Select a pane and forward mouse events to the application
#   Right click - No action
#   In BROADCAST mode, all keystrokes go to all 10 panes simultaneously
#   In INDIVIDUAL mode, keystrokes go only to the active pane

if [ "$#" -ne 0 ]; then
    echo "Usage: ./25.sh" >&2
    exit 1
fi

# Use a separate server: mouse bindings and clipboard options are server-wide.
TMUX_CMD=(tmux -L ctf-codex-drag)

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROBLEM_NAME="$(basename -- "$SCRIPT_DIR")"
DIR_PREFIX="$SCRIPT_DIR/dist"
SOURCE_CHALL="$SCRIPT_DIR/chall"
SOURCE_AGENTS="$SOURCE_CHALL/AGENTS.md"
SESSION_NAME="${PROBLEM_NAME//[^[:alnum:]_-]/_}"
[ -z "$SESSION_NAME" ] && SESSION_NAME="grid10"
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
for i in $(seq 1 10); do
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

# Create new session (pane 0)
"${TMUX_CMD[@]}" new-session -d -s "$SESSION_NAME"

# Create 5 columns from left to right
declare -a TOP_PANES BOTTOM_PANES
TOP_PANES[0]="$("${TMUX_CMD[@]}" display-message -p -t "$SESSION_NAME:0.0" '#{pane_id}')"
current_pane="${TOP_PANES[0]}"

for size in 80% 75% 67% 50%; do
    current_pane="$("${TMUX_CMD[@]}" split-window -P -F '#{pane_id}' -t "$current_pane" -h -l "$size")"
    TOP_PANES+=("$current_pane")
done

# Split each column vertically to create 2 rows
for pane_id in "${TOP_PANES[@]}"; do
    BOTTOM_PANES+=("$("${TMUX_CMD[@]}" split-window -P -F '#{pane_id}' -t "$pane_id" -v -l 50%)")
done

# cd into the corresponding worker directory.
for i in $(seq 0 4); do
    top_dir_num=$((i + 1))
    bottom_dir_num=$((i + 6))
    "${TMUX_CMD[@]}" send-keys -t "${TOP_PANES[$i]}" "cd \"${DIR_PREFIX}_${top_dir_num}\"" C-m
    "${TMUX_CMD[@]}" send-keys -t "${BOTTOM_PANES[$i]}" "cd \"${DIR_PREFIX}_${bottom_dir_num}\"" C-m
done

# Mouse controls:
#   - A plain left click selects a pane and forwards the press.
#   - Left drag and release are forwarded to Codex, without tmux copy mode.
#   - A plain right click is intentionally ignored.
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

# Status bar with mode indicator
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" status-left-length 60
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" status-left \
    '#[fg=white,bg=#{?synchronize-panes,red,green},bold] #{?synchronize-panes, BROADCAST , INDIVIDUAL } #[default] '
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" status-right \
    '#[fg=yellow,bold] F2: Mode  F3: Focus  LMB: Select #[default]'
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" status-style 'bg=black'

F2_TOGGLE_COMMAND='set-option -w synchronize-panes'
FOCUS_ENTER_COMMAND='set-option -wF @sync_before_focus "#{?synchronize-panes,on,off}" ; set-option -w synchronize-panes off ; resize-pane -Z'
FOCUS_EXIT_COMMAND='resize-pane -Z ; if-shell -F "#{==:#{@sync_before_focus},on}" "set-option -w synchronize-panes on" "set-option -w synchronize-panes off" ; set-option -wu @sync_before_focus'
"${TMUX_CMD[@]}" bind-key -n F2 if-shell -F '#{window_zoomed_flag}' \
    'run-shell true' "$F2_TOGGLE_COMMAND"
"${TMUX_CMD[@]}" bind-key -n F3 if-shell -F '#{window_zoomed_flag}' \
    "$FOCUS_EXIT_COMMAND" "$FOCUS_ENTER_COMMAND"

# Start in BROADCAST mode
"${TMUX_CMD[@]}" set-option -t "$SESSION_NAME" synchronize-panes on

# Select first pane
"${TMUX_CMD[@]}" select-pane -t "${TOP_PANES[0]}"

echo "Attaching to tmux session '$SESSION_NAME'..."
echo "  F2     : Toggle BROADCAST / INDIVIDUAL mode"
echo "  F3     : Focus pane; lock INDIVIDUAL; restore mode on exit"
echo "  LMB    : Select pane"
echo "  L-drag : Codex native selection/copy (mouse reporting required)"
echo "  Ctrl+B : tmux prefix (for pane navigation etc.)"
echo "  Startup: cd into worker directories -> BROADCAST"
echo ""

# Attach
exec env -u TMUX "${TMUX_CMD[@]}" attach-session -t "$SESSION_NAME"
