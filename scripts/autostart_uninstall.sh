#!/usr/bin/env bash
# Desfaz o autostart_install.sh. Nao para a instancia que estiver rodando
# (para isso: ./scripts/stop.sh).
#
# Uso:
#   ./scripts/autostart_uninstall.sh
set -euo pipefail

F="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/wisper.desktop"
if [[ -f "$F" ]]; then
    rm -f "$F"
    echo "removido: $F"
else
    echo "nao havia autostart do wisper em $F"
fi
