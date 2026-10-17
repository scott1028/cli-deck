# Sourced from ~/.bashrc.d/cli-deck (a symlink to this file, made by `make install`).
#
# Why a shell function: the deck daemon starts children with `bash -ic`, so the
# daemon's environment is not the caller's. Aliases and ~/.bashrc functions are
# re-derived by `bash -ic`, but a function defined only in this shell is not --
# so export it as BASH_FUNC_<name>%% before running the real binary.
#
# The cli-deck binary itself is installed separately (`make install` -> uv tool install).

cli-deck() {
  # `type -P` skips this function and finds the installed executable on PATH.
  local cli
  cli="$(type -P cli-deck)"
  if [ -z "$cli" ]; then
    echo "cli-deck: CLI not on PATH; run 'make install' in the repo" >&2
    return 1
  fi

  # Bare `cli-deck` opens the TUI: nothing to export.
  if [ $# -eq 0 ]; then
    "$cli"
    return $?
  fi

  # `--` so a leading flag such as -l is not read as a declare option.
  if declare -F -- "$1" >/dev/null 2>&1; then
    export -f -- "$1"
  fi

  # exec only in non-interactive shells (scripts): in an interactive shell exec
  # would replace the shell itself, and detaching with Ctrl-] would close it.
  if [[ $- == *i* ]]; then
    "$cli" "$@"
  else
    exec "$cli" "$@"
  fi
}
