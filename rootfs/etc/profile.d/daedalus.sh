# Loaded by login shells inside the daedalus.
export GPG_TTY="$(tty 2>/dev/null || true)"
export PATH="$HOME/.local/bin:$PATH"
export HISTSIZE=50000 HISTFILESIZE=50000 HISTCONTROL=ignoredups
shopt -s histappend 2>/dev/null || true

case "${DAEDALUS_MODE:-operator}" in
  agent) _tb_color='\[\e[1;31m\]' ;;   # red: fenced agent session
  *)     _tb_color='\[\e[1;36m\]' ;;
esac
PS1="${_tb_color}[daedalus:${DAEDALUS_MODE:-operator}]\[\e[0m\] \u@${DAEDALUS_PROJECT:-\h} \W \$ "
unset _tb_color

