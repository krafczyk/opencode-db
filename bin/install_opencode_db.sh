#!/usr/bin/env bash

set -euo pipefail
umask 077

program=${0##*/}
script_dir=$(dirname "$(realpath "$0")")
source_launcher="$script_dir/opencode-db"
source_package="$script_dir/../src/opencode_db"
target_dir="$HOME/.local/bin"
target="$target_dir/opencode-db"
check_only=0
recovery_dir=
stage=
recovery_stage=

usage() {
  cat <<EOF
Usage: $program [--check] [--recovery-dir DIR]

--check verifies an installed launcher. With --recovery-dir it performs only
preflight for an installation. Installation requires a caller-created private
recovery directory and retains a replaced noncompliant launcher there.
EOF
}

die() {
  printf '%s: %s\n' "$program" "$*" >&2
  exit 1
}

cleanup() {
  [[ -z $stage || ! -e $stage ]] || rm -f -- "$stage"
  [[ -z $recovery_stage || ! -e $recovery_stage ]] || rm -f -- "$recovery_stage"
}
trap cleanup EXIT

check_safe_directory() {
  local path=$1 label=$2 allow_trusted_root=${3:-0} uid mode
  [[ ! -L $path ]] || die "unsafe directory symlink: $label"
  [[ -d $path ]] || die "unsafe path is not a directory: $label"
  uid=$(stat -Lc '%u' -- "$path") || die "cannot inspect directory: $label"
  mode=$(stat -Lc '%a' -- "$path") || die "cannot inspect directory mode: $label"
  if (( allow_trusted_root )) && [[ ${MKCHAD_TRUST_GROUP_WRITABLE_ROOTS:-0} == 1 ]]; then
    [[ $uid == 0 || $uid == "$EUID" ]] || die "unsafe directory owner: $label"
    (( (8#$mode & 002) == 0 )) || die "unsafe world-writable directory: $label"
    return
  fi
  [[ $uid == "$EUID" ]] || die "unsafe directory owner: $label"
  (( (8#$mode & 022) == 0 )) || die "unsafe group- or world-writable directory: $label"
}

validate_target_parents() {
  local current=$HOME part
  [[ $HOME == /* ]] || die "HOME must be an absolute path"
  check_safe_directory "$current" "$current" 1
  for part in .local bin; do
    current="$current/$part"
    [[ ! -e $current && ! -L $current ]] && continue
    check_safe_directory "$current" "$current"
  done
}

ensure_target_dir() {
  local current=$HOME part
  validate_target_parents
  for part in .local bin; do
    current="$current/$part"
    if [[ ! -e $current && ! -L $current ]]; then
      mkdir -m 700 -- "$current"
    fi
    check_safe_directory "$current" "$current"
  done
}

validate_source() {
  [[ ! -L $source_launcher && -f $source_launcher ]] || die "launcher source is unavailable: $source_launcher"
  [[ ! -L $source_package && -d $source_package ]] || die "source package is unavailable: $source_package"
  [[ ! -L $source_package/__main__.py && -f $source_package/__main__.py ]] || die "source package entry point is unavailable"
}

validate_python() {
  command -v python3 >/dev/null 2>&1 || die "python3 is unavailable"
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or newer is required"
}

validate_recovery_dir() {
  local mode
  [[ -n $recovery_dir ]] || die "--recovery-dir is required for installation"
  [[ ! -L $recovery_dir && -d $recovery_dir ]] || die "recovery directory is not a directory: $recovery_dir"
  check_safe_directory "$recovery_dir" "$recovery_dir"
  mode=$(stat -Lc '%a' -- "$recovery_dir") || die "cannot inspect recovery directory mode: $recovery_dir"
  [[ $mode == 700 ]] || die "recovery directory must have mode 0700: $recovery_dir"
}

validate_output() {
  local uid
  [[ ! -L $target ]] || die "symlink output is unsafe: $target"
  [[ ! -e $target ]] && return
  [[ -f $target ]] || die "non-regular output is unsafe: $target"
  uid=$(stat -Lc '%u' -- "$target") || die "cannot inspect output: $target"
  [[ $uid == "$EUID" ]] || die "foreign-owned output is unsafe: $target"
}

validate_existing_backup() {
  local backup="$recovery_dir/opencode-db" mode uid
  [[ ! -L $backup && -f $backup ]] || die "unsafe existing recovery asset: $backup"
  uid=$(stat -Lc '%u' -- "$backup") || die "cannot inspect recovery asset: $backup"
  mode=$(stat -Lc '%a' -- "$backup") || die "cannot inspect recovery asset mode: $backup"
  [[ $uid == "$EUID" && $mode == 600 ]] || die "unsafe existing recovery asset: $backup"
  cmp -s -- "$target" "$backup" || die "existing recovery asset differs from output: $backup"
}

output_is_compliant() {
  local mode
  validate_output
  [[ -f $target ]] || return 1
  mode=$(stat -Lc '%a' -- "$target") || die "cannot inspect output mode: $target"
  [[ $mode == 755 ]] && cmp -s -- "$source_launcher" "$target"
}

output_identity() {
  if [[ ! -e $target && ! -L $target ]]; then
    printf '%s\n' absent
    return
  fi
  validate_output
  stat -Lc '%d:%i' -- "$target" || die "cannot inspect output identity: $target"
}

preflight_install() {
  validate_source
  validate_python
  validate_target_parents
  validate_output
  validate_recovery_dir
  if [[ -f $target ]] && ! output_is_compliant; then
    if [[ -e $recovery_dir/opencode-db || -L $recovery_dir/opencode-db ]]; then
      validate_existing_backup
    fi
  fi
}

backup_output() {
  local backup="$recovery_dir/opencode-db"
  [[ -f $target ]] || return 0
  if [[ -e $backup || -L $backup ]]; then
    validate_existing_backup
    return
  fi
  recovery_stage=$(mktemp "$recovery_dir/.opencode-db.XXXXXX")
  cp -- "$target" "$recovery_stage"
  chmod 600 -- "$recovery_stage"
  cmp -s -- "$target" "$recovery_stage" || die "recovery staging differs from output"
  sync -- "$recovery_stage"
  mv -T -- "$recovery_stage" "$backup"
  recovery_stage=
  sync -- "$recovery_dir"
}

install_launcher() {
  local expected_output_identity target_dir_identity
  output_is_compliant && return
  check_safe_directory "$target_dir" "$target_dir"
  target_dir_identity=$(stat -Lc '%d:%i' -- "$target_dir") || die "cannot inspect target directory"
  expected_output_identity=$(output_identity)
  backup_output
  stage=$(mktemp "$target_dir/.opencode-db.XXXXXX")
  cp -- "$source_launcher" "$stage"
  chmod 755 -- "$stage"
  cmp -s -- "$source_launcher" "$stage" || die "staged launcher differs from source"
  sync -- "$stage"
  [[ $(stat -Lc '%d:%i' -- "$target_dir") == "$target_dir_identity" ]] \
    || die "target directory changed during installation"
  [[ $(output_identity) == "$expected_output_identity" ]] \
    || die "output changed during installation: $target"
  mv -fT -- "$stage" "$target"
  stage=
  sync -- "$target"
  sync -- "$target_dir"
}

verify_installed() {
  local mode
  validate_source
  validate_python
  validate_target_parents
  validate_output
  [[ -f $target ]] || die "installed launcher is absent: $target"
  mode=$(stat -Lc '%a' -- "$target") || die "cannot inspect installed launcher mode"
  [[ $mode == 755 ]] || die "installed launcher must have mode 0755: $target"
  cmp -s -- "$source_launcher" "$target" || die "installed launcher differs from source"
  command -v timeout >/dev/null 2>&1 || die "timeout is unavailable for launcher verification"
  timeout --foreground --kill-after=2 10 "$target" --help >/dev/null \
    || die "installed launcher --help verification failed"
}

while (($#)); do
  case "$1" in
    --check) check_only=1; shift ;;
    --recovery-dir)
      (($# >= 2)) || die "--recovery-dir requires a path"
      recovery_dir=$2
      shift 2
      ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

case ${MKCHAD_TRUST_GROUP_WRITABLE_ROOTS:-0} in
  0 | 1) ;;
  *) die "MKCHAD_TRUST_GROUP_WRITABLE_ROOTS must be 0 or 1" ;;
esac

if (( check_only )); then
  if [[ -n $recovery_dir ]]; then
    preflight_install
  else
    verify_installed
  fi
  exit 0
fi

preflight_install
ensure_target_dir
preflight_install
install_launcher
