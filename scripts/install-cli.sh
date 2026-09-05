#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMMAND="$ROOT/lighttable"
if [ "${1:-}" = "--app" ]; then
  if [ "$#" -lt 2 ]; then
    echo 'Usage: install-cli.sh [--app /path/to/LightTable.app] [destination]' >&2
    exit 2
  fi
  APP=$(CDPATH= cd -- "$2" && pwd -P)
  COMMAND="$APP/Contents/MacOS/lighttable-cli"
  shift 2
fi
if [ "$#" -gt 1 ] || [ ! -x "$COMMAND" ]; then
  echo 'Usage: install-cli.sh [--app /path/to/LightTable.app] [destination]' >&2
  exit 2
fi
DESTINATION=${1:-"$HOME/.local/bin"}
mkdir -p "$DESTINATION"
if [ -e "$DESTINATION/lighttable" ] && [ ! -L "$DESTINATION/lighttable" ]; then
  echo "Refusing to replace an existing file: $DESTINATION/lighttable" >&2
  exit 1
fi
ln -sfn "$COMMAND" "$DESTINATION/lighttable"
printf 'Installed lighttable at %s\n' "$DESTINATION/lighttable"
case ":$PATH:" in
  *":$DESTINATION:"*) ;;
  *) printf 'Add %s to PATH to use it from any shell.\n' "$DESTINATION" ;;
esac
