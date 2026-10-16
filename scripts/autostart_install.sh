#!/usr/bin/env bash
# Faz o wisper subir sozinho no login do Plasma.
#
# Grava ~/.config/autostart/wisper.desktop apontando para o .venv/bin/wisper
# com o main.pyw (gravavel sem root, e o Plasma 6 le essa pasta no login).
# O Exec= e' o MESMO executavel do ~/.local/share/applications/wisper.desktop
# criado pelo setup.sh: e' por ele que o KWin libera o colar automatico.
#
# Uso:
#   ./scripts/autostart_install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXE="$ROOT/.venv/bin/wisper"
[[ -x "$EXE" ]] || { echo "ERRO: $EXE nao existe. Rode ./scripts/setup.sh antes." >&2; exit 1; }

DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
mkdir -p "$DIR"
cat > "$DIR/wisper.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=wisper
Comment=Ditado por voz (Meta+A grava, Enter insere, Esc cancela)
Exec=$EXE $ROOT/main.pyw
Path=$ROOT
Icon=audio-input-microphone
Terminal=false
X-KDE-autostart-phase=2
X-KDE-Wayland-Interfaces=org_kde_kwin_fake_input
EOF
echo "autostart gravado em $DIR/wisper.desktop"
echo "para desfazer: ./scripts/autostart_uninstall.sh"
