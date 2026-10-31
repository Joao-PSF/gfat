# Serviço GPU do GeoFAT (Runpod GPU Pod ou qualquer host NVIDIA com nvidia-container-toolkit).
#   docker build -t geofat-gpu gpu/                      (OCR: --build-arg OCR=1)
#   docker run --gpus all -p 8000:8000 -e GEOFAT_GPU_TOKEN=... -v /caminho/modelos:/workspace/modelos geofat-gpu
# Versões iguais às do GeoFAT local (requirements-ia.txt), com PyTorch CUDA 12.6 e llama.cpp b11846
# (a mesma build testada com o Bonsai no README).

ARG CUDA=12.6.3

FROM nvidia/cuda:${CUDA}-devel-ubuntu24.04 AS llama
ARG LLAMA_CPP=b11846
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# 75 = T4, 80 = A100, 86 = RTX 30xx/A40/A6000, 89 = RTX 40xx/L4/L40S, 90 = H100
RUN git clone --depth 1 --branch ${LLAMA_CPP} https://github.com/ggml-org/llama.cpp /src \
 && cmake -S /src -B /src/build -DGGML_CUDA=ON -DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF \
          -DCMAKE_CUDA_ARCHITECTURES="75;80;86;89;90" -DCMAKE_BUILD_TYPE=Release \
 && cmake --build /src/build --target llama-server -j"$(nproc)"

FROM nvidia/cuda:${CUDA}-cudnn-runtime-ubuntu24.04
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/workspace/hf MODELOS_DIR=/workspace/modelos \
    LLAMA_SERVER=/opt/llama/llama-server PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv libgomp1 libgl1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/* && python3 -m venv /opt/venv
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
ARG OCR=0
# shortcut: PaddleOCR sem versão fixada e não testado na GPU; fixar as versões depois do primeiro teste no pod
RUN if [ "$OCR" = "1" ]; then pip install paddleocr paddlepaddle-gpu; fi
COPY --from=llama /src/build/bin/llama-server /opt/llama/llama-server
COPY servico.py /app/servico.py
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8000/saude || exit 1
CMD ["uvicorn", "servico:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
