#!/usr/bin/env bash
# Leak check: fails when tracked (or not-yet-ignored untracked) files hold
# personal data. Static rules run everywhere; the runtime rules run only on a
# machine with Steam and read THIS machine's real identifiers at run time, so
# nothing personal is ever stored in the repo. Runtime hits print file:line only.
#
# Allow-listed placeholders: /home/{user,tester,x,u,you,name,deck,example},
# addresses on example.{invalid,com,org,net}, SteamID64 76561198000000xxx (synthetic),
# 76561197960265728 (the universe base constant) and 76561190000000000 (fixture).
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
fail=0
G=(git grep --untracked -nIP --no-color)
# Skip this script itself: it spells out the allow-lists.
excl=(':(exclude)scripts/check-personal-data.sh')

report() { # title, rc-of-grep, output
  if [ -n "$3" ]; then echo "LEAK: $1" >&2; printf '%s\n' "$3" | cut -c1-160 >&2; fail=1; fi
}

out="$("${G[@]}" '(?<![\w}$)/.-])/home/(?!(user|tester|x|u|you|name|deck|example)\b)[A-Za-z_][\w.-]*' -- . "${excl[@]}")"
report 'absolute /home/<name> path (use /home/user)' 0 "$out"

out="$("${G[@]}" '(?<![\w.+-])[\w.+-]+@(?!(?:[\w-]+\.)*example\.(?:invalid|com|org|net)\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b' -- . "${excl[@]}")"
report 'non-placeholder e-mail address (use example.invalid)' 0 "$out"

out="$("${G[@]}" '(?<!\d)7656119\d{10}(?!\d)' -- . "${excl[@]}" | grep -vP '(?<!\d)(76561198000000\d{3}|76561197960265728|76561190000000000)(?!\d)')"
report 'SteamID64 outside the synthetic range 76561198000000xxx' 0 "$out"

# ---- runtime rules: this machine's real identifiers --------------------------
pat="$(mktemp)"; trap 'rm -f "$pat"' EXIT
generic=' localhost laptop desktop deck steamdeck host box server archlinux user steam '
add() { local v="${1:-}"; [ "${#v}" -ge 4 ] || return 0
  case "$generic" in *" ${v,,} "*) return 0;; esac; printf '%s\n' "$v" >>"$pat"; }
steam="${STEAM_ROOT:-$HOME/.local/share/Steam}"
if [ -r "$steam/config/loginusers.vdf" ]; then
  while IFS= read -r id; do add "$id"; add "$((id - 76561197960265728))"; done \
    < <(grep -oE '"7656[0-9]{13}"' "$steam/config/loginusers.vdf" | tr -d '"')
  while IFS= read -r v; do add "$v"; done \
    < <(sed -nE 's/^[[:space:]]*"(PersonaName|AccountName)"[[:space:]]+"([^"]*)".*/\2/p' "$steam/config/loginusers.vdf")
fi
if [ -d "$steam/userdata" ]; then
  for d in "$steam"/userdata/*; do b="$(basename "$d")"; case "$b" in ''|*[!0-9]*|0) ;; *) [ "${#b}" -ge 6 ] && add "$b";; esac; done
fi
add "$(hostname 2>/dev/null || true)"
add "$(cat /etc/machine-id 2>/dev/null || true)"
fleet="${XDG_CONFIG_HOME:-$HOME/.config}/luna/fleet.json"
if [ -r "$fleet" ] && command -v python3 >/dev/null; then
  while IFS= read -r v; do add "$v"; done < <(python3 -I - "$fleet" <<'PY' 2>/dev/null
import json, sys
def walk(x):
    if isinstance(x, dict):
        for v in x.values(): yield from walk(v)
    elif isinstance(x, list):
        for v in x: yield from walk(v)
    elif isinstance(x, str): yield x
for s in walk(json.load(open(sys.argv[1]))): print(s.split('@')[-1])
PY
)
fi
if [ -s "$pat" ]; then
  out="$(git grep --untracked -nIiwF --no-color -f "$pat" -- . "${excl[@]}" | cut -d: -f1,2)"
  report "this machine's real account id / persona / host name found (values withheld)" 0 "$out"
fi

if [ "$fail" = 0 ]; then echo 'check-personal-data: OK'; else exit 1; fi
