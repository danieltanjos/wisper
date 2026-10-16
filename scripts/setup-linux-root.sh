#!/usr/bin/env bash
# Tudo do port Linux que precisa de root. NAO e' rodado pelo setup.sh: rode
# voce mesmo, uma vez, se o setup.sh apontar pacote faltando.
#
#   sudo ./scripts/setup-linux-root.sh
#
# O wisper NAO precisa de root para funcionar: o atalho global vem do
# kglobalaccel (D-Bus de sessao) e o colar automatico vem do
# org_kde_kwin_fake_input, liberado por um .desktop do usuario
# (~/.local/share/applications/wisper.desktop, criado pelo setup.sh).
# Nada de /dev/uinput, grupo `input`, ydotoold ou regra udev.
#
# Medido em 2026-10-15 neste notebook (Fedora 44 KDE): portaudio, wl-clipboard,
# libappindicator-gtk3, python3-gobject e xprop ja vinham instalados; faltava
# so o python3-tkinter (a pilula). Os outros ficam na linha para uma maquina
# nova, e o dnf pula o que ja existe.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "rode com sudo: sudo $0" >&2
    exit 1
fi

dnf install -y python3-tkinter portaudio wl-clipboard libappindicator-gtk3 \
    python3-gobject xprop
