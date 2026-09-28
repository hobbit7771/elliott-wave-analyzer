#!/bin/bash
# Local LLM for the news filter: llama.cpp server with a small open model (Apache-2.0), listening on 127.0.0.1:8081 only.
# Idempotent: re-running skips what is already installed. Runs as root (Run Command or first boot).
set -euo pipefail
exec >> /var/log/oft-llm.log 2>&1
echo "== $(date -u +%FT%TZ) llm setup"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y cmake build-essential git curl >/dev/null
mkdir -p /opt/llm/models
if [ ! -x /opt/llm/llama.cpp/build/bin/llama-server ]; then
  [ -d /opt/llm/llama.cpp ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp /opt/llm/llama.cpp
  cd /opt/llm/llama.cpp
  cmake -B build -DGGML_NATIVE=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DCMAKE_BUILD_TYPE=Release
  cmake --build build --target llama-server -j "$(nproc)"
fi
MODEL=/opt/llm/models/model.gguf
if [ ! -s "$MODEL" ]; then
  for URL in \
    https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
    https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf; do
    if curl -fL --retry 3 -o "$MODEL.part" "$URL"; then mv "$MODEL.part" "$MODEL"; echo "$URL" > /opt/llm/models/source.txt; break; fi
  done
fi
[ -s "$MODEL" ] || { echo "no model downloaded"; exit 1; }
cat > /etc/systemd/system/llama.service <<'SVC'
[Unit]
Description=Local LLM (llama.cpp) for the OrderFlow news filter
After=network-online.target
[Service]
ExecStart=/opt/llm/llama.cpp/build/bin/llama-server -m /opt/llm/models/model.gguf --host 127.0.0.1 --port 8081 -c 4096 -t 2 --parallel 1 --alias local-news-model
Restart=always
RestartSec=10
Nice=10
[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload
systemctl enable --now llama
grep -q '^LLM_URL=' /etc/oft/oft.env || { echo 'LLM_URL=http://127.0.0.1:8081' >> /etc/oft/oft.env; systemctl restart oft; }
for i in $(seq 1 60); do curl -fs http://127.0.0.1:8081/health >/dev/null && break; sleep 5; done
curl -s http://127.0.0.1:8081/health; echo
echo "llm ready: $(cat /opt/llm/models/source.txt)"
