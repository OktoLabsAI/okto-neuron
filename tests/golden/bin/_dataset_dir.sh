# shellcheck shell=bash
# Shared golden-dataset directory resolution for run-golden.sh and eval-run.sh.
# Sourced, never executed.
#
# resolve_dataset_dir <selector> <golden-dir>
#   Prints the resolved dataset directory on stdout.
#
# Lookup order (in-repo always wins):
#   1. the selector itself, when it is an existing directory
#   2. <golden-dir>/datasets/<selector>
#   3. $OKTO_NEURON_PRIVATE_GOLDEN_DIR/<selector>
#
# Step 3 exists so laptop-only datasets built from private corpora can live
# OUTSIDE the repository and never be tracked. OKTO_NEURON_PRIVATE_GOLDEN_DIR
# defaults to unset; unset means no private lookup happens at all and an
# unknown name still fails with `exit 64`, so CI behaviour is unchanged.
resolve_dataset_dir() {
  local selector="$1" golden_dir="$2" private_root

  if [[ -d "$selector" ]]; then
    (cd "$selector" && pwd)
    return 0
  fi

  local in_repo="$golden_dir/datasets/$selector"
  if [[ -d "$in_repo" || -z "${OKTO_NEURON_PRIVATE_GOLDEN_DIR:-}" ]]; then
    printf '%s\n' "$in_repo"
    return 0
  fi

  private_root="${OKTO_NEURON_PRIVATE_GOLDEN_DIR%/}"
  if [[ "$private_root" != /* ]]; then
    echo "OKTO_NEURON_PRIVATE_GOLDEN_DIR must be an absolute path: $private_root" >&2
    return 64
  fi
  if [[ "$selector" == */* || "$selector" == .* ]]; then
    echo "private dataset selector must be a bare name: $selector" >&2
    return 64
  fi
  if [[ -d "$private_root/$selector" ]]; then
    (cd "$private_root/$selector" && pwd)
    return 0
  fi

  # Fall through to the in-repo path so the existing "no dataset: <path>"
  # diagnostic keeps naming the canonical location.
  printf '%s\n' "$in_repo"
}
