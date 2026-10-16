#!/usr/bin/env bash
# Solta na hora os atalhos globais do wisper no kglobalaccel do KDE.
#
# Para que serve: enquanto o wisper grava, Return e Escape sao atalhos GLOBAIS
# dele, e o kglobalaccel mantem um atalho ativo mesmo depois que o processo
# dono morre (medido). O app solta ao sair, o guardiao solta se o app morrer,
# e o proximo start solta o que sobrou. Este script e' para o caso em que os
# tres falharam e o Enter/Esc "pararam de funcionar" no sistema inteiro.
#
# Uso:
#   ./scripts/release-keys.sh            # solta Return/Escape, desativa o Meta+A
#   ./scripts/release-keys.sh --forget   # alem disso apaga o grupo [wisper] das
#                                        # Configuracoes do Sistema > Atalhos
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=/usr/bin/python3
MODE="--now"
[[ "${1:-}" == "--forget" ]] && MODE="--forget"
cd "$ROOT"
exec "$PY" -m wispr.linux.keyguard "$MODE"
