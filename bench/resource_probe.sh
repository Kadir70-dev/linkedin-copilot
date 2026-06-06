#!/usr/bin/env bash
# Measure RAM/CPU footprint of a model under the ollama container during a real
# generation. Reports: model's loaded SIZE + processor split (ollama ps), and the
# ollama container's peak MEM USAGE + CPU% sampled during inference (docker stats).
set -u
MODEL="${1:?usage: resource_probe.sh <model>}"
OLLAMA_CID=$(docker-compose ps -q ollama)

# Kick a real generation (long enough to sample) in the background.
PROMPT='Write a 60 word professional LinkedIn note to a startup founder about cloud infrastructure and go-to-market strategy.'
curl -s localhost:11434/api/generate \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"$PROMPT\",\"stream\":false}" >/tmp/probe_gen.out 2>&1 &
GENPID=$!

peak_mem=0; peak_cpu=0; samples=0
while kill -0 "$GENPID" 2>/dev/null; do
  line=$(docker stats --no-stream --format '{{.MemUsage}}|{{.CPUPerc}}' "$OLLAMA_CID" 2>/dev/null)
  mem=$(echo "$line" | sed 's#/.*##' | tr -d ' ' )         # e.g. 5.6GiB
  cpu=$(echo "$line" | cut -d'|' -f2 | tr -d ' %')
  # normalize mem to MiB
  num=$(echo "$mem" | grep -oE '[0-9.]+'); unit=$(echo "$mem" | grep -oE '[A-Za-z]+')
  case "$unit" in
    GiB) mib=$(awk "BEGIN{printf \"%.1f\", $num*1024}") ;;
    MiB) mib=$num ;;
    *)   mib=0 ;;
  esac
  awk "BEGIN{exit !($mib>$peak_mem)}" && peak_mem=$mib
  awk "BEGIN{exit !($cpu>$peak_cpu)}" 2>/dev/null && peak_cpu=$cpu
  samples=$((samples+1))
  sleep 1
done
wait "$GENPID" 2>/dev/null

echo "=== $MODEL ==="
docker-compose exec -T ollama ollama ps 2>/dev/null | grep -E "NAME|$MODEL"
peak_gib=$(awk "BEGIN{printf \"%.2f\", $peak_mem/1024}")
echo "ollama container peak: MEM=${peak_gib}GiB  CPU=${peak_cpu}%  (samples=$samples)"
