#!/usr/bin/env bash
# Launch a capped, resumable editorial study in the background with a study card for replay.
# usage: launch.sh OUTPUT_DIR [study-script flags...]   (flags are passed to run_editorial_prequential_study.py)
# env:   KEYS_FILE (default ~/Projects/Biased-Decisions/.env), KEY_NAMES (default "TYPESAFE_API_KEY OPENAI_API_KEY"),
#        MIN_FREE_GB (default 5)
set -euo pipefail
out=${1:?output dir}; shift
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
keys=${KEYS_FILE:-$HOME/Projects/Biased-Decisions/.env}
free=$(df -g "$HOME" | awk 'NR==2{print $4}')
[ "$free" -ge "${MIN_FREE_GB:-5}" ] || { echo "only ${free} GB free; free disk first" >&2; exit 1; }
for name in ${KEY_NAMES:-TYPESAFE_API_KEY OPENAI_API_KEY}; do
  value=$(grep -m1 "^${name}=" "$keys" | cut -d= -f2-) || true
  [ -n "$value" ] || { echo "$name not found in $keys" >&2; exit 1; }
  export "$name=$value"
done
for flag in --max-decision-calls --max-optimizer-calls; do
  case " $* " in *" $flag "*) ;; *) echo "missing required cap $flag" >&2; exit 1;; esac
done
[ ! -e "$out" ] || case " $* " in *" --resume "*) ;; *) echo "$out exists; use --resume or a new dir" >&2; exit 1;; esac
log="$out.log"; [ ! -e "$log" ] || log="$out.$(date +%H%M%S).log"; mkdir -p "$(dirname "$out")"
"$repo/.venv/bin/python" - "$repo" "$out" "$@" <<'P'
import json,subprocess,sys,time,platform
repo,out,*argv=sys.argv[1:]
sha=subprocess.check_output(['git','-C',repo,'rev-parse','HEAD'],text=True).strip()
dirty=bool(subprocess.check_output(['git','-C',repo,'status','--porcelain','--','src','scripts'],text=True).strip())
card=out+'.card.json'
import os
if os.path.exists(card): card=out+'.card.'+time.strftime('%H%M%S')+'.json'
json.dump({'started':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'engine_commit':sha,'engine_dirty':dirty,'python':platform.python_version(),
 'argv':argv,'output':out},open(card,'w'),indent=1)
P
cd "$repo"
nohup .venv/bin/python scripts/run_editorial_prequential_study.py --output "$out" --confirm-live "$@" > "$log" 2>&1 &
pid=$!
# Capture the environment next to the card: engine commit, dirty flag, python, and (for kev) the model server's identity.
models=""; reached=""
case " $* " in *" --decision-provider kev "*)
  models=$(curl -s -m 5 "${KEV_BASE_URL:-http://127.0.0.1:8009}/v1/models" 2>/dev/null) && reached=yes || reached=no;;
esac
MODELS="$models" REACHED="$reached" "$repo/.venv/bin/python" - "$repo" "$out" <<'P'
import json,os,platform,subprocess,sys,time
repo,out=sys.argv[1:]
git=lambda *a: subprocess.check_output(['git','-C',repo,*a],text=True).strip()
env={'captured':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'engine_commit':git('rev-parse','HEAD'),
 'engine_dirty':bool(git('status','--porcelain','--','src','scripts')),'python':platform.python_version()}
if os.environ['REACHED']:
    env['kev_base_url']=os.environ.get('KEV_BASE_URL','http://127.0.0.1:8009')
    env['kev_server_reachable']=os.environ['REACHED']=='yes'
    try: env['kev_models']=json.loads(os.environ['MODELS'])
    except ValueError: env['kev_models']=None
json.dump(env,open(out+'.environment.json','w'),indent=1)
P
echo "pid $pid log $log card $out.card.json environment $out.environment.json"
