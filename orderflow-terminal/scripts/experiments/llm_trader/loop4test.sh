#!/bin/bash
# Final held-out test: m3 prompt, Qwen3-4B; restarts the server and resumes if it dies.
S=/tmp/claude-0/-home-user-elliott-wave-analyzer/6f1108b8-3684-52a2-9de8-2e3817b6e995/scratchpad
cd $S/llmt
touch res_m3_test.jsonl
for i in $(seq 1 30); do
  [ $(wc -l < res_m3_test.jsonl) -ge 188 ] && break
  # a server left over from a previous round may hang on shutdown and keep the port
  for p in $(ps -eo pid,comm | awk '$2=="llama-server"{print $1}'); do kill -9 $p; done
  sleep 2
  (cd $S/llm && exec llama.cpp/build/bin/llama-server -m qwen4b.gguf --host 127.0.0.1 --port 8090 -c 8192 -t 4 --parallel 1 --cache-ram 0 > server.log 2>&1) &
  P=$!
  until curl -s 127.0.0.1:8090/health | grep -q ok; do sleep 3; kill -0 $P 2>/dev/null || break; done
  python3 run.py m3 test >> m3_test.log 2>&1
  kill -9 $P 2>/dev/null; sleep 3
done
python3 run.py rules test > rules_test.log 2>&1
echo done > test.done
