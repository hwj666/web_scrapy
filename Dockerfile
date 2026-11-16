# 基础镜像（极少变动 -> 放最前）
FROM ghcr.nju.edu.cn/astral-sh/uv:python3.11-bookworm-slim

# 全局环境变量：将虚拟环境隔离在 /opt/venv，并加入系统 PATH
ENV UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 系统依赖: ffmpeg + libatomic1(playwright node 驱动依赖) + aria2
RUN sed -i \
        -e 's|deb.debian.org|mirrors.huaweicloud.com|g' \
        -e 's|security.debian.org|mirrors.huaweicloud.com|g' \
        /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates libatomic1 aria2 && \
    rm -rf /var/lib/apt/lists/*


# 安装 videoduplicatefinder CLI 工具到全局可执行目录（aria2c 16 线程下载）
# 包内路径按 release 版本可能变化（如 outputCLI/vdf-cli），这里自动查找，避免升级后失效
RUN aria2c -k 1M -x 8 -s 8 -o /tmp/vdf.tar.gz "https://v6.gh-proxy.org/https://github.com/0x90d/videoduplicatefinder/releases/download/v4.1.1/CLI-linux-x64.tar.gz" && \
    mkdir -p /opt/vdf && \
    tar -xzf /tmp/vdf.tar.gz -C /opt/vdf && \
    CLI="$(find /opt/vdf -type f -name 'vdf-cli' | head -n 1)" && \
    test -n "$CLI" && \
    chmod +x "$CLI" && \
    ln -sf "$CLI" /usr/local/bin/vdf && \
    rm -f /tmp/vdf.tar.gz && \
    vdf --help >/dev/null 2>&1 || echo "warn: vdf 自检未通过（可能缺少 ffmpeg），运行时再验证"

# 预置查重的 AI 组件, 烘焙进镜像, 免得首次扫描时再联网下载(低配机器上这步又慢又容易超时)。
#   1) ONNX Runtime 1.23.2 的 linux-x64 运行时库
#   2) DINOv2-small int8 视觉模型 dinov2-small-int8.onnx
# VDF 判定"组件已就绪"的条件是: <StateFolder>/ai 下能找到 onnxruntime 库,
# 且 runtime.version 的内容等于它编译进去的版本号 —— 两样缺一它就会重新下载。
# ⚠ 升级 vdf 版本时, 这两个 ARG 必须跟着改成新版本内置的值。
ARG VDF_ORT_VER=1.23.2
ARG VDF_MODEL_SHA=3afdc8bc63b50558d6e5770f5b799bb82455c2311183a2de43803f343a29d917
RUN mkdir -p /opt/vdf-state/VDF/ai && \
    aria2c -k 1M -x 8 -s 8 -o /tmp/ort.tgz \
      "https://v6.gh-proxy.org/https://github.com/microsoft/onnxruntime/releases/download/v${VDF_ORT_VER}/onnxruntime-linux-x64-${VDF_ORT_VER}.tgz" && \
    aria2c -k 1M -x 8 -s 8 -o "/opt/vdf-state/VDF/ai/dinov2-small-int8.onnx" \
      "https://v6.gh-proxy.org/https://github.com/0x90d/videoduplicatefinder/releases/download/ai-models-v1/dinov2-small-int8.onnx" && \
    echo "${VDF_MODEL_SHA}  /opt/vdf-state/VDF/ai/dinov2-small-int8.onnx" | sha256sum -c - && \
    tar -xzf /tmp/ort.tgz -C /tmp && \
    cp -a /tmp/onnxruntime-linux-x64-*/lib/libonnxruntime*.so* /opt/vdf-state/VDF/ai/ && \
    printf '%s' "${VDF_ORT_VER}" > /opt/vdf-state/VDF/ai/runtime.version && \
    rm -rf /tmp/ort.tgz /tmp/onnxruntime-linux-x64-* && \
    ls -l /opt/vdf-state/VDF/ai

RUN uv run --with playwright==1.65.2 playwright install --with-deps chromium && \
    chmod -R 755 /ms-playwright && \
    uv cache clean && \
    rm -rf /var/lib/apt/lists/*