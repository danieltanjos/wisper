#!/usr/bin/env bash
# Para a instancia do wisper que estiver rodando.
#
# SIGTERM primeiro: o app trata o sinal, desarma os atalhos (Return/Escape se
# estiver gravando, e desativa o Meta+A), fecha microfone e modelo. Passado o
# timeout, SIGKILL — e ai quem solta as teclas e' o guardiao
# (wispr/linux/keyguard.py), que ve o pai morrer. Por garantia o script chama
# o release-keys no fim. O jeito mais limpo continua sendo "Sair" na bandeja.
#
# Uso:
#   ./scripts/stop.sh [--force] [--timeout 8]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMEOUT=8
FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1 ;;
        --timeout) TIMEOUT="$2"; shift ;;
        *) echo "opcao desconhecida: $1" >&2; exit 2 ;;
    esac
    shift
done

PIDS="$(pgrep -f -- "$ROOT/main.pyw" || true)"
if [[ -z "$PIDS" ]]; then
    echo "wisper nao esta rodando."
    exit 0
fi

if [[ $FORCE == 0 ]]; then
    kill -TERM $PIDS 2>/dev/null || true
    for _ in $(seq 1 $((TIMEOUT * 4))); do
        sleep 0.25
        PIDS="$(pgrep -f -- "$ROOT/main.pyw" || true)"
        [[ -z "$PIDS" ]] && break
    done
fi
if [[ -n "$PIDS" ]]; then
    echo "encerrando a forca: $PIDS"
    kill -KILL $PIDS 2>/dev/null || true
    sleep 1
fi
"$ROOT/scripts/release-keys.sh" >/dev/null 2>&1 || true
echo "wisper parado."
