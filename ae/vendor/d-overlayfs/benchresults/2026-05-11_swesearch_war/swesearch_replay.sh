#!/usr/bin/env bash
# =============================================================================
# swesearch_replay.sh — Replay a swe-search/mcts trace's EditCode sequence on
# overlay-on-{ext4 | xfs | xfs+reflink}, measuring per-edit copy-up and phys I/O.
#
# For each edit:
#   1. record file_size_before (in current overlay merged view)
#   2. apply diff to overlay merged via mmap-friendly partial-write
#   3. layer-switch to new upper (so per-edit copy-up is isolated)
#   4. measure: FIEMAP non-shared bytes in just-closed upper, /sys/block stat delta
#
# Usage: sudo bash swesearch_replay.sh <instance_id> <fstype>
#   fstype: ext4 | xfs | xfs_reflink
# =============================================================================
set -euo pipefail

INSTANCE="${1:?missing instance_id}"
FSTYPE="${2:?missing fstype}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ACTIONS_JSON="${SCRIPT_DIR}/swesearch_actions/${INSTANCE}.json"
[[ -f "$ACTIONS_JSON" ]] || { echo "missing $ACTIONS_JSON"; exit 1; }

WORK_BASE="/tmp/swesearch_replay_${INSTANCE//\//_}_${FSTYPE}_$$"
IMG_FILE="${WORK_BASE}/loop.img"
LOOP_FILE="${WORK_BASE}/loop.dev"
FS_MNT="${WORK_BASE}/fs"
LOWER="${FS_MNT}/lower"
UPPER_BASE="${FS_MNT}/uppers"
MERGED="${WORK_BASE}/merged"

OVL_IOCTL_BIN="${REPO_ROOT}/agentfs/ovl_ioctl"

INFO()   { echo "[INFO] $*" >&2; }
RESULT() { echo "[RESULT] $*" >&2; }

cleanup() {
    INFO "cleanup $WORK_BASE"
    mountpoint -q "$MERGED" 2>/dev/null && umount -l "$MERGED" 2>/dev/null || true
    mountpoint -q "$FS_MNT" 2>/dev/null && umount -l "$FS_MNT" 2>/dev/null || true
    [[ -f "$LOOP_FILE" ]] && losetup -d "$(cat "$LOOP_FILE")" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$WORK_BASE" "$MERGED"
INFO "instance=$INSTANCE fstype=$FSTYPE"

# ---------- 1. Loopback FS ----------
LOOP_MB=4096
dd if=/dev/zero bs=1M count=$LOOP_MB of="$IMG_FILE" status=none
LOOPDEV=$(losetup -f --show "$IMG_FILE")
echo "$LOOPDEV" > "$LOOP_FILE"

mkdir -p "$FS_MNT"
case "$FSTYPE" in
    ext4)        mkfs.ext4 -q -F "$LOOPDEV"; mount -t ext4 "$LOOPDEV" "$FS_MNT" ;;
    xfs)         mkfs.xfs -q -f -m reflink=0 "$LOOPDEV"; mount -t xfs "$LOOPDEV" "$FS_MNT" ;;
    xfs_reflink) mkfs.xfs -q -f -m reflink=1 "$LOOPDEV"; mount -t xfs "$LOOPDEV" "$FS_MNT" ;;
    *) echo "unknown fstype: $FSTYPE" >&2; exit 1 ;;
esac

mkdir -p "$LOWER" "$UPPER_BASE"

# ---------- 2. Fetch target file(s) at trace's base_commit ----------
# Two paths:
#   (a) local docker image present → docker cp full /testbed (heaviest, most faithful)
#   (b) no image → curl single file(s) from github raw at base_commit (lightweight)
# For per-edit FIEMAP measurement we only need the target files; (b) is sufficient.
DOCKER_IMG=$(python3 -c "import json; print(json.load(open('$ACTIONS_JSON'))['image'])")
BASE_COMMIT=$(python3 -c "import json; print(json.load(open('$ACTIONS_JSON'))['base_commit'])")
HAS_LOCAL_IMG=$(docker images --format '{{.Repository}}' | grep -Fx "$DOCKER_IMG" | head -1 || true)
HAS_LOCAL_IMG=${HAS_LOCAL_IMG:-}

if [[ -n "$HAS_LOCAL_IMG" ]]; then
    INFO "extracting testbed (local image): $DOCKER_IMG (base $BASE_COMMIT)"
    TMP_CID=$(docker create "$DOCKER_IMG" sleep 1)
    docker cp "$TMP_CID:/testbed/." "$LOWER/"
    docker rm -f "$TMP_CID" >/dev/null
    git -C "$LOWER" checkout -q "$BASE_COMMIT" -- . 2>&1 | head -5 || true
    git -C "$LOWER" clean -fdq 2>&1 | head -5 || true
else
    INFO "fetching target files via github raw at base $BASE_COMMIT"
    # Determine github org/repo from instance_id (org__repo-iid)
    ORG_REPO=$(echo "$INSTANCE" | awk -F__ '{print $1"/"$2}' | sed 's/-[0-9]*$//')
    # ↑ astropy__astropy-14309 → astropy/astropy
    #   pylint-dev__pylint-6903 → pylint-dev/pylint
    #   scikit-learn__scikit-learn-10844 → scikit-learn/scikit-learn
    #   pytest-dev__pytest-5809 → pytest-dev/pytest
    INFO "  github repo: $ORG_REPO"
    # Get unique file paths from actions
    python3 -c "
import json
info = json.load(open('$ACTIONS_JSON'))
files = set(e['file_path'] for e in info['edits'])
for f in sorted(files):
    print(f)
" | while read fp; do
        url="https://raw.githubusercontent.com/${ORG_REPO}/${BASE_COMMIT}/${fp}"
        dest="${LOWER}/${fp}"
        mkdir -p "$(dirname "$dest")"
        if ! curl -sf -o "$dest" "$url"; then
            INFO "  WARN: curl failed for $url"
        else
            sz=$(stat -c %s "$dest")
            INFO "  fetched $fp ($sz B)"
        fi
    done
fi

# ---------- 3. Per-edit replay loop (Python helper does the work) ----------
LOOP_NAME=$(basename "$LOOPDEV")
sync; echo 3 > /proc/sys/vm/drop_caches

OUT_DIR="$REPO_ROOT/benchresults/2026-05-11_swesearch_war"
mkdir -p "$OUT_DIR"
RESULTS_JSONL="${OUT_DIR}/${INSTANCE}_${FSTYPE}.jsonl"

python3 "$SCRIPT_DIR/swesearch_replay_engine.py" \
    --actions "$ACTIONS_JSON" \
    --lower "$LOWER" \
    --upper-base "$UPPER_BASE" \
    --merged "$MERGED" \
    --fs-mnt "$FS_MNT" \
    --ovl-ioctl-bin "$OVL_IOCTL_BIN" \
    --loop-name "$LOOP_NAME" \
    --fs-arm "$FSTYPE" \
    --instance "$INSTANCE" \
    --out-jsonl "$RESULTS_JSONL"

INFO "results saved to $RESULTS_JSONL"
N_EDITS=$(wc -l < "$RESULTS_JSONL")
RESULT "  $INSTANCE / $FSTYPE: $N_EDITS edits replayed"
