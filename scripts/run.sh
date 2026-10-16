#!/usr/bin/env bash
# Sobe o wisper no Linux.
#
# Por padrao lanca main.pyw com o .venv/bin/wisper, destacado (setsid): sem
# terminal, sobrevive ao fechamento deste shell, e TUDO vai para logs/wisper.log.
# Tem que ser o .venv/bin/wisper (e nao o .venv/bin/python): e' o executavel que
# o KWin libera para colar o texto sozinho. Ver scripts/setup.sh.
#
# Uso:
#   ./scripts/run.sh             # em segundo plano
#   ./scripts/run.sh --console   # em primeiro plano, erros visiveis; Ctrl+C encerra
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXE="$ROOT/.venv/bin/wisper"
CONSOLE=0
[[ "${1:-}" == "--console" ]] && CONSOLE=1

[[ -x "$EXE" ]] || { echo "ERRO: $EXE nao existe. Rode ./scripts/setup.sh" >&2; exit 1; }

LOCK="${XDG_RUNTIME_DIR:-$ROOT/logs}/wisper.lock"
if [[ -f "$LOCK" ]] && ! flock -n "$LOCK" true 2>/dev/null; then
    echo "wisper ja esta rodando (pid $(cat "$LOCK" 2>/dev/null || echo ?)). Use ./scripts/stop.sh"
    exit 0
fi

cd "$ROOT"
if [[ $CONSOLE == 1 ]]; then
    exec "$EXE" "$ROOT/main.pyw"
fi
setsid -f "$EXE" "$ROOT/main.pyw" </dev/null >/dev/null 2>&1
sleep 1
echo "wisper subiu em segundo plano. Log: $ROOT/logs/wisper.log"
