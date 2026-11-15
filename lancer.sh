#!/bin/bash
# Jardinier des dossiers : un passage par nuit sur un dépôt git de dossiers Markdown (voir README.md).
# Travaille dans une copie de travail jetable, effacée à la fin : le dépôt de travail de l'utilisateur n'est jamais touché.
#
# Usage : lancer.sh nuit     passage de nuit (heures HEURES, rattrapages compris ; FORCE=1 hors de ces heures)
#         lancer.sh essai    passage complet SANS push ni dépôt du rapport ; la copie reste dans donnees/essai/ pour relecture
#                            (SANS_CLAUDE=1 : sans le passage de Claude ; ESSAI_DATE=AAAA-MM-JJ possible)
#
# Réglages, hors dépôt : donnees/lancer.local (variables shell)
#   DEPOT        adresse git du dépôt des dossiers (dépôt central, branche main)
#   DEPOSER      commande qui dépose la section du rapport du matin : $DEPOSER <agent> --sujet S --text-file F --date J
#   COMPTE_CMD   imprime le compte Claude à utiliser ; JETON_FICHIER / CONFIG_CLAUDE : chemins (%s = compte)
#   CLAUDE_CMD   lanceur de Claude Code (défaut : claude) ; MODELE, EFFORT
#   LOURD        préfixe des étapes lourdes (ex. « lourd --nom jardinier ») ; HEURES (défaut « 02 03 04 », heure de Paris)
#   MAX_FUSIONS, MAX_CLOS : plafonds par nuit (défaut 3 et 5) ; TZ_RAPPORT (défaut Europe/Paris)
set -u
PATH="/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin"; export PATH
ICI=$(cd "$(dirname "$0")" && pwd)
D="$ICI/donnees"; MODE="${1:-nuit}"
DEPOT=""; DEPOSER=""; COMPTE_CMD="echo defaut"; JETON_FICHIER=""; CONFIG_CLAUDE=""; CLAUDE_CMD=claude
MODELE=claude-opus-5-5; EFFORT=medium; LOURD=""; HEURES="02 03 04"; MAX_FUSIONS=3; MAX_CLOS=5; TZ_RAPPORT=Europe/Paris
[ -f "$D/lancer.local" ] && . "$D/lancer.local"
[ -n "$DEPOT" ] || { echo "DEPOT non réglé (donnees/lancer.local)"; exit 2; }
H=$(TZ=$TZ_RAPPORT date +%H); J=${ESSAI_DATE:-$(TZ=$TZ_RAPPORT date +%F)}
N="$D/nuits/$J"; mkdir -p "$N" "$D/logs" "$D/verrous"
L="$D/logs/$J.log"; log() { echo "$(TZ=$TZ_RAPPORT date '+%F %T') [$MODE] $*" >> "$L"; }
case "$MODE" in
  nuit) if [ "${FORCE:-0}" != 1 ]; then case " $HEURES " in *" $H "*) ;; *) exit 0;; esac; fi
        [ -f "$N/.fini" ] && exit 0 ;;
  essai) N="$D/essai"; rm -rf "$N"; mkdir -p "$N" ;;
  *) echo "usage : lancer.sh nuit|essai"; exit 2 ;;
esac
exec 9>"$D/verrous/passe.lock"; flock -n 9 || { log "un passage tourne déjà"; exit 1; }
PY="python3 -I $ICI/jardinier.py"; export JARDINIER_MASQUER="$D/masquer.local"
[ -n "$LOURD" ] && PY="$LOURD $PY"
W="$HOME/jardinier-passe"; [ "$MODE" = essai ] && W="$N/copie"
log "début"

copie() {   # copie de travail neuve du dépôt central
  rm -rf "$W"
  git clone -q --branch main "$DEPOT" "$W" 2>> "$L" || return 1
  AVANT=$(git -C "$W" rev-parse HEAD)
}

# 1. analyse et passage de Claude (une fois)
copie || { log "clone impossible"; echo '{"erreur": "dépôt des dossiers injoignable"}' > "$N/resultat.json"; }
if [ ! -s "$N/resultat.json" ]; then
  $PY analyser "$W" "$N/analyse.json" --date "$J" >> "$L" 2>&1 || { log "analyse en échec"; echo '{"erreur": "analyse en échec (voir le journal)"}' > "$N/resultat.json"; }
fi
if [ ! -s "$N/resultat.json" ]; then
  A_JUGER=$(python3 -c "import json,sys;a=json.load(open(sys.argv[1]));print(sum(len(a[k]) for k in ('liens_a_juger','isoles','doublons','perimes')))" "$N/analyse.json")
  echo '{}' > "$N/decisions.json"
  if [ "$A_JUGER" -gt 0 ] && [ "${SANS_CLAUDE:-0}" != 1 ]; then
    $PY consigne "$W" "$N/analyse.json" "$ICI/consigne.md" > "$N/consigne.md"
    CPT=$(eval "$COMPTE_CMD" < /dev/null 2>/dev/null | tr -d "[:space:]")
    ENVCL=(env -u ANTHROPIC_API_KEY -u CLAUDECODE)
    if [ -n "$CONFIG_CLAUDE" ]; then CFG=$(printf "$CONFIG_CLAUDE" "$CPT"); mkdir -p "$CFG"; ENVCL+=(CLAUDE_CONFIG_DIR="$CFG"); fi
    if [ -n "$JETON_FICHIER" ]; then
      TOK=$(printf "$JETON_FICHIER" "$CPT")
      ENVCL+=(CLAUDE_CODE_OAUTH_TOKEN="$(grep -oP '(?<=CLAUDE_CODE_OAUTH_TOKEN=).*' "$TOK" 2>/dev/null | tr -d "\"'")")
    fi
    log "claude $MODELE ($EFFORT), compte $CPT, $A_JUGER cas à juger"
    ( cd "$N" && "${ENVCL[@]}" timeout 30m ${LOURD:+$LOURD} "$CLAUDE_CMD" -p "$(cat "$N/consigne.md")" --model "$MODELE" --effort "$EFFORT" \
        --tools "" --output-format text < /dev/null ) > "$N/reponse.txt" 2>> "$L"
    log "claude fini : code $?"
    $PY decisions "$N/analyse.json" "$N/reponse.txt" > "$N/decisions.json" 2>> "$L" || echo '{}' > "$N/decisions.json"
    AVIS=$(python3 -c "
import json,re,sys
t=open(sys.argv[1]).read(); m=re.search(r'\{.*\}',t,re.S)
try: print(str(json.loads(m.group(0)).get('avis',''))[:240])
except Exception: print('')" "$N/reponse.txt" 2>/dev/null)
    [ -s "$N/reponse.txt" ] || AVIS="passage de Claude non fait (réponse vide) : seules les réparations sûres sont appliquées"
  fi
fi

# 2. appliquer, vérifier, pousser (3 essais : si le dépôt a bougé entre-temps, on refait sur la copie neuve)
ESSAI=0; POUSSE=""
while [ ! -s "$N/resultat.json" ] || [ "$ESSAI" -gt 0 ]; do
  ESSAI=$((ESSAI + 1))
  if [ "$ESSAI" -gt 1 ]; then copie || break; $PY analyser "$W" "$N/analyse.json" --date "$J" >> "$L" 2>&1 || break; fi
  git -C "$W" config user.name >/dev/null || git -C "$W" config user.name "Jardinier des dossiers"
  $PY appliquer "$W" "$N/analyse.json" "$N/decisions.json" --max-fusions "$MAX_FUSIONS" --max-clos "$MAX_CLOS" --date "$J" \
      > "$N/resultat.json" 2>> "$L" || { log "application en échec"; echo '{"erreur": "application en échec (voir le journal)"}' > "$N/resultat.json"; break; }
  if ! $PY verifier "$W" "$AVANT" > "$N/pertes.txt" 2>> "$L"; then
    log "contrôle : contenu perdu, rien n'est poussé ($(wc -l < "$N/pertes.txt") ligne(s))"
    python3 - "$N/resultat.json" "$(wc -l < "$N/pertes.txt")" <<'P'
import json, sys
r = json.load(open(sys.argv[1])); r["erreur"] = f"le contrôle a trouvé {sys.argv[2]} ligne(s) qui auraient disparu : passage annulé, à corriger dans le programme"
json.dump(r, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
P
    break
  fi
  NB=$(git -C "$W" rev-list --count "$AVANT..HEAD")
  if [ "$NB" = 0 ] || [ "$MODE" = essai ]; then log "$NB commit(s) ; rien à pousser ($MODE)"; break; fi
  if git -C "$W" pull -q --rebase origin main >> "$L" 2>&1 && git -C "$W" push -q origin HEAD:main >> "$L" 2>&1; then
    HEAD_=$(git -C "$W" rev-parse HEAD)
    DISTANT=$(git ls-remote "$DEPOT" refs/heads/main | cut -f1)
    if [ "$DISTANT" = "$HEAD_" ]; then
      BASE_PUSH=$(git -C "$W" rev-parse "HEAD~$NB")   # après le rebase, les NB commits du passage sont en tête
      POUSSE="${BASE_PUSH:0:8}..${HEAD_:0:8}"
      log "poussé : $NB commit(s), $POUSSE (vérifié sur le dépôt central)"
      break
    fi
    log "push annoncé mais le dépôt central est à ${DISTANT:0:8}, pas à ${HEAD_:0:8}"
  else
    git -C "$W" rebase --abort >/dev/null 2>&1
    log "push refusé ou conflit au rebase (essai $ESSAI)"
  fi
  if [ "$ESSAI" -ge 3 ]; then
    python3 - "$N/resultat.json" <<'P'
import json, sys
r = json.load(open(sys.argv[1])); r["erreur"] = "le dépôt des dossiers a bougé pendant le passage 3 fois de suite : rien n'est poussé, nouvel essai la nuit prochaine"
json.dump(r, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
P
    break
  fi
done

# 3. le rapport du matin
python3 - "$N/resultat.json" "${AVIS:-}" <<'P'
import json, sys
r = json.load(open(sys.argv[1]))
if sys.argv[2]:
    r["claude"] = sys.argv[2]
json.dump(r, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
P
$PY rapport "$N/resultat.json" "$POUSSE" > "$N/rapport.txt" 2>> "$L"
SUJET="Jardinier des dossiers : $(head -1 "$N/rapport.txt" | cut -c1-120)"
if [ "$MODE" = nuit ] && [ -n "$DEPOSER" ]; then
  $DEPOSER jardinier --sujet "$SUJET" --text-file "$N/rapport.txt" --date "$J" >> "$L" 2>&1 && log "section déposée" || log "dépôt de la section en échec"
fi
[ "$MODE" = nuit ] && rm -rf "$W"
ERR=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('erreur',''))" "$N/resultat.json")
if [ "$MODE" = nuit ]; then
  # un échec passager (dépôt qui a bougé, injoignable) laisse la place au rattrapage de l'heure suivante
  case "$ERR" in *"a bougé"*|*injoignable*) [ "$H" = "${HEURES##* }" ] && touch "$N/.fini";; *) touch "$N/.fini";; esac
fi
log "fin${ERR:+ : $ERR}"
[ -z "$ERR" ]
